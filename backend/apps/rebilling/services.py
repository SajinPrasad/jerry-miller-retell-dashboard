"""The two voice ingest paths, and the client each one belongs to.

Retell: a webhook per call, plus a nightly audit that catches what the webhook missed.
Twilio: a daily pull of regular call minutes, because Usage Records has no webhook.

The two never touch. They share `ClientMapping` and nothing else: Retell is one row per call,
written once; Twilio is one row per day, re-written every time we read it.

Docs: https://docs.retellai.com/features/webhook-overview
      https://www.twilio.com/docs/usage/api/usage-record
"""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .exceptions import RetellError, TwilioError
from .models import (
    CALLS,
    UNBILLABLE,
    ClientMapping,
    RetellCall,
    TwilioUsageDay,
    UnassignedCall,
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
