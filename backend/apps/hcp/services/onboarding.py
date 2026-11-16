"""The four onboarding steps a sub-account goes through before HCP data can sync.

Mirrors the SOP's own ordering (`local_folder/hcp_reference_project_sop.md`, "Onboarding
Steps"):

1. **Connect** - an admin pastes the company's HCP API key; `GET /company` proves it works and
   names the company. The account is stored `is_active=False`: connected, syncing nothing.
2. **Map employees** - every HCP employee that should carry an assignment is matched to one GHL
   user, one-to-one in both directions.
3. **Configure calendars** - pick the two round-robin calendars (one for estimates, one for
   jobs) and the fallback users, *and prove every mapped user is on both calendar teams*.
4. **Activate** - Phase 2 stops before this; `is_active` is flipped by the backfill step.

Step 3's team check is the reason this module exists rather than the views doing it inline. The
SOP is explicit that a user missing from a calendar team does not fail loudly: GHL rejects that
one appointment with "The user id not part of calendar team" while the lead/estimate/job it
belongs to syncs fine, so the account looks healthy and quietly drops appointments. Checking it
once at save time is much cheaper than finding it in production.
"""

import logging

from django.db import IntegrityError, transaction

from apps.ghl.services import GHLOAuthService
from apps.hcp.exceptions import HCPConfigurationError
from apps.hcp.models import HCPAccount, HCPCalendarSettings, HCPEmployeeMapping
from apps.hcp.services.hcp_client import HCPClient

logger = logging.getLogger(__name__)

# The only calendar type the integration offers, per the SOP: estimate and job appointments are
# assigned round-robin across a team, which no other GHL calendar type does.
ROUND_ROBIN = 'round_robin'

# `onboarding_status()['state']`, in the order an account passes through them.
NOT_CONNECTED = 'not_connected'
NEEDS_EMPLOYEE_MAPPING = 'needs_employee_mapping'
NEEDS_CALENDAR_SETTINGS = 'needs_calendar_settings'
READY_TO_ACTIVATE = 'ready_to_activate'
ACTIVE = 'active'


# --- step 1: connect ------------------------------------------------------------------------


def connect_account(location_id: str, api_key: str, client: HCPClient | None = None) -> HCPAccount:
    """Validate an HCP API key and store the account it belongs to.

    `GET /company` is the validation: HCP has no "check this credential" endpoint, and a key
    that cannot read the company cannot read anything else either. It also supplies the company
    id and the display metadata, so one call does both jobs.

    **Never activates.** A reconnect of the same company keeps whatever `is_active` it had -
    rotating an expired key must not silently stop a syncing account - but pointing a location
    at a *different* HCP company resets it to false, because that is a new integration wearing
    an old row and it has not been through steps 2 and 3.
    """
    company = (client or HCPClient(api_key)).get_company()

    existing = HCPAccount.objects.filter(location_id=location_id).first()
    keeps_activation = existing is not None and existing.hcp_company_id == company.id

    defaults = {
        'hcp_company_id': company.id,
        'api_key': api_key,
        'company_name': company.name,
        'company_phone': company.phone_number,
        'company_email': company.support_email,
        'timezone': company.time_zone,
    }
    if not keeps_activation:
        defaults['is_active'] = False

    try:
        with transaction.atomic():
            account, _ = HCPAccount.objects.update_or_create(
                location_id=location_id, defaults=defaults
            )
    except IntegrityError as exc:
        # `hcp_company_id` is unique across the whole table, not per location: one HCP company
        # feeding two GHL sub-accounts would duplicate every record it owns into both. Caught
        # here so it reads as the configuration mistake it is rather than a 500.
        raise HCPConfigurationError(
            'This Housecall Pro company is already connected to another sub-account.',
            details={'hcp_company_id': company.id},
        ) from exc
    return account


# --- step 2: employee mapping ---------------------------------------------------------------


