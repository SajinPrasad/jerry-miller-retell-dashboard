"""The onboarding steps themselves: connect, map employees, configure calendars, report status.

The GHL half is mocked at `GHLOAuthService` rather than over HTTP - these tests are about the
onboarding rules, and `apps/ghl/tests/test_services.py` already covers that the service builds
the right requests. The HCP half goes through `respx` so the client is exercised for real.
"""

import httpx
import pytest
import respx
from django.conf import settings

from apps.hcp.exceptions import HCPAuthError, HCPConfigurationError
from apps.hcp.models import HCPAccount, HCPCalendarSettings, HCPEmployeeMapping
from apps.hcp.services import onboarding
from apps.hcp.tests.conftest import LOCATION_ID

pytestmark = pytest.mark.django_db

BASE = settings.HCP_API_BASE_URL
COMPANY_URL = f'{BASE}/company'
EMPLOYEES_URL = f'{BASE}/employees'

COMPANY = {
    'id': 'hcp-company-1',
    'name': 'Cool Breeze HVAC',
    'phone_number': '+15551230000',
    'support_email': 'support@coolbreeze.test',
    'time_zone': 'America/Chicago',
}

ESTIMATE_CALENDAR = {'id': 'cal-est', 'name': 'Design Consultation', 'calendarType': 'round_robin'}
JOB_CALENDAR = {'id': 'cal-job', 'name': 'Install', 'calendarType': 'round_robin'}


class FakeGHL:
    """Stands in for `GHLOAuthService`'s three read methods.

    `teams` is `{calendar_id: [ghl_user_id, ...]}` - the roster the calendar team check reads.
    """

    def __init__(self, users=None, calendars=None, teams=None):
        self._users = users if users is not None else []
        self._calendars = calendars if calendars is not None else [ESTIMATE_CALENDAR, JOB_CALENDAR]
        self._teams = teams or {}

    def users(self, location_id):
        return self._users

    def calendars(self, location_id):
        return self._calendars

    def calendar(self, location_id, calendar_id):
        found = next((c for c in self._calendars if c['id'] == calendar_id), {})
        members = [{'userId': user_id} for user_id in self._teams.get(calendar_id, [])]
        return {**found, 'teamMembers': members}


def make_mapping(hcp_employee_id='emp-1', ghl_user_id='user-1', location_id=LOCATION_ID):
    return HCPEmployeeMapping.objects.create(
        location_id=location_id,
        hcp_employee_id=hcp_employee_id,
        hcp_employee_name=f'Employee {hcp_employee_id}',
        ghl_user_id=ghl_user_id,
        ghl_user_name=f'User {ghl_user_id}',
    )


def both_teams(*user_ids):
    """A roster putting the same users on both calendars - the configuration that saves."""
    return {ESTIMATE_CALENDAR['id']: list(user_ids), JOB_CALENDAR['id']: list(user_ids)}


def save_calendars(**overrides):
    """A calendar save that succeeds, so each test below can change one thing about it.

    The default user is required, so it is filled in from the location's first mapping unless
    the test overrides it - otherwise every test here would be re-testing that one rule.
    """
    kwargs = {
        'estimate_calendar_id': ESTIMATE_CALENDAR['id'],
        'job_calendar_id': JOB_CALENDAR['id'],
        'service': FakeGHL(teams=both_teams('user-1')),
    }
    first = HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).order_by('pk').first()
    if first is not None:
        kwargs['default_user_id'] = first.pk
    kwargs.update(overrides)
    return onboarding.save_calendar_settings(LOCATION_ID, **kwargs)


# --- step 1: connect ---------------------------------------------------------------------


@respx.mock
def test_connect_validates_the_key_and_stores_the_company():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))

    account = onboarding.connect_account('loc-new', 'hcp-key')

    assert account.hcp_company_id == 'hcp-company-1'
    assert account.company_name == 'Cool Breeze HVAC'
    assert account.company_email == 'support@coolbreeze.test'
    assert account.timezone == 'America/Chicago'


@respx.mock
def test_a_new_account_starts_inactive():
    """Connected is not syncing: steps 2 and 3 have not happened yet."""
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))

    assert onboarding.connect_account('loc-new', 'hcp-key').is_active is False


