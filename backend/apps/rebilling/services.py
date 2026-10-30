"""The two voice ingest paths, and the client each one belongs to.

Retell: a webhook per call, plus a nightly audit that catches what the webhook missed.
Twilio: a daily pull of regular call minutes, because Usage Records has no webhook.

The two never touch. They share `ClientMapping` and nothing else: Retell is one row per call,
written once; Twilio is one row per day, re-written every time we read it.

Docs: https://docs.retellai.com/features/webhook-overview
      https://www.twilio.com/docs/usage/api/usage-record
"""

import calendar
import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx
from django.conf import settings
from django.db.models import Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ghl.models import GHLOAuthToken

from .exceptions import RetellError, TwilioError
from .models import (
    BILLABLE,
    CALLS,
    DEFAULT_CALL_RATE_CENTS,
    DEFAULT_VOICE_RATE_CENTS,
    RETELL_LINE,
    TWILIO_LINE,
    UNBILLABLE,
    Bill,
    BillLine,
    ClientMapping,
    RateHistory,
    RetellCall,
    TwilioUsageDay,
    UnassignedCall,
    billable_minutes,
    billing_state,
)

logger = logging.getLogger(__name__)

# How far back the nightly audit looks. Longer than a day so a missed run still gets caught.
RECONCILE_WINDOW_HOURS = 48
# Retell's maximum for /v3/list-calls.
PAGE_SIZE = 1000

# How many days back the daily Twilio pull re-reads, today included. A day keeps settling after
# it ends and a re-pull only updates rows, so this is cheap insurance against a missed run.
TWILIO_WINDOW_DAYS = 3
# Twilio's maximum page size for Usage Records (the default is 50).
TWILIO_PAGE_SIZE = 1000


def normalise_number(value: str) -> str:
    """Digits only, so '+1 (213) 777-1234' and '+12137771234' compare equal."""
    return ''.join(c for c in (value or '') if c.isdigit())


def match_client(call: dict) -> ClientMapping | None:
    """The client a call belongs to: by Retell agent first, then by phone number."""
    mappings = list(ClientMapping.objects.filter(active=True))

    agent_id = call.get('agent_id')
    if agent_id:
        for mapping in mappings:
            if agent_id in mapping.retell_agent_ids:
                return mapping

    # The client's own number: the one dialled on the way in, the one dialled from on the way out.
    own_number = (
        call.get('to_number') if call.get('direction') == 'inbound' else call.get('from_number')
    )
    wanted = normalise_number(own_number)
    if wanted:
        for mapping in mappings:
            if any(normalise_number(n) == wanted for n in mapping.phone_numbers):
                return mapping
    return None


def store_call(call: dict) -> bool:
    """Store one call against its client, or as unassigned. True if it was new.

    Does nothing if we already have the call: Retell retries a webhook up to three times, and the
    nightly audit re-reads calls the webhook already delivered.
    """
    call_id = call.get('call_id')
    if not call_id:
        logger.warning('retell: call without a call_id, ignored')
        return False

    already_stored = (
        RetellCall.objects.filter(call_id=call_id).exists()
        or UnassignedCall.objects.filter(call_id=call_id).exists()
    )
    if already_stored:
        return False

    fields = _call_fields(call)
    client = match_client(call)
    if client is None:
        UnassignedCall.objects.get_or_create(call_id=call_id, defaults=fields)
        logger.warning(
            'retell: call %s matched no client (agent=%s, from=%s, to=%s)',
            call_id,
            call.get('agent_id'),
            call.get('from_number'),
            call.get('to_number'),
        )
        return True

    RetellCall.objects.get_or_create(call_id=call_id, defaults={'client': client, **fields})
    return True