def employee_directory(account: HCPAccount, service: GHLOAuthService | None = None) -> dict:
    """Both sides of the mapping screen, plus whatever is already mapped.

    The two directories are read live rather than cached in tables of their own (which is what
    the reference project's `HCPEmployee`/`LuxioUser` models do): a staff change on either side
    has to show up the next time the screen is opened, and a cached row that has to be
    "synced" first is exactly the step people forget.
    """
    service = service or GHLOAuthService()
    employees = HCPClient(account.api_key).get_employees()
    users = service.users(account.location_id)
    mappings = HCPEmployeeMapping.objects.filter(location_id=account.location_id)

    return {
        'hcp_employees': [
            {
                'id': employee.id,
                'name': employee.name,
                'first_name': employee.first_name,
                'last_name': employee.last_name,
                'email': employee.email,
                'mobile_number': employee.mobile_number,
            }
            for employee in employees
        ],
        'ghl_users': [
            {'id': user.get('id') or '', 'name': _user_name(user), 'email': user.get('email') or ''}
            for user in users
            if user.get('id')
        ],
        'mappings': [_mapping_payload(mapping) for mapping in mappings],
    }


def _user_name(user: dict) -> str:
    """What GHL calls a user. `name` when it sends one, else the two halves, else the email."""
    name = (user.get('name') or '').strip()
    if name:
        return name
    parts = f'{user.get("firstName") or ""} {user.get("lastName") or ""}'.strip()
    return parts or (user.get('email') or '')


def _mapping_payload(mapping: HCPEmployeeMapping) -> dict:
    return {
        'id': mapping.pk,
        'hcp_employee_id': mapping.hcp_employee_id,
        'hcp_employee_name': mapping.hcp_employee_name,
        'ghl_user_id': mapping.ghl_user_id,
        'ghl_user_name': mapping.ghl_user_name,
    }


def save_employee_mappings(location_id: str, rows: list[dict]) -> list[HCPEmployeeMapping]:
    """Replace a location's employee mappings with exactly `rows`.

    The whole set, not one row at a time, because the screen it backs is a table an admin edits
    and saves: an unmapping is a row that stopped being sent, and a per-row endpoint has no way
    to express one.

    Rows are matched on `hcp_employee_id` and **updated in place wherever they can be**, so the
    `HCPCalendarSettings` foreign keys pointing at them survive an unrelated edit elsewhere in
    the table. A row is deleted first only when it has to be: its employee is no longer mapped
    at all, or the GHL user it holds is being handed to a different employee in this same save.
    That second case is what lets two technicians swap GHL users in one edit without tripping
    `unique_ghl_user_per_location` halfway through.

    `rows` is already validated by `EmployeeMappingSerializer`, so it carries no duplicate HCP
    employee or GHL user; the database constraints behind this are the backstop, not the check.
    """
    wanted = {row['hcp_employee_id']: row for row in rows}
    # Which employee each GHL user is going to, after this save.
    claimed_by = {row['ghl_user_id']: row['hcp_employee_id'] for row in rows}

    with transaction.atomic():
        existing = HCPEmployeeMapping.objects.select_for_update().filter(location_id=location_id)
        stale = [
            mapping.pk
            for mapping in existing
            if mapping.hcp_employee_id not in wanted
            or claimed_by.get(mapping.ghl_user_id, mapping.hcp_employee_id)
            != mapping.hcp_employee_id
        ]
        if stale:
            # Cascades `HCPCalendarSettings.default_user` and friends to NULL (`SET_NULL`), so a
            # fallback user who just stopped being mapped stops being a fallback too. The
            # calendar step refuses to save without the defaults it needs, which is where that
            # surfaces.
            HCPEmployeeMapping.objects.filter(pk__in=stale).delete()

        for row in wanted.values():
            HCPEmployeeMapping.objects.update_or_create(
                location_id=location_id,
                hcp_employee_id=row['hcp_employee_id'],
                defaults={
                    'hcp_employee_name': row.get('hcp_employee_name', ''),
                    'ghl_user_id': row['ghl_user_id'],
                    'ghl_user_name': row.get('ghl_user_name', ''),
                },
            )

    return list(HCPEmployeeMapping.objects.filter(location_id=location_id))


# --- step 3: calendar settings ----------------------------------------------------------------


def round_robin_calendars(location_id: str, service: GHLOAuthService | None = None) -> list[dict]:
    """The sub-account's round-robin calendars - the only ones selectable for HCP scheduling."""
    service = service or GHLOAuthService()
    return [
        {
            'id': calendar.get('id') or '',
            'name': calendar.get('name') or '',
            'calendar_type': calendar.get('calendarType') or '',
        }
        for calendar in service.calendars(location_id)
        if isinstance(calendar, dict)
        and calendar.get('id')
        and calendar.get('calendarType') == ROUND_ROBIN
    ]