@respx.mock
def test_the_key_is_stored_encrypted_and_reads_back_as_plaintext():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))
    onboarding.connect_account('loc-new', 'hcp-key-secret')

    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute('SELECT api_key FROM hcp_account WHERE location_id = %s', ['loc-new'])
        stored = cursor.fetchone()[0]

    assert stored != 'hcp-key-secret'
    assert HCPAccount.objects.get(location_id='loc-new').api_key == 'hcp-key-secret'


@respx.mock
def test_a_rejected_key_stores_nothing():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(401, json={'error': 'nope'}))

    with pytest.raises(HCPAuthError):
        onboarding.connect_account('loc-new', 'wrong-key')

    assert not HCPAccount.objects.filter(location_id='loc-new').exists()


@respx.mock
def test_rotating_the_key_keeps_an_active_account_active():
    """An expired key being replaced must not silently stop a syncing integration."""
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))
    HCPAccount.objects.create(
        location_id='loc-live',
        hcp_company_id='hcp-company-1',
        api_key='old-key',
        is_active=True,
    )

    account = onboarding.connect_account('loc-live', 'new-key')

    assert account.is_active is True
    assert account.api_key == 'new-key'


@respx.mock
def test_pointing_a_location_at_a_different_company_deactivates_it():
    """A new integration wearing an old row: it has not been through steps 2 and 3."""
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))
    HCPAccount.objects.create(
        location_id='loc-live',
        hcp_company_id='hcp-company-OLD',
        api_key='old-key',
        is_active=True,
    )

    assert onboarding.connect_account('loc-live', 'new-key').is_active is False


@respx.mock
def test_one_hcp_company_cannot_be_connected_to_two_sub_accounts(account):
    """It would duplicate every customer, lead, estimate and job into both sub-accounts."""
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(200, json={**COMPANY, 'id': account.hcp_company_id})
    )

    with pytest.raises(HCPConfigurationError, match='already connected'):
        onboarding.connect_account('loc-other', 'hcp-key')

    assert not HCPAccount.objects.filter(location_id='loc-other').exists()


# --- step 2: employee mapping -------------------------------------------------------------


@respx.mock
def test_the_directory_lists_both_sides_and_what_is_mapped(account):
    respx.get(EMPLOYEES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                'employees': [
                    {'id': 'emp-1', 'first_name': 'Ana', 'last_name': 'Ruiz'},
                    {'id': 'emp-2', 'first_name': 'Bo', 'last_name': 'Chen'},
                ]
            },
        )
    )
    make_mapping('emp-1', 'user-1')
    service = FakeGHL(users=[{'id': 'user-1', 'name': 'Ana R'}, {'id': 'user-2', 'name': 'Bo C'}])

    directory = onboarding.employee_directory(account, service=service)

    assert [row['id'] for row in directory['hcp_employees']] == ['emp-1', 'emp-2']
    assert [row['id'] for row in directory['ghl_users']] == ['user-1', 'user-2']
    assert [row['hcp_employee_id'] for row in directory['mappings']] == ['emp-1']


@respx.mock
def test_a_ghl_user_with_no_name_falls_back_to_its_parts_then_its_email(account):
    respx.get(EMPLOYEES_URL).mock(return_value=httpx.Response(200, json={'employees': []}))
    service = FakeGHL(
        users=[
            {'id': 'user-1', 'firstName': 'Ana', 'lastName': 'Ruiz'},
            {'id': 'user-2', 'email': 'bo@coolbreeze.test'},
        ]
    )

    users = onboarding.employee_directory(account, service=service)['ghl_users']

    assert users[0]['name'] == 'Ana Ruiz'
    assert users[1]['name'] == 'bo@coolbreeze.test'


@respx.mock
def test_a_ghl_user_with_no_id_is_not_offered(account):
    respx.get(EMPLOYEES_URL).mock(return_value=httpx.Response(200, json={'employees': []}))
    service = FakeGHL(users=[{'name': 'Ghost'}, {'id': 'user-1', 'name': 'Ana'}])

    assert [
        row['id'] for row in onboarding.employee_directory(account, service=service)['ghl_users']
    ] == ['user-1']


