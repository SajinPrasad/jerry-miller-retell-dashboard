"""The inbound GHL webhook for Phase 4 (GHL -> HCP reverse sync).

Structured like `apps/hcp/webhooks.py`'s HCP-side webhook and `apps/ghl/views.py`'s
`LocationCreatedWebhookView`: verify the signature over the raw body first, record the delivery
through `apps.audit` before anything acts on it, queue a Celery task, always answer quickly.

**Signature-verified, unlike the HCP-side webhook.** `apps/hcp/webhooks.py` is knowingly
unauthenticated because Housecall Pro's own signing scheme could not be confirmed from its
docs. GHL's is already settled and in production use for `LocationCreate`
(`apps.ghl.views.verify_webhook_signature`, reused here rather than re-implemented) - there is
no equivalent open question for this webhook.

Must be registered under the marketplace app's Advanced Settings > Webhooks, subscribed to
`ContactCreate`, `ContactUpdate`, `RecordCreate`, `RecordUpdate`, `RecordDelete` and
`AppointmentUpdate`, `AppointmentDelete`, pointing at this URL. See `handover.md` §6A for what
each one does and does not drive.
"""

import json
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.ghl.views import verify_webhook_signature
from apps.hcp import constants
from apps.hcp import reverse_tasks as tasks

logger = logging.getLogger(__name__)

# Every `custom_objects.*` + event-type combination Phase 4 actually handles. Leads, Estimates
# and Jobs carry `RecordCreate` only - the SOP's asymmetry itself, not a per-event check, is why
# an update for one of them is simply absent from this table. Job Line Items are the one
# genuinely two-way custom object.
RECORD_DISPATCH = {
    (constants.LEADS_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_lead,
    (constants.ESTIMATES_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_estimate,
    (constants.JOBS_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_job,
    (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_job_line_item,
    (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_UPDATE): tasks.sync_ghl_job_line_item,
    (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_DELETE): tasks.delete_ghl_job_line_item,
}

_RECORD_EVENTS = frozenset(
    {constants.GHL_RECORD_CREATE, constants.GHL_RECORD_UPDATE, constants.GHL_RECORD_DELETE}
)


def task_for(event_type: str, object_key: str):
    """The Celery task that handles one GHL event, or `None` if we do not handle it.

    A `RecordCreate`/`Update`/`Delete` with no matching `(object_key, event_type)` pair -
    including an update for a create-only object, which has no entry at all - resolves to
    `None` the same as an event type this app never subscribed to.
    """
    if event_type == constants.GHL_CONTACT_CREATE:
        return tasks.sync_ghl_contact_create
    if event_type == constants.GHL_CONTACT_UPDATE:
        return tasks.sync_ghl_contact_update
    if event_type in constants.GHL_APPOINTMENT_EVENTS:
        return tasks.sync_ghl_appointment
    if event_type in _RECORD_EVENTS:
        return RECORD_DISPATCH.get((object_key, event_type))
    return None


class GHLReverseSyncWebhookView(APIView):
    """POST /api/hcp/webhooks/ghl/: one URL for every Phase 4 event, across every sub-account.

    GHL's own `locationId` on the payload is what the Celery task resolves to an `HCPAccount` -
    there is one webhook URL for the whole marketplace app, the same as `LocationCreate`, not
    one per sub-account.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        # Read before anything parses the stream: the signature covers the raw body.
        raw_body = request.body
        signature = request.headers.get('X-GHL-Signature', '')
        if not verify_webhook_signature(raw_body, signature):
            logger.warning('hcp: GHL reverse-sync webhook signature rejected')
            recording.webhook_rejected(WebhookEvent.GHL, 'Signature rejected.')
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(raw_body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            recording.webhook_rejected(
                WebhookEvent.GHL, 'Body is not a JSON object.', signature_valid=True
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        event_type = str(payload.get('type') or '').strip()
        location_id = str(payload.get('locationId') or '').strip()
        if not event_type or not location_id:
            recording.webhook_rejected(
                WebhookEvent.GHL, 'Missing event type or locationId.', signature_valid=True
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        object_key = str(payload.get('objectKey') or '').strip()
        stored = recording.webhook_received(
            WebhookEvent.GHL,
            payload,
            event_type=event_type,
            event_id=str(payload.get('webhookId') or ''),
        )

        task = task_for(event_type, object_key)
        if task is None:
            logger.info('hcp: unhandled GHL event %s (object %s)', event_type, object_key)
            recording.webhook_status(stored, WebhookEvent.IGNORED)
            return Response({'message': 'Unhandled event type.'}, status=status.HTTP_200_OK)

        task.delay(payload, stored.pk)
        return Response({'message': 'accepted'}, status=status.HTTP_200_OK)