class RetellReconciliationService:
    """The nightly audit: catch calls whose webhook never arrived, and fill in what it left out.

    Also answers the admin page's "which agents are there?" with the same client and key.

    Docs: https://docs.retellai.com/api-references/list-calls
    """

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=settings.RETELL_API_BASE_URL, timeout=settings.RETELL_REQUEST_TIMEOUT
        )

    def reconcile(self) -> dict:
        """Audit the last `RECONCILE_WINDOW_HOURS`. Returns what it did, for the command."""
        window_start = timezone.now() - timedelta(hours=RECONCILE_WINDOW_HOURS)

        seen = stored = 0
        for call in self._list_calls(window_start):
            seen += 1
            stored += store_call(call)

        return {'seen': seen, 'stored': stored, 'backfilled': self._backfill(window_start)}

    def _list_calls(self, window_start):
        """Every call Retell has in the window, a page at a time."""
        pagination_key = None
        while True:
            body = {
                'filter_criteria': {
                    'start_timestamp': {
                        'type': 'range',
                        'op': 'bt',
                        'value': [_epoch_ms(window_start), _epoch_ms(timezone.now())],
                    }
                },
                'limit': PAGE_SIZE,
            }
            if pagination_key:
                body['pagination_key'] = pagination_key

            page = self._request('POST', '/v3/list-calls', json=body)
            yield from page.get('items', [])

            pagination_key = page.get('pagination_key')
            if not (page.get('has_more') and pagination_key):
                return

    def _backfill(self, window_start) -> int:
        """Re-read calls in the window we can't bill yet, or that are still missing a cost.

        The webhook may carry neither duration nor cost, and a call that was still running when we
        first saw it has ended by now. Only the window is looked at, so a call that will never be
        billable (one that never connected) isn't re-fetched every night forever.
        """
        pending = Q(retell_cost_cents__isnull=True) | Q(billing_status=UNBILLABLE)
        records = [
            *RetellCall.objects.filter(pending, started_at__gte=window_start),
            *UnassignedCall.objects.filter(pending, started_at__gte=window_start),
        ]

        filled = 0
        for record in records:
            try:
                call = self._request('GET', f'/v2/get-call/{record.call_id}')
            except RetellError:
                # One bad call shouldn't end the run; tomorrow's audit tries again.
                logger.warning('retell: could not fetch call %s', record.call_id)
                continue
            record.duration_ms = _duration_ms(call)
            record.retell_cost_cents = _cost_cents(call)
            record.call_status = call.get('call_status') or ''
            record.ended_at = _timestamp(call.get('end_timestamp')) or record.ended_at
            record.billing_status, record.unbillable_reason = billing_state(
                record.duration_ms, record.started_at, record.call_status
            )
            record.save(
                update_fields=[
                    'duration_ms',
                    'retell_cost_cents',
                    'call_status',
                    'ended_at',
                    'billing_status',
                    'unbillable_reason',
                ]
            )
            filled += 1
        return filled

    def list_agents(self) -> list[dict]:
        """Every agent on the Retell account, for the admin page's agent picker.

        `/v2/list-agents` returns one row per agent rather than one per agent version, which is
        what someone mapping a client is choosing between. Sorted by name here because Retell
        sorts by timestamp, and a picker is read alphabetically.

        Docs: https://docs.retellai.com/api-references/list-agents
        """
        agents = []
        pagination_key = None
        while True:
            params = {'limit': PAGE_SIZE}
            if pagination_key:
                params['pagination_key'] = pagination_key

            page = self._request('POST', '/v2/list-agents', params=params, json={})
            agents.extend(
                {'agent_id': item['agent_id'], 'agent_name': item.get('agent_name') or ''}
                for item in page.get('items', [])
                if item.get('agent_id')
            )

            pagination_key = page.get('pagination_key')
            if not (page.get('has_more') and pagination_key):
                break

        agents.sort(key=lambda agent: (agent['agent_name'].lower(), agent['agent_id']))
        return agents

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {settings.RETELL_API_KEY}',
        }
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Includes 429. The window covers two days, so the next run picks up whatever this
            # one missed rather than us retrying in a loop here.
            raise RetellError(f'Retell {method} {path} failed: {exc}') from exc