def test_saving_mappings_creates_them(account):
    onboarding.save_employee_mappings(
        LOCATION_ID,
        [
            {'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-1', 'ghl_user_name': 'Ana'},
            {'hcp_employee_id': 'emp-2', 'ghl_user_id': 'user-2', 'ghl_user_name': 'Bo'},
        ],
    )

    assert HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).count() == 2


def test_a_row_that_stops_being_sent_is_unmapped(account):
    make_mapping('emp-1', 'user-1')
    make_mapping('emp-2', 'user-2')

    onboarding.save_employee_mappings(
        LOCATION_ID, [{'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-1'}]
    )

    assert list(
        HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).values_list(
            'hcp_employee_id', flat=True
        )
    ) == ['emp-1']


def test_moving_an_employee_to_a_free_user_keeps_the_same_row(account):
    """Updated in place, so a calendar fallback pointing at it survives an unrelated edit."""
    mapping = make_mapping('emp-1', 'user-1')

    onboarding.save_employee_mappings(
        LOCATION_ID, [{'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-9'}]
    )

    mapping.refresh_from_db()
    assert mapping.ghl_user_id == 'user-9'


def test_two_employees_can_swap_ghl_users_in_one_save(account):
    """Both rows hold a user the other is about to take, so both are dropped before the
    rewrite - otherwise `unique_ghl_user_per_location` fires halfway through.
    """
    make_mapping('emp-1', 'user-1')
    make_mapping('emp-2', 'user-2')

    onboarding.save_employee_mappings(
        LOCATION_ID,
        [
            {'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-2'},
            {'hcp_employee_id': 'emp-2', 'ghl_user_id': 'user-1'},
        ],
    )

    assert dict(
        HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).values_list(
            'hcp_employee_id', 'ghl_user_id'
        )
    ) == {'emp-1': 'user-2', 'emp-2': 'user-1'}


def test_handing_a_user_to_another_employee_in_one_save(account):
    """emp-1 is unmapped and its GHL user goes to emp-2, in the same table edit."""
    make_mapping('emp-1', 'user-1')

    onboarding.save_employee_mappings(
        LOCATION_ID, [{'hcp_employee_id': 'emp-2', 'ghl_user_id': 'user-1'}]
    )

    assert dict(
        HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).values_list(
            'hcp_employee_id', 'ghl_user_id'
        )
    ) == {'emp-2': 'user-1'}


def test_an_empty_table_unmaps_everyone(account):
    make_mapping('emp-1', 'user-1')

    onboarding.save_employee_mappings(LOCATION_ID, [])

    assert not HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).exists()


def test_mappings_in_another_location_are_untouched(account):
    other = make_mapping('emp-1', 'user-1', location_id='loc-other')

    onboarding.save_employee_mappings(LOCATION_ID, [])

    assert HCPEmployeeMapping.objects.filter(pk=other.pk).exists()


def test_the_same_hcp_employee_can_map_in_two_locations(account):
    """The constraints are per location: two companies can both employ an "emp-1"."""
    make_mapping('emp-1', 'user-1', location_id='loc-other')

    onboarding.save_employee_mappings(
        LOCATION_ID, [{'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-1'}]
    )

    assert HCPEmployeeMapping.objects.filter(hcp_employee_id='emp-1').count() == 2


# --- step 3: calendar settings -------------------------------------------------------------


def test_only_round_robin_calendars_are_offered(account):
    service = FakeGHL(
        calendars=[
            ESTIMATE_CALENDAR,
            {'id': 'cal-personal', 'name': 'Mine', 'calendarType': 'personal'},
            {'id': 'cal-class', 'name': 'Class', 'calendarType': 'class_booking'},
        ]
    )

    assert [c['id'] for c in onboarding.round_robin_calendars(LOCATION_ID, service)] == ['cal-est']


def test_calendar_settings_save_when_every_mapped_user_is_on_both_teams(account):
    make_mapping('emp-1', 'user-1')

    saved = save_calendars()

    assert saved.estimate_calendar_id == 'cal-est'
    assert saved.estimate_calendar_name == 'Design Consultation'
    assert saved.job_calendar_id == 'cal-job'
    assert saved.job_calendar_name == 'Install'


