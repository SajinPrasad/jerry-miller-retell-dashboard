"""The inbound Housecall Pro webhook.

Structured like `apps/ghl/views.py::LocationCreatedWebhookView`: read the raw body first, record
the delivery through `apps.audit` before anything acts on it, hand the work to Celery, answer
200. Recording first is what makes the endpoint cheap and the event survivable - a handler bug
costs a retry, not the event, and `manage.py replay_webhook` re-runs it afterwards.

**Unauthenticated, knowingly.** Housecall Pro's webhook signing scheme could not be confirmed
from its own documentation (`local_folder/hcp_plan.md`, Open Question 1): third-party guides
disagree on both the header name and the algorithm, and the reference project verifies nothing
at all. Rather than implement a guess that would look like security without being any, this
endpoint accepts the delivery, records it, and acts only on payloads whose `company_id` matches
an account that is already connected *and* activated. An attacker who knows a company id can
therefore cause a mirror of data they would have to already have; they cannot reach another
sub-account or write anything of their own choosing. This is a known, temporary gap, tracked as
Open Question 1 in `local_folder/hcp_plan.md`; it closes the moment the real scheme is
confirmed against a live delivery.
"""

import json
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.hcp import constants, tasks

logger = logging.getLogger(__name__)

# HCP verifies a newly-configured webhook URL by POSTing this exact body and expecting a 200.
# It carries no company and no event, so it must be answered before any of the usual checks.
HANDSHAKE_BODY = {'foo': 'bar'}

# Which task each event family goes to. The names are in `constants` because they are HCP's
# vocabulary, not ours.
DISPATCH = [
    (constants.CUSTOMER_EVENTS, tasks.sync_customer),
    (constants.LEAD_EVENTS, tasks.sync_lead),
    (constants.ESTIMATE_EVENTS, tasks.sync_estimate),
    (constants.JOB_EVENTS, tasks.sync_job),
    (constants.APPOINTMENT_EVENTS, tasks.sync_job_appointment),
]


def task_for(event_type: str):
    """The Celery task that handles one HCP event, or `None` if we do not handle it.

    Appointment events are matched before job events even though both start `job.`, because
    `JOB_EVENTS` and `APPOINTMENT_EVENTS` are disjoint sets of exact names rather than
    prefixes - the ordering in `DISPATCH` is not load-bearing, and this stays true if someone
    later adds a name to one of them.
    """
    for names, task in DISPATCH:
        if event_type in names:
            return task
    return None


class HCPWebhookView(APIView):
    """POST /api/hcp/webhooks/: every event from every connected Housecall Pro company.

    One endpoint rather than one per event: HCP is configured with a single URL per company,
    and the body says which event it is.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        raw_body = request.body
        try:
            payload = json.loads(raw_body)
        except ValueError:
            payload = None

        if not isinstance(payload, dict):
            recording.webhook_rejected(WebhookEvent.HCP, 'Body is not a JSON object.')
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # The configuration handshake. Answered before anything else and never recorded: it is
        # not a delivery, and storing one per URL check would bury the real events.
        if payload == HANDSHAKE_BODY:
            return Response({'message': 'success'}, status=status.HTTP_200_OK)

        company_id = str(payload.get('company_id') or '').strip()
        event_type = str(payload.get('event') or '').strip()
        if not company_id or not event_type:
            recording.webhook_rejected(WebhookEvent.HCP, 'Missing company_id or event.')
            return Response(status=status.HTTP_400_BAD_REQUEST)

        stored = recording.webhook_received(
            WebhookEvent.HCP, payload, event_type=event_type, event_id=_event_id(payload)
        )

        task = task_for(event_type)
        if task is None:
            logger.info('hcp: unhandled event type %s', event_type)
            recording.webhook_status(stored, WebhookEvent.IGNORED)
            return Response({'message': 'Unhandled event type.'}, status=status.HTTP_200_OK)

        # Queued, not run: the endpoint answers while the mirror happens behind it. Whether the
        # company is connected and active is the task's call, because it is the task that has
        # to be replayable once it is.
        task.delay(payload, stored.pk)
        return Response({'message': 'accepted'}, status=status.HTTP_200_OK)


def _event_id(payload: dict) -> str:
    """The sender's own id for this delivery, for the webhook record.

    HCP has no envelope-level event id, so the id of whatever entity the event is about is used
    instead - which is what makes two deliveries about the same job findable together.
    """
    for key in ('appointment', 'job', 'estimate', 'lead', 'customer'):
        entity = payload.get(key)
        if isinstance(entity, dict) and entity.get('id'):
            return str(entity['id'])
    return ''
