"""Matching a Retell call to a client, and storing it.

Docs: https://docs.retellai.com/features/webhook-overview
"""

import logging
from datetime import UTC, datetime

from .models import ClientMapping, RetellCall, UnassignedCall

logger = logging.getLogger(__name__)


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


def store_call(call: dict) -> None:
    """Store one `call_ended` call against its client, or as unassigned.

    Does nothing if we already have the call: Retell retries a webhook up to three times.
    """
    call_id = call.get('call_id')
    if not call_id:
        logger.warning('retell: call_ended without a call_id, ignored')
        return

    already_stored = (
        RetellCall.objects.filter(call_id=call_id).exists()
        or UnassignedCall.objects.filter(call_id=call_id).exists()
    )
    if already_stored:
        return

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
        return

    RetellCall.objects.get_or_create(call_id=call_id, defaults={'client': client, **fields})


def _call_fields(call: dict) -> dict:
    cost = call.get('call_cost') or {}
    combined_cost = cost.get('combined_cost')
    return {
        'agent_id': call.get('agent_id') or '',
        'from_number': call.get('from_number') or '',
        'to_number': call.get('to_number') or '',
        'direction': call.get('direction') or '',
        'started_at': _timestamp(call.get('start_timestamp')),
        'ended_at': _timestamp(call.get('end_timestamp')),
        'duration_ms': call.get('duration_ms'),
        'retell_cost_cents': None if combined_cost is None else round(combined_cost),
        'call_status': call.get('call_status') or '',
    }


def _timestamp(milliseconds: int | None) -> datetime | None:
    """Retell sends epoch milliseconds."""
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
