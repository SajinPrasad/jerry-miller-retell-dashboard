"""Re-running a stored webhook delivery through the same handler that first received it.

Every handler is idempotent (that is what lets the senders retry), so replaying an event whose
effect already landed changes nothing, and replaying one that failed - after the bug is fixed -
applies it. Stripe replays read the invoice from Stripe again where the live handler would.
"""

import traceback

import stripe
from django.conf import settings

from . import context
from .models import WebhookEvent
from .recording import record, webhook_status


class ReplayRefused(Exception):
    """This delivery has nothing to replay (it was rejected, so no body was kept)."""


def _replay_retell(payload: dict) -> str:
    from apps.rebilling.services import store_call

    if payload.get('event') != 'call_ended':
        return 'ignored'
    return 'stored' if store_call(payload.get('call') or {}) else 'already stored'


def _replay_stripe(payload: dict) -> str:
    from apps.rebilling.stripe_billing import apply_event

    event = stripe.Event.construct_from(payload, settings.STRIPE_SECRET_KEY)
    return 'applied' if apply_event(event) else 'ignored'


def _replay_ghl(payload: dict) -> str:
    from apps.ghl.views import sync_for_location_created

    return 'synced' if sync_for_location_created(payload) else 'ignored'


def _replay_card(payload: dict) -> str:
    from apps.rebilling.stripe_billing import handle_card_webhook

    matched = handle_card_webhook(payload)
    return f'{matched} client(s) matched' if matched else 'ignored'


HANDLERS = {
    WebhookEvent.RETELL: _replay_retell,
    WebhookEvent.STRIPE: _replay_stripe,
    WebhookEvent.GHL: _replay_ghl,
    WebhookEvent.GHL_CARD: _replay_card,
}


def replay(event: WebhookEvent) -> str:
    """Replay one stored delivery. Returns what came of it; raises if the handler does."""
    if event.status == WebhookEvent.REJECTED or event.payload is None:
        raise ReplayRefused(f'Webhook event {event.pk} was rejected and has no payload to replay.')

    previous = {'status': event.status, 'error': event.error}
    saved_webhook_event = context.webhook_event_id()
    context.set_webhook_event(event.pk)
    try:
        try:
            outcome = HANDLERS[event.source](event.payload)
        except Exception as exc:
            text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            webhook_status(event, WebhookEvent.FAILED, text)
            record(
                'webhook.replay_failed',
                model='audit.WebhookEvent',
                object_id=event.pk,
                detail={
                    'source': event.source,
                    'event_type': event.event_type,
                    'previous': previous,
                },
            )
            raise

        webhook_status(
            event, WebhookEvent.IGNORED if outcome == 'ignored' else WebhookEvent.PROCESSED
        )
        record(
            'webhook.replayed',
            model='audit.WebhookEvent',
            object_id=event.pk,
            detail={
                'source': event.source,
                'event_type': event.event_type,
                'previous': previous,
                'outcome': outcome,
            },
        )
        return outcome
    finally:
        context.set_webhook_event(saved_webhook_event)