def test_a_mapped_user_missing_from_the_job_calendar_blocks_the_save(account):
    """The failure this check exists for: GHL would reject that user's job appointments one at
    a time while the jobs themselves kept syncing, so nothing would look wrong.
    """
    make_mapping('emp-1', 'user-1')
    service = FakeGHL(teams={ESTIMATE_CALENDAR['id']: ['user-1'], JOB_CALENDAR['id']: []})

    with pytest.raises(HCPConfigurationError) as caught:
        save_calendars(service=service)

    assert 'job_calendar_id' in caught.value.details['missing_team_members']
    assert 'estimate_calendar_id' not in caught.value.details['missing_team_members']
    assert not HCPCalendarSettings.objects.filter(location_id=LOCATION_ID).exists()


def test_a_mapped_user_missing_from_the_estimate_calendar_blocks_the_save(account):
    make_mapping('emp-1', 'user-1')
    service = FakeGHL(teams={ESTIMATE_CALENDAR['id']: [], JOB_CALENDAR['id']: ['user-1']})

    with pytest.raises(HCPConfigurationError) as caught:
        save_calendars(service=service)

    assert 'estimate_calendar_id' in caught.value.details['missing_team_members']


def test_the_refusal_names_the_users_an_admin_has_to_add(account):
    make_mapping('emp-1', 'user-1')
    make_mapping('emp-2', 'user-2')
    service = FakeGHL(teams=both_teams('user-1'))

    with pytest.raises(HCPConfigurationError) as caught:
        save_calendars(service=service)

    assert 'User user-2' in caught.value.message
    assert 'User user-1' not in caught.value.message


def test_a_calendar_with_no_team_at_all_blocks_the_save(account):
    make_mapping('emp-1', 'user-1')

    with pytest.raises(HCPConfigurationError):
        save_calendars(service=FakeGHL(teams={}))


def test_every_mapped_user_is_checked_not_just_the_fallbacks(account):
    """The SOP is explicit: every user in the Employee Mapping, not only the default users."""
    default = make_mapping('emp-1', 'user-1')
    make_mapping('emp-2', 'user-2')
    service = FakeGHL(teams=both_teams('user-1'))

    with pytest.raises(HCPConfigurationError):
        save_calendars(service=service, default_user_id=default.pk)


def test_the_two_calendars_must_be_different(account):
    make_mapping('emp-1', 'user-1')

    with pytest.raises(HCPConfigurationError, match='two different calendars'):
        save_calendars(job_calendar_id=ESTIMATE_CALENDAR['id'])


def test_a_calendar_that_is_not_round_robin_is_refused(account):
    make_mapping('emp-1', 'user-1')
    service = FakeGHL(
        calendars=[ESTIMATE_CALENDAR, {'id': 'cal-job', 'name': 'J', 'calendarType': 'personal'}],
        teams=both_teams('user-1'),
    )

    with pytest.raises(HCPConfigurationError, match='round-robin'):
        save_calendars(service=service)


def test_a_calendar_from_another_sub_account_is_refused(account):
    make_mapping('emp-1', 'user-1')

    with pytest.raises(HCPConfigurationError, match='round-robin'):
        save_calendars(job_calendar_id='cal-somewhere-else')


def test_calendar_settings_need_at_least_one_mapping(account):
    """Step 2 before step 3: with nothing mapped the team check would pass vacuously."""
    with pytest.raises(HCPConfigurationError, match='Map at least one'):
        save_calendars()


def test_fallback_users_are_stored(account):
    default = make_mapping('emp-1', 'user-1')
    estimate_default = make_mapping('emp-2', 'user-2')
    service = FakeGHL(teams=both_teams('user-1', 'user-2'))

    saved = save_calendars(
        service=service, default_user_id=default.pk, estimate_default_user_id=estimate_default.pk
    )

    assert saved.default_user_id == default.pk
    assert saved.estimate_default_user_id == estimate_default.pk
    assert saved.job_default_user_id is None
    # The per-calendar property falls back to the location default where there is no override.
    assert saved.estimate_appointment_default_user == estimate_default
    assert saved.job_appointment_default_user == default


