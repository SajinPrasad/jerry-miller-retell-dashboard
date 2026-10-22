"""Matching a Retell call to a client, storing it, and the nightly audit that catches what the
webhook missed.

Docs: https://docs.retellai.com/features/webhook-overview
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .exceptions import RetellError
from .models import ClientMapping, RetellCall, UnassignedCall

logger = logging.getLogger(__name__)

# How far back the nightly audit looks. Longer than a day so a missed run still gets caught.
RECONCILE_WINDOW_HOURS = 48
# Retell's maximum for /v3/list-calls.
PAGE_SIZE = 1000


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
        """Fill in duration and cost for calls in the window that are still missing them.

        The webhook may not carry either field. Only the window is looked at, so a call that
        legitimately has no duration (one that never connected) isn't re-fetched every night.
        """
        incomplete = Q(duration_ms__isnull=True) | Q(retell_cost_cents__isnull=True)
        pending = [
            *RetellCall.objects.filter(incomplete, started_at__gte=window_start),
            *UnassignedCall.objects.filter(incomplete, started_at__gte=window_start),
        ]

        filled = 0
        for record in pending:
            try:
                call = self._request('GET', f'/v2/get-call/{record.call_id}')
            except RetellError:
                # One bad call shouldn't end the run; tomorrow's audit tries again.
                logger.warning('retell: could not fetch call %s', record.call_id)
                continue
            record.duration_ms = call.get('duration_ms')
            record.retell_cost_cents = _cost_cents(call)
            record.save(update_fields=['duration_ms', 'retell_cost_cents'])
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


def _call_fields(call: dict) -> dict:
    return {
        'agent_id': call.get('agent_id') or '',
        'from_number': call.get('from_number') or '',
        'to_number': call.get('to_number') or '',
        'direction': call.get('direction') or '',
        'started_at': _timestamp(call.get('start_timestamp')),
        'ended_at': _timestamp(call.get('end_timestamp')),
        'duration_ms': call.get('duration_ms'),
        'retell_cost_cents': _cost_cents(call),
        'call_status': call.get('call_status') or '',
    }


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
