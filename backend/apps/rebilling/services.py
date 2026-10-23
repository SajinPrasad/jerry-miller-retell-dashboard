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
    UNBILLABLE,
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
        'twilio_connected': bool(mapping.twilio_subaccount_sid),
        'rates': _rate_row(current_rate(mapping, end)),
        'usage': usage_summary(mapping, start, end),
    }


def client_detail(mapping: ClientMapping, start: date, end: date) -> dict:
    """One client: how they are mapped, what they are charged, and what they used."""
    return {
        'location_id': mapping.location_id,
        'client_name': mapping.client_name,
        'active': mapping.active,
        'billing_mode': mapping.billing_mode,
        'retell_agent_ids': mapping.retell_agent_ids or [],
        'phone_numbers': mapping.phone_numbers or [],
        'twilio_subaccount_sid': mapping.twilio_subaccount_sid,
        'rates': _rate_row(current_rate(mapping, end)),
        'rate_history': [_rate_row(rate) for rate in mapping.rates.all()],
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