def test_a_fallback_that_is_not_a_mapping_is_refused(account):
    make_mapping('emp-1', 'user-1')

    with pytest.raises(HCPConfigurationError, match='not one of'):
        save_calendars(default_user_id=99999)


def test_a_fallback_from_another_location_is_refused(account):
    make_mapping('emp-1', 'user-1')
    foreign = make_mapping('emp-9', 'user-9', location_id='loc-other')

    with pytest.raises(HCPConfigurationError, match='not one of'):
        save_calendars(default_user_id=foreign.pk)


def test_saving_twice_updates_the_one_row(account):
    make_mapping('emp-1', 'user-1')
    save_calendars()
    save_calendars()

    assert HCPCalendarSettings.objects.filter(location_id=LOCATION_ID).count() == 1


def test_sending_null_clears_a_saved_per_calendar_override(account):
    override = make_mapping('emp-1', 'user-1')
    save_calendars(estimate_default_user_id=override.pk)

    assert save_calendars(estimate_default_user_id=None).estimate_default_user_id is None


def test_calendar_settings_need_a_default_user(account):
    """The SOP's step 3 makes it required: it is who an HCP appointment with no technician
    assigned gets booked to, and one with nobody to book has nowhere to go.
    """
    make_mapping('emp-1', 'user-1')

    with pytest.raises(HCPConfigurationError, match='default user'):
        save_calendars(default_user_id=None)

    assert not HCPCalendarSettings.objects.filter(location_id=LOCATION_ID).exists()


def test_unmapping_a_fallback_user_clears_it_from_the_calendar_settings(account):
    """`SET_NULL` on the foreign key: a fallback who stopped being mapped stops being one."""
    default = make_mapping('emp-1', 'user-1')
    make_mapping('emp-2', 'user-2')
    save_calendars(
        service=FakeGHL(teams=both_teams('user-1', 'user-2')), default_user_id=default.pk
    )

    onboarding.save_employee_mappings(
        LOCATION_ID, [{'hcp_employee_id': 'emp-2', 'ghl_user_id': 'user-2'}]
    )

    assert HCPCalendarSettings.objects.get(location_id=LOCATION_ID).default_user_id is None


# --- status -----------------------------------------------------------------------------


def test_status_of_a_sub_account_nobody_connected():
    state = onboarding.onboarding_status('loc-untouched')

    assert state['state'] == onboarding.NOT_CONNECTED
    assert state['account'] is None
    assert state['is_active'] is False


def test_status_after_connecting_asks_for_the_employee_mapping(account):
    state = onboarding.onboarding_status(LOCATION_ID)

    assert state['state'] == onboarding.NEEDS_EMPLOYEE_MAPPING
    assert state['account']['company_name'] == 'Cool Breeze HVAC'
    assert state['employee_mappings'] == []


def test_status_never_returns_the_api_key(account):
    """It is a credential, and nothing on the page reads it back - a new one is pasted whole."""
    state = onboarding.onboarding_status(LOCATION_ID)

    assert 'api_key' not in state['account']
    assert account.api_key not in str(state)


def test_status_after_mapping_asks_for_the_calendars(account):
    make_mapping('emp-1', 'user-1')

    assert onboarding.onboarding_status(LOCATION_ID)['state'] == onboarding.NEEDS_CALENDAR_SETTINGS


def test_status_is_ready_to_activate_once_both_steps_are_done(account):
    make_mapping('emp-1', 'user-1')
    save_calendars()

    state = onboarding.onboarding_status(LOCATION_ID)

    assert state['state'] == onboarding.READY_TO_ACTIVATE
    assert state['calendar_settings']['estimate_calendar_name'] == 'Design Consultation'


def test_status_is_active_once_the_account_is(account):
    make_mapping('emp-1', 'user-1')
    save_calendars()
    HCPAccount.objects.filter(pk=account.pk).update(is_active=True)

    assert onboarding.onboarding_status(LOCATION_ID)['state'] == onboarding.ACTIVE
