"""Writing the audit trail and the webhook record.

`record()` never raises: a failed audit insert must not undo or block the money movement it
describes - the charge has already happened at Stripe by the time most of these run. It logs
instead, which lands in `ErrorLog`. `webhook_received()` is the opposite on purpose: if the
delivery cannot be stored the view fails, so the sender retries rather than being told it
arrived.
"""

import logging
import traceback
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

from . import context
from .models import AuditEvent, WebhookEvent
from .redaction import redact, scrub, strip_retell_pii

logger = logging.getLogger(__name__)


def record(
    action: str,
    *,
    model: str = '',
    object_id='',
    location_id: str = '',
    billing_period: str = '',
    before=None,
    after=None,
    detail: dict | None = None,
) -> AuditEvent | None:
    actor, label = context.get_actor()
    try:
        # A savepoint, so a failed insert cannot abort the transaction it was called from.
        with transaction.atomic():
            return AuditEvent.objects.create(
                actor=actor[:255],
                actor_label=label[:255],
                action=action,
                model=model,
                object_id=str(object_id),
                location_id=location_id,
                billing_period=billing_period,
                before=before,
                after=after,
                detail=scrub(detail or {}),
                webhook_event_id=context.webhook_event_id(),
                request_id=context.request_id(),
            )
    except Exception:
        logger.exception(
            'audit: could not record %s',
            action,
            extra={'context': {'location_id': location_id, 'billing_period': billing_period}},
        )
        return None


# --- webhooks --------------------------------------------------------------------------------


def webhook_received(source: str, payload: dict, *, event_type='', event_id='') -> WebhookEvent:
    """Store a verified delivery before anything acts on it."""
    if source == WebhookEvent.RETELL:
        payload = strip_retell_pii(payload)
    return WebhookEvent.objects.create(
        source=source,
        event_type=str(event_type)[:255],
        event_id=str(event_id)[:255],
        signature_valid=True,
        payload=scrub(payload),
        request_id=context.request_id(),
    )


def webhook_rejected(source: str, reason: str, *, signature_valid: bool = False) -> None:
    """Record a delivery that was not accepted, without its body. Never raises."""
    try:
        with transaction.atomic():
            WebhookEvent.objects.create(
                source=source,
                status=WebhookEvent.REJECTED,
                signature_valid=signature_valid,
                error=reason,
                request_id=context.request_id(),
            )
    except Exception:
        logger.exception('audit: could not record a rejected %s webhook', source)


def webhook_status(event: WebhookEvent, status: str, error: str = '') -> None:
    event.status = status
    event.error = redact(error)
    event.processed_at = timezone.now()
    event.save(update_fields=['status', 'error', 'processed_at'])


def webhook_failed(event: WebhookEvent, exc: BaseException) -> None:
    webhook_status(
        event,
        WebhookEvent.FAILED,
        ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )


@contextmanager
def processing(event: WebhookEvent):
    """Run a handler for a stored delivery, and settle its status from how that goes.

    Anything the handler raises marks the delivery `failed` with the traceback and is re-raised,
    so the view still answers the way it always did. A handler that finishes without setting a
    status of its own (`webhook_status`, e.g. `ignored`) leaves the delivery `processed`. Changes
    made inside carry this delivery's id on their audit rows.
    """
    saved_actor = context.get_actor()
    saved_webhook_event = context.webhook_event_id()
    context.set_actor(f'webhook:{event.source}')
    context.set_webhook_event(event.pk)
    try:
        yield event
    except Exception as exc:
        try:
            webhook_failed(event, exc)
        except Exception:
            logger.exception('audit: could not mark webhook %s failed', event.pk)
        raise
    else:
        if event.status == WebhookEvent.RECEIVED:
            webhook_status(event, WebhookEvent.PROCESSED)
    finally:
        # Put back what was there: outside a request nothing clears the context afterwards.
        context.set_actor(*saved_actor)
        context.set_webhook_event(saved_webhook_event)