class TwilioUsageService:
    """The daily pull of regular call minutes, one mapped Twilio subaccount at a time.

    Only the `calls` category is read. Twilio's own definition of it excludes SIP and trunking
    calls, which is where Retell's traffic lands, so an AI minute can never be billed twice.

    Also answers the admin page's "which subaccounts are there?" with the same credentials.

    Docs: https://www.twilio.com/docs/usage/api/usage-record
    """

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=settings.TWILIO_API_BASE_URL,
            timeout=settings.TWILIO_REQUEST_TIMEOUT,
            # The parent account's SID and token. A main-account API key is denied access to
            # subaccount resources, so it cannot be used here.
            auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
        )

    def pull(self, start_date: date | None = None, end_date: date | None = None) -> dict:
        """Read `calls` usage for every mapped client between the two GMT days, inclusive.

        Defaults to the rolling window; pass both dates for a month-close run.
        """
        end_date = end_date or timezone.now().date()
        start_date = start_date or end_date - timedelta(days=TWILIO_WINDOW_DAYS - 1)

        result = {'clients': 0, 'days': 0, 'updated': 0, 'failed': 0}
        clients = ClientMapping.objects.filter(active=True).exclude(twilio_subaccount_sid='')
        for client in clients:
            result['clients'] += 1
            try:
                records = list(
                    self._list_records(client.twilio_subaccount_sid, start_date, end_date)
                )
            except TwilioError:
                # One client's bad day must not cost the others theirs, and tomorrow's run
                # re-reads the same window anyway.
                logger.warning(
                    'twilio: usage pull failed for %s', client.location_id, exc_info=True
                )
                result['failed'] += 1
                continue

            for record in records:
                existed = self._store(client, record)
                if existed is None:
                    continue
                result['days'] += 1
                result['updated'] += existed
        return result

    def _list_records(self, subaccount_sid: str, start_date: date, end_date: date):
        """Every `calls` usage record in the range, a page at a time."""
        path = f'/2010-04-01/Accounts/{subaccount_sid}/Usage/Records/Daily.json'
        params = {
            'Category': CALLS,
            'StartDate': start_date.isoformat(),
            'EndDate': end_date.isoformat(),
            # Defaults to true. Without this, a client whose own Twilio account has subaccounts
            # would be billed for their usage too.
            'IncludeSubaccounts': 'false',
            'PageSize': TWILIO_PAGE_SIZE,
        }
        while True:
            page = self._request('GET', path, params=params)
            yield from page.get('usage_records') or []

            next_uri = page.get('next_page_uri')
            if not next_uri:
                return
            # Twilio builds the next page's path and query itself; following it verbatim keeps
            # its cursor rather than us rebuilding one.
            path, params = next_uri, None

    def _store(self, client: ClientMapping, record: dict) -> bool | None:
        """Write one day's usage. True if the row already existed, None if it was unusable.

        `update_or_create` on purpose: Twilio keeps revising a day after it ends, so the newest
        read wins. `store_call()`'s first-write-wins rule would freeze a day at whatever it
        happened to look like the first time we saw it.
        """
        usage_date = record.get('start_date')
        if not usage_date:
            logger.warning(
                'twilio: usage record without a start_date for %s, ignored', client.location_id
            )
            return None

        _, created = TwilioUsageDay.objects.update_or_create(
            client=client,
            usage_date=date.fromisoformat(usage_date),
            category=record.get('category') or CALLS,
            defaults={
                'subaccount_sid': record.get('account_sid') or client.twilio_subaccount_sid,
                'call_count': int(record.get('count') or 0),
                'usage_minutes': _usage_minutes(record),
                'twilio_price_cents': _price_cents(record),
                'as_of': _as_of(record.get('as_of')),
            },
        )
        return not created

    def list_subaccounts(self) -> list[dict]:
        """Every subaccount under the parent account, for the admin page's subaccount picker.

        Twilio lists the parent account alongside its subaccounts. The parent is left out: a
        client mapped to it would be billed for every other client's minutes as well as its own.

        Docs: https://www.twilio.com/docs/iam/api/account#read-multiple-account-resources
        """
        subaccounts = []
        path = '/2010-04-01/Accounts.json'
        params = {'PageSize': TWILIO_PAGE_SIZE}
        while True:
            page = self._request('GET', path, params=params)
            subaccounts.extend(
                {'sid': account['sid'], 'friendly_name': account.get('friendly_name') or ''}
                for account in page.get('accounts') or []
                if account.get('sid') and account['sid'] != settings.TWILIO_ACCOUNT_SID
            )

            next_uri = page.get('next_page_uri')
            if not next_uri:
                break
            # Twilio builds the next page's path and query itself, same as the usage pull.
            path, params = next_uri, None

        subaccounts.sort(key=lambda account: (account['friendly_name'].lower(), account['sid']))
        return subaccounts

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(
                method, path, headers={'Accept': 'application/json'}, **kwargs
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Includes 429. Retrying here would stack requests behind a limit we can't see, and
            # the rolling window re-reads the same days tomorrow.
            raise TwilioError(f'Twilio {method} {path} failed: {exc}') from exc


def _call_fields(call: dict) -> dict:
    duration_ms = _duration_ms(call)
    started_at = _timestamp(call.get('start_timestamp'))
    call_status = call.get('call_status') or ''
    billing_status, reason = billing_state(duration_ms, started_at, call_status)
    return {
        'agent_id': call.get('agent_id') or '',
        'from_number': call.get('from_number') or '',
        'to_number': call.get('to_number') or '',
        'direction': call.get('direction') or '',
        'started_at': started_at,
        'ended_at': _timestamp(call.get('end_timestamp')),
        'duration_ms': duration_ms,
        'retell_cost_cents': _cost_cents(call),
        'call_status': call_status,
        'billing_status': billing_status,
        'unbillable_reason': reason,
    }


def _duration_ms(call: dict) -> int | None:
    """How long the call lasted, in milliseconds. The one field billing can't do without.

    Retell reports `duration_ms`, but a duration given in seconds is taken too, so a call is never
    left unbillable over the unit it arrived in.
    """
    milliseconds = call.get('duration_ms')
    if milliseconds is not None:
        return round(milliseconds)

    seconds = call.get('duration_seconds')
    if seconds is None:
        seconds = (call.get('call_cost') or {}).get('total_duration_seconds')
    return None if seconds is None else round(seconds * 1000)


def _cost_cents(call: dict) -> int | None:
    """call_cost.combined_cost: what Retell charges Jerry, in cents."""
    combined_cost = (call.get('call_cost') or {}).get('combined_cost')
    return None if combined_cost is None else round(combined_cost)


def _timestamp(milliseconds: int | None) -> datetime | None:
    """Retell sends epoch milliseconds."""
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _usage_minutes(record: dict) -> Decimal:
    """Twilio's `usage` for `calls`: minutes, already rounded per call by Twilio.

    Parsed as a Decimal rather than a float because this number is multiplied by a rate in
    Phase 3, and binary rounding error has no business anywhere near a bill.
    """
    return _decimal(record.get('usage'))


def _price_cents(record: dict) -> int:
    """Twilio's `price` (dollars, as a string) in whole cents. What Twilio charges Jerry.

    Absolute: `UsageRecord.price` is positive, but the per-call `Call.price` is negative, so a
    sign convention can't leak in if we ever read that endpoint too.
    """
    cents = _decimal(record.get('price')).copy_abs() * 100
    return int(cents.to_integral_value(rounding=ROUND_HALF_UP))


def _decimal(value) -> Decimal:
    """Twilio sends its numbers as strings; an absent one means no usage."""
    if value is None or value == '':
        return Decimal(0)
    return Decimal(str(value))


def _as_of(value: str | None) -> datetime | None:
    """Twilio's freshness stamp: the moment through which the day's data is current.

    Not a billing input, only a staleness signal, so a stamp we can't read is left empty and
    logged rather than failing the pull.
    """
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        logger.warning('twilio: could not read as_of %r', value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# The one write the embedded admin page makes: a client's mapping.
# --------------------------------------------------------------------------------------------


def billing_email_for(location_id: str) -> str:
    """The sub-account owner's email, as the last GHL sync stored it."""
    row = GHLOAuthToken.objects.filter(is_agency=False, location_id=location_id).first()
    return row.owner_email if row else ''


def sync_billing_emails() -> int:
    """Copy each sub-account owner's email from GoHighLevel onto its client mapping.

    Run straight after a sub-account sync, so the address a client is billed at is always the
    one GHL holds today. Nobody types it, so there is nowhere for it to drift.

    **A changed email clears the cached Stripe customer.** The id was found by looking the old
    address up; keeping it would go on charging the previous payer's card forever, since
    `customer_id()` returns a cached id without checking anything. Clearing it forces one fresh
    lookup against the new address.
    """
    owners = dict(
        GHLOAuthToken.objects.filter(is_agency=False).values_list('location_id', 'owner_email')
    )
    changed = 0
    for mapping in ClientMapping.objects.filter(location_id__in=owners):
        email = owners[mapping.location_id]
        if mapping.billing_email == email:
            continue
        mapping.billing_email = email
        mapping.stripe_customer_id = ''
        mapping.save(update_fields=['billing_email', 'stripe_customer_id'])
        changed += 1
    return changed


def set_mapping(location_id: str, default_client_name: str, fields: dict) -> ClientMapping:
    """Create or update a client's mapping from what an agency admin edited.

    `fields` holds only keys that passed `ClientMappingSerializer`: `twilio_subaccount_sid`,
    `retell_agent_ids`, `phone_numbers`. A key left out is left alone rather than cleared, so
    editing one field never has to resend the other two. `client_name` is only ever set here on
    first creation, from the sub-account's own GHL name - renaming a client is not part of this.

    `billing_email` is not among them and never typed: it is seeded from the sub-account's GHL
    owner the moment the mapping is created, and kept in step by `sync_billing_emails()`.
    """
    mapping, _ = ClientMapping.objects.get_or_create(
        location_id=location_id,
        defaults={
            'client_name': default_client_name,
            'billing_email': billing_email_for(location_id),
        },
    )
    changed = [field for field in fields if getattr(mapping, field, None) != fields[field]]
    for field in changed:
        setattr(mapping, field, fields[field])
    if changed:
        mapping.save(update_fields=changed)
    return mapping


def set_rates(mapping: ClientMapping, voice_rate_cents: int, call_rate_cents: int) -> RateHistory:
    """Put a client on new per-minute rates from today, leaving every earlier period alone.

    A rate change adds a row rather than editing one, which is the whole point of
    `RateHistory`: usage already recorded still bills at the rate that was in effect on its own
    day, and a month that has been billed cannot be changed by editing a rate afterwards.

    Saving twice in one day **updates today's row** instead of failing on the
    `(client, start_date)` unique constraint. Correcting a rate minutes after setting it is one
    decision, not two, and no usage can have been billed in between - the billing run is
    monthly.
    """
    today = timezone.now().date()
    rate, _ = RateHistory.objects.update_or_create(
        client=mapping,
        start_date=today,
        defaults={
            'voice_rate_cents': voice_rate_cents,
            'call_rate_cents': call_rate_cents,
        },
    )
    return rate


# --------------------------------------------------------------------------------------------
# Reads for the embedded admin page.
#
# Everything below is read-only and derived from what the two ingest paths already stored. It
# deliberately stops short of Phase 3: the money it reports is an estimate at the client's
# current rate, not a bill. Phase 3 applies the rate in effect on each call's day and each
# usage date, and owns `Bill`/`BillLine`. Keep that line where it is.
# --------------------------------------------------------------------------------------------

# How many of a client's most recent calls the detail view shows.
RECENT_CALL_LIMIT = 50
# How stale Twilio's own freshness stamp can get before the page flags it. The pull runs daily,
# so two days means at least one run did nothing.
TWILIO_STALE_AFTER_DAYS = 2


def month_window(month: str | None) -> tuple[date, date]:
    """First and last day of a billing month. `month` is 'YYYY-MM'; absent means this month.

    Raises `ValueError` on anything else, so a mistyped month is a 400 rather than silently
    becoming a different month's numbers.
    """
    if not month:
        today = timezone.now().date()
        year, number = today.year, today.month
    else:
        try:
            year, number = (int(part) for part in month.split('-', 1))
        except ValueError as exc:
            raise ValueError('month must look like YYYY-MM.') from exc
        if not 1 <= number <= 12 or not 2000 <= year <= 2100:
            raise ValueError('month must look like YYYY-MM.')
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def client_rows(start: date, end: date, location_id: str | None = None) -> list[dict]:
    """Every onboarded sub-account, with its mapping, rates and usage for the window.

    Sub-accounts come from Task 1's sync, so a client who has been onboarded but never mapped
    still appears - with `mapped: False`. That is the whole point of the column: it separates
    "nobody has set this client up" from "this client used nothing".
    """
    mappings = {m.location_id: m for m in ClientMapping.objects.all()}
    locations = GHLOAuthToken.objects.filter(is_agency=False).order_by('name')

    rows = []
    seen = set()
    for location in locations:
        if location_id and location.location_id != location_id:
            continue
        seen.add(location.location_id)
        rows.append(
            _client_row(
                location.location_id,
                location.name,
                mappings.get(location.location_id),
                start,
                end,
            )
        )

    # A mapping whose sub-account the sync has not seen still bills, so it must still be visible.
    for mapped_location, mapping in mappings.items():
        if mapped_location in seen or (location_id and mapped_location != location_id):
            continue
        rows.append(_client_row(mapped_location, '', mapping, start, end))

    rows.sort(key=lambda row: (row['client_name'] or '').lower())
    return rows


def _client_row(
    location_id: str, ghl_name: str, mapping: ClientMapping | None, start: date, end: date
) -> dict:
    if mapping is None:
        # No usage object rather than a zero one: an unmapped client cannot have usage, and
        # "0.00 minutes" would read as a client who made no calls.
        return {
            'location_id': location_id,
            'client_name': ghl_name,
            'mapped': False,
            'active': None,
            'billing_mode': None,
            'retell_connected': False,
            'twilio_connected': False,
            'rates': None,
            'usage': None,
        }
    return {
        'location_id': location_id,
        'client_name': mapping.client_name or ghl_name,
        'mapped': True,
        'active': mapping.active,
        'billing_mode': mapping.billing_mode,
        # Retell matches a call by agent id first, then by phone number, so either one mapped
        # means Retell calls can reach this client.
        'retell_connected': bool(mapping.retell_agent_ids or mapping.phone_numbers),
        'twilio_connected': bool(mapping.twilio_subaccount_sid),
        'rates': _rate_row(current_rate(mapping, end)),
        'usage': usage_summary(mapping, start, end),
    }


def _billing_link(mapping: ClientMapping) -> dict:
    """Who GoHighLevel says pays, and whether Stripe has been matched to them yet.

    All of it is read, never edited: the owner comes from GHL and the Stripe customer is found
    by their email on the first charge. When no customer could be matched, the reason the last
    bill recorded is shown instead of an id - that message is the whole of what an admin can act
    on, and it is the only thing distinguishing "not charged yet" from "cannot be charged".
    """
    owner = GHLOAuthToken.objects.filter(is_agency=False, location_id=mapping.location_id).first()
    latest = mapping.bills.first()
    return {
        'owner_name': owner.owner_name if owner else '',
        'billing_email': mapping.billing_email,
        'stripe_customer_id': mapping.stripe_customer_id,
        'billing_failure_reason': latest.failure_reason if latest else '',
    }


def _bill_row(mapping: ClientMapping, start: date) -> dict | None:
    """The bill for the period being read, once the monthly run has calculated one.

    `None` until then, which is what keeps the page's own estimate on screen for a month still
    in progress. Once a bill exists it is the figure that matters: it was worked out from the
    rate in effect on each call's own day, so it is what the client is actually charged, and no
    later rate change can move it.

    `failure_reason` is carried whatever the status. A charge that never reached Stripe - no
    matched customer, no card on file - records its reason while leaving the bill `uncharged`,
    so reading it only on `failed` would hide the one state nobody can act on without being
    told.
    """
    bill = mapping.bills.filter(billing_period=f'{start.year:04d}-{start.month:02d}').first()
    if bill is None:
        return None
    return {
        'status': bill.status,
        'total_cents': bill.total_cents,
        'failure_reason': bill.failure_reason,
    }


def client_detail(mapping: ClientMapping, start: date, end: date) -> dict:
    """One client: how they are mapped, what they are charged, and what they used."""
    return {
        'location_id': mapping.location_id,
        'client_name': mapping.client_name,
        'mapped': True,
        'active': mapping.active,
        'billing_mode': mapping.billing_mode,
        'retell_agent_ids': mapping.retell_agent_ids or [],
        'phone_numbers': mapping.phone_numbers or [],
        'twilio_subaccount_sid': mapping.twilio_subaccount_sid,
        **_billing_link(mapping),
        'rates': _rate_row(current_rate(mapping, end)),
        'rate_history': [_rate_row(rate) for rate in mapping.rates.all()],
        # What "Reset to defaults" on the page writes back, so the page never holds its own
        # copy of two numbers the model already owns.
        'default_rates': {
            'voice_rate_cents': DEFAULT_VOICE_RATE_CENTS,
            'call_rate_cents': DEFAULT_CALL_RATE_CENTS,
        },
        'bill': _bill_row(mapping, start),
        'usage': usage_summary(mapping, start, end),
        'twilio_days': [
            {
                'usage_date': day.usage_date.isoformat(),
                'category': day.category,
                'call_count': day.call_count,
                'usage_minutes': _minutes_str(day.usage_minutes),
                'twilio_price_cents': day.twilio_price_cents,
                'as_of': day.as_of.isoformat() if day.as_of else None,
            }
            for day in TwilioUsageDay.objects.filter(
                client=mapping, usage_date__gte=start, usage_date__lte=end
            )
        ],
        'recent_calls': [
            {
                'call_id': call.call_id,
                'agent_id': call.agent_id,
                'from_number': call.from_number,
                'to_number': call.to_number,
                'direction': call.direction,
                'started_at': call.started_at.isoformat() if call.started_at else None,
                'duration_ms': call.duration_ms,
                'billable_minutes': _minutes_str(call.billable_minutes),
                'retell_cost_cents': call.retell_cost_cents,
                'call_status': call.call_status,
                'billing_status': call.billing_status,
                'unbillable_reason': call.unbillable_reason,
            }
            for call in _calls_in_window(mapping, start, end)[:RECENT_CALL_LIMIT]
        ],
    }


def usage_summary(mapping: ClientMapping, start: date, end: date) -> dict:
    """What one client used in the window, per source, with Jerry's cost against it."""
    billable = _calls_in_window(mapping, start, end).filter(billing_status=BILLABLE)
    # Rounding is per call, so the minutes are summed in Python rather than by the database.
    ai_minutes = sum(
        (billable_minutes(ms) for ms in billable.values_list('duration_ms', flat=True)),
        Decimal(0),
    )
    ai_cost_cents = billable.aggregate(total=Sum('retell_cost_cents'))['total'] or 0
    unbillable_calls = (
        _calls_in_window(mapping, start, end).filter(billing_status=UNBILLABLE).count()
    )

    days = TwilioUsageDay.objects.filter(client=mapping, usage_date__gte=start, usage_date__lte=end)
    twilio = days.aggregate(
        minutes=Sum('usage_minutes'), cents=Sum('twilio_price_cents'), as_of=Max('as_of')
    )
    # Twilio has already rounded each call into these minutes. Never round them again.
    call_minutes = twilio['minutes'] or Decimal(0)

    rate = current_rate(mapping, end)
    estimated = None
    if rate is not None:
        estimated = int(
            (
                ai_minutes * rate.voice_rate_cents + call_minutes * rate.call_rate_cents
            ).to_integral_value(rounding=ROUND_HALF_UP)
        )

    return {
        'ai_minutes': _minutes_str(ai_minutes),
        'ai_cost_cents': ai_cost_cents,
        'call_minutes': _minutes_str(call_minutes),
        'call_cost_cents': twilio['cents'] or 0,
        'has_twilio_usage': days.exists(),
        # An estimate at the current rate, not a bill: see the note at the top of this section.
        'estimated_bill_cents': estimated,
        'unbillable_calls': unbillable_calls,
        'twilio_as_of': twilio['as_of'].isoformat() if twilio['as_of'] else None,
        'twilio_stale': _is_stale(twilio['as_of']),
    }


def current_rate(mapping: ClientMapping, end: date) -> RateHistory | None:
    """The rate in effect on the window's last day, or today if the window has not ended.

    `RateHistory` is ordered newest first, so the first row starting on or before that day is
    the one in effect. No row at all means no rate has been set for this client yet.
    """
    as_of = min(end, timezone.now().date())
    return mapping.rates.filter(start_date__lte=as_of).first()


def _minutes_str(minutes: Decimal) -> str:
    """Minutes as a fixed two-decimal string, which is what the page renders.

    Two places because that is `TwilioUsageDay.usage_minutes`' own precision, and because a
    database `Sum` drops trailing zeros, so without this one client reads `10.5` and the next
    `100.00`. This is display only: the estimate above is worked out from the full value, and
    Phase 3 bills from the stored rows, not from this string.
    """
    return str(minutes.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _rate_row(rate: RateHistory | None) -> dict | None:
    if rate is None:
        return None
    return {
        'voice_rate_cents': rate.voice_rate_cents,
        'call_rate_cents': rate.call_rate_cents,
        'start_date': rate.start_date.isoformat(),
    }


def _calls_in_window(mapping: ClientMapping, start: date, end: date):
    return RetellCall.objects.filter(
        client=mapping,
        started_at__gte=datetime.combine(start, time.min, tzinfo=UTC),
        started_at__lt=datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _is_stale(as_of: datetime | None) -> bool:
    """Whether Twilio's freshness stamp has fallen behind the daily pull.

    No stamp at all is not stale: it means no Twilio usage was read, which the page shows as a
    dash rather than as a warning.
    """
    if as_of is None:
        return False
    return as_of < timezone.now() - timedelta(days=TWILIO_STALE_AFTER_DAYS)


def calculate_bills(month: str | None = None) -> dict:
    """Calculate and store one bill per active client for one calendar month.

    `month` is 'YYYY-MM'; absent means this month (see `month_window`). One bill per client per
    period: a client already billed for this period is left untouched, so re-running a month
    that has already been billed changes nothing. A client with nothing billable from either
    source in the period gets no bill at all — a zero bill is never created.
    """
    start, end = month_window(month)
    period = f'{start.year:04d}-{start.month:02d}'

    created = 0
    already_billed = 0
    total_cents = 0

    for client in ClientMapping.objects.filter(active=True):
        if Bill.objects.filter(client=client, billing_period=period).exists():
            already_billed += 1
            continue

        bill = _bill_one_client(client, period, start, end)
        if bill is not None:
            created += 1
            total_cents += bill.total_cents
            logger.info('billing: Bill(%s, %s, %d¢)', client.location_id, period, bill.total_cents)

    return {
        'period': period,
        'bills_created': created,
        'already_billed': already_billed,
        'total_cents': total_cents,
    }


def _bill_one_client(client: ClientMapping, period: str, start: date, end: date) -> Bill | None:
    """One client's bill for one period, or None if there is nothing billable from either source."""
    retell = _retell_line(client, start, end)
    twilio = _twilio_line(client, start, end)
    if retell is None and twilio is None:
        return None

    # Saved before any charging happens, so any number on it can be explained later.
    bill = Bill.objects.create(client=client, billing_period=period, total_cents=0)
    total_cents = 0

    if retell is not None:
        BillLine.objects.create(
            bill=bill,
            line_type=RETELL_LINE,
            quantity_minutes=retell['minutes'],
            cost_cents=retell['cost_cents'],
            bill_cents=retell['bill_cents'],
        )
        RetellCall.objects.filter(pk__in=retell['call_ids']).update(bill=bill)
        total_cents += retell['bill_cents']

    if twilio is not None:
        BillLine.objects.create(
            bill=bill,
            line_type=TWILIO_LINE,
            quantity_minutes=twilio['minutes'],
            cost_cents=twilio['cost_cents'],
            bill_cents=twilio['bill_cents'],
        )
        TwilioUsageDay.objects.filter(pk__in=twilio['day_ids']).update(bill=bill)
        total_cents += twilio['bill_cents']

    bill.total_cents = total_cents
    bill.save(update_fields=['total_cents'])
    return bill


def _retell_line(client: ClientMapping, start: date, end: date) -> dict | None:
    """Sum billable AI minutes in the window, each at the rate in effect on its call's day.

    Returns None if there is no billable call in the window. A call on a day with no rate in
    effect yet is logged and left out of this bill rather than failing the whole run; it is not
    marked billed, so it is still available the next time a rate exists and this is re-run for a
    period that has not already been billed.
    """
    calls = list(_calls_in_window(client, start, end).filter(billing_status=BILLABLE))
    if not calls:
        return None

    # Fetched once per client, not once per call: a month can hold thousands of calls, and the
    # rate only changes a handful of times at most.
    rates = list(client.rates.all())

    minutes = Decimal(0)
    cost_cents = 0
    bill_cents = Decimal(0)
    call_ids = []
    for call in calls:
        rate = _rate_in_effect(rates, call.started_at.date())
        if rate is None:
            logger.warning(
                'billing: no rate for %s on %s, call %s left out of this bill',
                client.location_id,
                call.started_at.date(),
                call.call_id,
            )
            continue
        minutes += call.billable_minutes
        cost_cents += call.retell_cost_cents or 0
        bill_cents += call.billable_minutes * rate.voice_rate_cents
        call_ids.append(call.pk)

    if not call_ids:
        return None

    return {
        'minutes': minutes,
        'cost_cents': cost_cents,
        'bill_cents': int(bill_cents.to_integral_value(rounding=ROUND_HALF_UP)),
        'call_ids': call_ids,
    }


def _twilio_line(client: ClientMapping, start: date, end: date) -> dict | None:
    """Sum Twilio call minutes in the window, each at the rate in effect on its usage date.

    Returns None if the client has no Twilio subaccount mapped, or no usage day in the window.
    Twilio's minutes arrive pre-rounded and are never rounded again here.
    """
    if not client.twilio_subaccount_sid:
        return None

    days = list(
        TwilioUsageDay.objects.filter(client=client, usage_date__gte=start, usage_date__lte=end)
    )
    if not days:
        return None

    rates = list(client.rates.all())

    minutes = Decimal(0)
    cost_cents = 0
    bill_cents = Decimal(0)
    day_ids = []
    for day in days:
        rate = _rate_in_effect(rates, day.usage_date)
        if rate is None:
            logger.warning(
                'billing: no rate for %s on %s, Twilio day left out of this bill',
                client.location_id,
                day.usage_date,
            )
            continue
        minutes += day.usage_minutes
        cost_cents += day.twilio_price_cents
        bill_cents += day.usage_minutes * rate.call_rate_cents
        day_ids.append(day.pk)

    if not day_ids:
        return None

    return {
        'minutes': minutes,
        'cost_cents': cost_cents,
        'bill_cents': int(bill_cents.to_integral_value(rounding=ROUND_HALF_UP)),
        'day_ids': day_ids,
    }


def _rate_in_effect(rates: list[RateHistory], day: date) -> RateHistory | None:
    """The rate in effect on `day`, from a client's RateHistory rows (newest start_date first).

    Works against an already-fetched list rather than `current_rate`'s one-row-per-call query,
    because Phase 3 looks this up once per call or per usage day, not once per client.
    """
    for rate in rates:
        if rate.start_date <= day:
            return rate
    return None