def save_calendar_settings(
    location_id: str,
    estimate_calendar_id: str,
    job_calendar_id: str,
    default_user_id: int | None = None,
    estimate_default_user_id: int | None = None,
    job_default_user_id: int | None = None,
    service: GHLOAuthService | None = None,
) -> HCPCalendarSettings:
    """Store which calendars this location schedules into, after proving GHL will accept them.

    The `*_user_id` arguments are `HCPEmployeeMapping` primary keys, not GHL user ids: a
    fallback has to be an employee the integration already knows how to assign, and naming a
    GHL user who is on no mapping would produce appointments nothing in HCP corresponds to.

    `default_user_id` is required, as the SOP's step 3 has it: it is who an HCP appointment with
    no technician assigned gets booked to, and an appointment with nobody to book has nowhere to
    go. The two per-calendar overrides are optional and blank on most accounts - leave one out
    and that calendar uses `default_user`.

    Refuses, before writing anything, when:

    - no employees are mapped yet (step 2 has not happened, so there is nothing to schedule);
    - no default user is named, or any named user is not one of this location's mappings;
    - either calendar is missing, unknown to this sub-account, or not round-robin;
    - both calendars are the same one (the SOP requires two distinct ones, and sharing one
      makes an estimate appointment indistinguishable from a job appointment on it);
    - any mapped GHL user, or either fallback, is not on **both** calendar teams.
    """
    service = service or GHLOAuthService()

    mappings = list(HCPEmployeeMapping.objects.filter(location_id=location_id))
    if not mappings:
        raise HCPConfigurationError(
            'Map at least one Housecall Pro employee to a GoHighLevel user first.'
        )
    by_pk = {mapping.pk: mapping for mapping in mappings}

    if default_user_id is None:
        raise HCPConfigurationError(
            'Choose a default user. Housecall Pro appointments that arrive with no technician '
            'assigned are booked to them.',
            details={'default_user': 'This field is required.'},
        )

    defaults = {}
    for field, pk in (
        ('default_user', default_user_id),
        ('estimate_default_user', estimate_default_user_id),
        ('job_default_user', job_default_user_id),
    ):
        if pk is None:
            defaults[field] = None
            continue
        if pk not in by_pk:
            raise HCPConfigurationError(
                "That fallback user is not one of this sub-account's employee mappings.",
                details={field: pk},
            )
        defaults[field] = by_pk[pk]

    calendars = {
        calendar['id']: calendar for calendar in round_robin_calendars(location_id, service)
    }
    estimate_calendar = _selected_calendar(calendars, estimate_calendar_id, 'estimate_calendar_id')
    job_calendar = _selected_calendar(calendars, job_calendar_id, 'job_calendar_id')
    if estimate_calendar_id == job_calendar_id:
        raise HCPConfigurationError(
            'The estimate and job calendars must be two different calendars.'
        )

    _assert_calendar_teams(
        service,
        location_id,
        {'estimate_calendar_id': estimate_calendar, 'job_calendar_id': job_calendar},
        # Every mapped user, which by construction already includes the fallbacks - they are
        # mappings themselves. The SOP names both, so both are checked rather than assumed.
        required={mapping.ghl_user_id: mapping.ghl_user_name for mapping in mappings},
    )

    settings_row, _ = HCPCalendarSettings.objects.update_or_create(
        location_id=location_id,
        defaults={
            'estimate_calendar_id': estimate_calendar['id'],
            'estimate_calendar_name': estimate_calendar['name'],
            'job_calendar_id': job_calendar['id'],
            'job_calendar_name': job_calendar['name'],
            **defaults,
        },
    )
    return settings_row


def _selected_calendar(calendars: dict[str, dict], calendar_id: str, field: str) -> dict:
    calendar = calendars.get(calendar_id)
    if calendar is None:
        raise HCPConfigurationError(
            'That calendar is not a round-robin calendar in this sub-account.',
            details={field: calendar_id},
        )
    return calendar


