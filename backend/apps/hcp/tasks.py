"""Celery tasks behind the HCP webhook: one per entity family, matching the dispatch table.

Each takes the raw webhook payload and the id of the `WebhookEvent` it was recorded as, rather
than pre-parsed arguments. Two reasons: the delivery is already stored, so passing anything else
would mean two copies of the same facts that can disagree; and `recording.processing()` wants
the event so the task's own database writes carry the delivery's id on their audit rows and a
raised handler marks the delivery failed with its traceback.

Tasks are the unit of retry. Nothing here catches a `GHLError` or `HCPError` - a failure leaves
the delivery `failed` with the reason, and `manage.py replay_webhook` re-runs it once the cause
is fixed, which works because every handler is an upsert.
"""

import logging

from celery import shared_task

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.hcp import constants
from apps.hcp.models import HCPAccount, Job
from apps.hcp.services.sync import IGNORED, SKIPPED, HCPToGHLSync

logger = logging.getLogger(__name__)


def _run(webhook_event_id: int | None, payload: dict, handler) -> str:
    """Resolve the account, then run `handler(sync, payload)` inside the delivery's record.

    A company that is not connected, or connected but not activated, is `ignored` rather than
    failed: both are ordinary states, and HCP keeps sending while an operator finishes
    onboarding.
    """
    company_id = str(payload.get('company_id') or '')
    account = HCPAccount.objects.filter(hcp_company_id=company_id).first()
    event = WebhookEvent.objects.filter(pk=webhook_event_id).first() if webhook_event_id else None

    if account is None:
        logger.info('hcp: webhook for unconnected company %s', company_id)
        return _settle(event, IGNORED)
    if not account.is_active:
        # Deliberate: until the historical backfill has run, a live event would land ahead of
        # the records it belongs with. Deliveries are kept, so a replay applies them in order
        # once the account is activated.
        logger.info(
            'hcp: webhook for inactive account %s ignored',
            account.location_id,
            extra={'context': {'location_id': account.location_id}},
        )
        return _settle(event, IGNORED)

    if event is None:
        return handler(HCPToGHLSync(account), payload)

    with recording.processing(event):
        outcome = handler(HCPToGHLSync(account), payload)
        if outcome in (IGNORED, SKIPPED):
            recording.webhook_status(event, WebhookEvent.IGNORED)
    return outcome


def _settle(event: WebhookEvent | None, outcome: str) -> str:
    if event is not None:
        recording.webhook_status(event, WebhookEvent.IGNORED)
    return outcome


def _entity(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


@shared_task
def sync_customer(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(
        webhook_event_id, payload, lambda sync, body: sync.sync_customer(_entity(body, 'customer'))
    )


@shared_task
def sync_lead(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(webhook_event_id, payload, lambda sync, body: sync.sync_lead(_entity(body, 'lead')))


@shared_task
def sync_estimate(payload: dict, webhook_event_id: int | None = None) -> str:
    return _run(
        webhook_event_id, payload, lambda sync, body: sync.sync_estimate(_entity(body, 'estimate'))
    )


@shared_task
def sync_job(payload: dict, webhook_event_id: int | None = None) -> str:
    """A job, then its line items.

    The line items are reconciled in the same task rather than a second one so that a job and
    the items hanging off it cannot be half-applied by one succeeding and the other being
    retried separately - and because the reconciliation needs the job's GHL record id, which
    this is what produces.
    """

    def handle(sync: HCPToGHLSync, body: dict) -> str:
        job = _entity(body, 'job')
        outcome = sync.sync_job(job)
        if outcome != 'synced':
            return outcome
        row = Job.objects.filter(
            location_id=sync.location_id, hcp_job_id=str(job.get('id') or '')
        ).first()
        if row is not None:
            # `line_items` from the event when it carried them; otherwise read from HCP, since
            # several `job.*` events send a trimmed job with none at all.
            items = job.get('line_items') if isinstance(job.get('line_items'), list) else None
            result = sync.reconcile_line_items(row, items)
            logger.info(
                'hcp: job %s line items %s',
                row.hcp_job_id,
                result,
                extra={'context': {'location_id': sync.location_id}},
            )
        return outcome

    return _run(webhook_event_id, payload, handle)


@shared_task
def sync_job_appointment(payload: dict, webhook_event_id: int | None = None) -> str:
    def handle(sync: HCPToGHLSync, body: dict) -> str:
        appointment = _entity(body, 'appointment')
        if body.get('event') in constants.APPOINTMENT_REMOVED_EVENTS:
            return sync.remove_job_appointment(appointment)
        return sync.sync_job_appointment(_entity(body, 'job'), appointment)

    return _run(webhook_event_id, payload, handle)
