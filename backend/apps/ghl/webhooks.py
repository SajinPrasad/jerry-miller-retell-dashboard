"""The one GHL webhook URL for this marketplace app: `POST /api/ghl/webhooks/`.

GoHighLevel's Marketplace app UI (Advanced Settings > Webhooks) takes a single webhook URL per
app, so every event this app subscribes to comes here. The body's `type` decides where a delivery
goes:

- `LocationCreate` -> the location sync (`apps.ghl.views.sync_for_location_created`), in the
  request.
- The Phase 4 events (`ContactCreate`, `ContactUpdate`, `RecordCreate`/`Update`/`Delete`,
  `AppointmentUpdate`/`Delete`) -> the `apps.hcp` reverse-sync Celery task for that event, queued.
- Anything else -> recorded, marked `ignored` and answered 200, so GHL does not retry it.

The signature (`X-GHL-Signature`) is checked once, here, before anything else. Every delivery is
recorded through `apps.audit` before anything acts on it, under `WebhookEvent.GHL`, whichever route
it takes. `apps.audit.replay` runs a stored delivery again through `replay_outcome`, the same route.

Earlier URLs stay wired to this view, so a registration made against either of them keeps working:
`/api/ghl/webhooks/location-created/` (the `LocationCreate` URL) and `/api/hcp/webhooks/ghl/` (the
Phase 4 URL).
"""

import json
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.hcp import ghl_webhooks as hcp_routes

from . import views
from .exceptions import GHLError

logger = logging.getLogger(__name__)


def _hcp_task(payload: dict):
    """The Phase 4 Celery task for this delivery, or `None` if the reverse sync does not take it."""
    event_type = str(payload.get('type') or '').strip()
    object_key = str(payload.get('objectKey') or '').strip()
    return hcp_routes.task_for(event_type, object_key)


def replay_outcome(payload: dict) -> str:
    """Run a stored GHL delivery again, through the same route the view sends it by.

    Called synchronously rather than queued, the way `apps.hcp` replays its own deliveries: a replay
    is run by a person who wants to see what came of it. Returns the outcome `replay()` settles the
    delivery on: `synced`, a task's own outcome, or `ignored`. A handler error propagates, and
    `replay()` marks the delivery `failed`.
    """
    if str(payload.get('type') or '').strip() == views.LOCATION_CREATE_EVENT:
        return 'synced' if views.sync_for_location_created(payload) else 'ignored'
    task = _hcp_task(payload)
    if task is None:
        return 'ignored'
    # `webhook_event_id` is left out on purpose: `replay()` owns settling this delivery's status.
    outcome = task(payload)
    return 'ignored' if outcome in {'ignored', 'skipped'} else outcome


class GHLWebhookView(APIView):
    """POST /api/ghl/webhooks/: every GHL event this marketplace app subscribes to."""

    # The signature in `X-GHL-Signature` is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        # Read before anything parses the stream: the signature covers the raw body.
        raw_body = request.body
        signature = request.headers.get('X-GHL-Signature', '')
        if not views.verify_webhook_signature(raw_body, signature):
            logger.warning('ghl: webhook signature rejected')
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
        if not event_type:
            recording.webhook_rejected(
                WebhookEvent.GHL, 'Missing event type.', signature_valid=True
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        stored = recording.webhook_received(
            WebhookEvent.GHL,
            payload,
            event_type=event_type,
            event_id=str(payload.get('webhookId') or ''),
        )

        if event_type == views.LOCATION_CREATE_EVENT:
            # Synchronous, as it always was: a sync is one GET per sub-account, and a replay
            # wants the result. A failure is answered 200 (GHL retries anything else) and kept as
            # `failed`.
            with recording.processing(stored):
                try:
                    views.sync_for_location_created(payload)
                except GHLError as exc:
                    logger.exception(
                        'ghl: webhook-triggered location sync failed',
                        extra={'context': {'webhook_event': stored.pk}},
                    )
                    recording.webhook_failed(stored, exc)
            return Response(status=status.HTTP_200_OK)

        task = _hcp_task(payload)
        if task is None:
            logger.info('ghl: unhandled webhook event %s', event_type)
            recording.webhook_status(stored, WebhookEvent.IGNORED)
            return Response({'message': 'Unhandled event type.'}, status=status.HTTP_200_OK)

        task.delay(payload, stored.pk)
        return Response({'message': 'accepted'}, status=status.HTTP_200_OK)
