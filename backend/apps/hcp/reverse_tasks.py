"""Celery tasks behind the GHL reverse-sync webhook (Phase 4): one per entity family, matching
`ghl_webhooks.py`'s dispatch table.

Structured like `apps/hcp/tasks.py`'s own tasks: each takes the raw webhook payload and the id
of the `WebhookEvent` it was recorded as, resolves the account from the payload rather than a
pre-parsed argument, and runs inside `recording.processing()` so a raised `GHLError`/`HCPError`
leaves the delivery `failed` with its traceback for `manage.py replay_webhook` to retry - every
handler in `GHLToHCPSync` is written to be replayed safely (see its module docstring).
"""

import logging

from celery import shared_task

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.hcp.models import HCPAccount
from apps.hcp.services.reverse_mapping import properties_dict
from apps.hcp.services.reverse_sync import GHLToHCPSync
from apps.hcp.services.sync import IGNORED, SKIPPED

logger = logging.getLogger(__name__)


def _run(webhook_event_id: int | None, payload: dict, handler) -> str:
    """Resolve the account by `locationId`, then run `handler(sync, payload)`.

    A sub-account with no connected HCP company, or one not yet activated, is `ignored` for the
    same reason the forward direction treats it that way (`apps/hcp/tasks.py`): both are
    ordinary states to pass through quietly, not failures.
    """
    location_id = str(payload.get('locationId') or '')
    account = HCPAccount.objects.filter(location_id=location_id).first()
    event = WebhookEvent.objects.filter(pk=webhook_event_id).first() if webhook_event_id else None

    if account is None:
        logger.info('hcp: GHL reverse-sync webhook for unconnected location %s', location_id)
        return _settle(event, IGNORED)
    if not account.is_active:
        logger.info(
            'hcp: GHL reverse-sync webhook for inactive account %s ignored',
            location_id,
            extra={'context': {'location_id': location_id}},
        )
        return _settle(event, IGNORED)

    if event is None:
        return handler(GHLToHCPSync(account), payload)

    with recording.processing(event):
        outcome = handler(GHLToHCPSync(account), payload)
        if outcome in (IGNORED, SKIPPED):
            recording.webhook_status(event, WebhookEvent.IGNORED)
    return outcome


def _settle(event: WebhookEvent | None, outcome: str) -> str:
    if event is not None:
        recording.webhook_status(event, WebhookEvent.IGNORED)
    return outcome


@shared_task
def sync_ghl_contact_create(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(webhook_event_id, payload, lambda sync, body: sync.sync_contact_create(body))


@shared_task
def sync_ghl_contact_update(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(webhook_event_id, payload, lambda sync, body: sync.sync_contact_update(body))


@shared_task
def sync_ghl_lead(payload: dict, webhook_event_id: int | None = None) -> str:
    def handle(sync: GHLToHCPSync, body: dict) -> str:
        return sync.sync_lead(body, properties_dict(body.get('properties')))

    return _run(webhook_event_id, payload, handle)


@shared_task
def sync_ghl_estimate(payload: dict, webhook_event_id: int | None = None) -> str:
    def handle(sync: GHLToHCPSync, body: dict) -> str:
        return sync.sync_estimate(body, properties_dict(body.get('properties')))

    return _run(webhook_event_id, payload, handle)


@shared_task
def sync_ghl_job(payload: dict, webhook_event_id: int | None = None) -> str:
    def handle(sync: GHLToHCPSync, body: dict) -> str:
        return sync.sync_job(body, properties_dict(body.get('properties')))

    return _run(webhook_event_id, payload, handle)


@shared_task
def sync_ghl_job_line_item(payload: dict, webhook_event_id: int | None = None) -> str:
    def handle(sync: GHLToHCPSync, body: dict) -> str:
        return sync.sync_job_line_item(body, properties_dict(body.get('properties')))

    return _run(webhook_event_id, payload, handle)


@shared_task
def delete_ghl_job_line_item(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(webhook_event_id, payload, lambda sync, body: sync.delete_job_line_item(body))


@shared_task
def sync_ghl_appointment(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(webhook_event_id, payload, lambda sync, body: sync.sync_appointment(body))