def _assert_calendar_teams(
    service: GHLOAuthService,
    location_id: str,
    calendars: dict[str, dict],
    required: dict[str, str],
) -> None:
    """Every user in `required` must be a team member on every calendar in `calendars`.

    `required` is `{ghl_user_id: display name}`; the names are only so the error can say who is
    missing in words an admin recognises, rather than handing back a list of opaque ids.
    """
    missing: dict[str, list[dict]] = {}
    for field, calendar in calendars.items():
        team = _team_member_ids(service.calendar(location_id, calendar['id']))
        absent = [
            {'ghl_user_id': user_id, 'ghl_user_name': name}
            for user_id, name in required.items()
            if user_id not in team
        ]
        if absent:
            missing[field] = absent

    if not missing:
        return

    names = sorted(
        {
            entry['ghl_user_name'] or entry['ghl_user_id']
            for rows in missing.values()
            for entry in rows
        }
    )
    calendar_names = ', '.join(calendars[field]['name'] or field for field in sorted(missing))
    logger.warning(
        'hcp: calendar team check refused a save for %s (%s missing from %s)',
        location_id,
        ', '.join(names),
        calendar_names,
        extra={'context': {'location_id': location_id}},
    )
    raise HCPConfigurationError(
        f'Add {", ".join(names)} to the team on {calendar_names} in GoHighLevel, then save '
        f'again. Appointments for a user who is not on the calendar team are rejected one by '
        f'one, while everything else keeps syncing.',
        details={'missing_team_members': missing},
    )


def _team_member_ids(calendar: dict) -> set[str]:
    members = calendar.get('teamMembers')
    if not isinstance(members, list):
        return set()
    return {
        str(member.get('userId'))
        for member in members
        if isinstance(member, dict) and member.get('userId')
    }


# --- status -----------------------------------------------------------------------------------


def onboarding_status(location_id: str) -> dict:
    """How far through onboarding one sub-account is, and what it is configured with.

    Written for a page that has to render *something* for a location nobody has touched, so an
    unconnected location is a state rather than a 404.
    """
    account = HCPAccount.objects.filter(location_id=location_id).first()
    if account is None:
        return {
            'location_id': location_id,
            'state': NOT_CONNECTED,
            'is_active': False,
            'account': None,
            'employee_mappings': [],
            'calendar_settings': None,
        }

    mappings = list(HCPEmployeeMapping.objects.filter(location_id=location_id))
    calendar_settings = HCPCalendarSettings.objects.filter(location_id=location_id).first()
    configured = bool(
        calendar_settings
        and calendar_settings.estimate_calendar_id
        and calendar_settings.job_calendar_id
    )

    if account.is_active:
        state = ACTIVE
    elif not mappings:
        state = NEEDS_EMPLOYEE_MAPPING
    elif not configured:
        state = NEEDS_CALENDAR_SETTINGS
    else:
        state = READY_TO_ACTIVATE

    return {
        'location_id': location_id,
        'state': state,
        'is_active': account.is_active,
        'account': {
            # Never the API key. It is a credential, and nothing on the page needs to read it
            # back - a replacement is pasted in whole.
            'hcp_company_id': account.hcp_company_id,
            'company_name': account.company_name,
            'company_phone': account.company_phone,
            'company_email': account.company_email,
            'timezone': account.timezone,
            'connected_at': account.created_at,
        },
        'employee_mappings': [_mapping_payload(mapping) for mapping in mappings],
        'calendar_settings': _calendar_settings_payload(calendar_settings)
        if calendar_settings
        else None,
    }


def _calendar_settings_payload(settings_row: HCPCalendarSettings) -> dict:
    return {
        'estimate_calendar_id': settings_row.estimate_calendar_id,
        'estimate_calendar_name': settings_row.estimate_calendar_name,
        'job_calendar_id': settings_row.job_calendar_id,
        'job_calendar_name': settings_row.job_calendar_name,
        'default_user': _fallback_payload(settings_row.default_user),
        'estimate_default_user': _fallback_payload(settings_row.estimate_default_user),
        'job_default_user': _fallback_payload(settings_row.job_default_user),
    }


def _fallback_payload(mapping: HCPEmployeeMapping | None) -> dict | None:
    return _mapping_payload(mapping) if mapping else None
