import base64
import binascii
import json
import logging
from urllib.parse import urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from django.conf import settings
from django.http import HttpResponseRedirect
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import recording
from apps.audit.models import WebhookEvent

from . import sso
from .exceptions import GHLError, GHLNotConnectedError
from .models import GHLOAuthToken
from .serializers import GHLOAuthTokenSerializer
from .services import GHLOAuthService

logger = logging.getLogger(__name__)

# The only location lifecycle event this app subscribes to: fired whenever a new sub-account is
# created under the agency. Available only to agency-level apps, which is what this one is.
LOCATION_CREATE_EVENT = 'LocationCreate'


def _onboarding_payload() -> dict:
    agency = GHLOAuthToken.objects.filter(is_agency=True, is_active=True).first()
    locations = GHLOAuthToken.objects.filter(is_agency=False)
    return {
        'install_url': GHLOAuthService().install_url(),
        'agency': GHLOAuthTokenSerializer(agency).data if agency else None,
        'locations': GHLOAuthTokenSerializer(locations, many=True).data,
    }


def _redirect_to_onboarding(**params) -> HttpResponseRedirect:
    return HttpResponseRedirect(f'{settings.FRONTEND_URL}/onboarding?{urlencode(params)}')


class OnboardingView(APIView):
    """GET /api/ghl/onboarding/: install link, agency and sub-accounts (no tokens)."""

    def get(self, request):
        return Response(_onboarding_payload())


class LocationSyncView(APIView):
    """POST /api/ghl/onboarding/sync/: re-read the sub-accounts the app is installed on."""

    def post(self, request):
        service = GHLOAuthService()
        try:
            service.sync_locations(service.get_agency())
        except GHLNotConnectedError:
            return Response({'detail': 'Not connected.'}, status=status.HTTP_409_CONFLICT)
        except GHLError:
            logger.exception('ghl: location sync failed')
            return Response({'detail': 'GHL request failed.'}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(_onboarding_payload())


class OAuthCallbackView(APIView):
    """GET /api/oauth/callback/: the redirect URI registered on the marketplace app."""

    def get(self, request):
        code = request.GET.get('code')
        if not code:
            return _redirect_to_onboarding(error='install_cancelled')

        service = GHLOAuthService()
        try:
            agency = service.exchange_code(code)
        except GHLError:
            logger.exception('ghl: install failed')
            return _redirect_to_onboarding(error='install_failed')

        try:
            service.sync_locations(agency)
        except GHLError:
            logger.exception('ghl: initial location sync failed')

        return _redirect_to_onboarding(status='connected')


def _verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """GHL signs the webhook body with its own private key; this checks it against the matching
    public key (`GHL_WEBHOOK_PUBLIC_KEY`), which is the same for every marketplace app.
    """
    if not signature:
        return False
    try:
        public_key = load_pem_public_key(settings.GHL_WEBHOOK_PUBLIC_KEY.encode())
        public_key.verify(base64.b64decode(signature), raw_body)
        return True
    except (InvalidSignature, ValueError, binascii.Error):
        return False


def sync_for_location_created(payload: dict) -> bool:
    """Re-sync the sub-accounts if this is a `LocationCreate` event. False for anything else.

    Raises `GHLError` if the sync fails: the webhook view records it, and a replay retries it.
    """
    if payload.get('type') != LOCATION_CREATE_EVENT:
        return False
    service = GHLOAuthService()
    service.sync_locations(service.get_agency())
    return True


class LocationCreatedWebhookView(APIView):
    """POST /api/ghl/webhooks/location-created/: GHL's `LocationCreate` event.

    Lets a new sub-account show up automatically, the moment Jerry creates it in GHL, instead of
    waiting for someone to click "Refresh clients". GHL retries up to 12 times on anything but a
    2xx, so this always answers quickly; a sync failure here is logged and left for the next
    webhook, manual refresh or install callback to catch - the same as the initial sync in
    `OAuthCallbackView`.
    """

    # The signature in `X-GHL-Signature` is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        # Read before anything parses the stream: the signature covers the raw body.
        raw_body = request.body
        signature = request.headers.get('X-GHL-Signature', '')
        if not _verify_webhook_signature(raw_body, signature):
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

        stored = recording.webhook_received(
            WebhookEvent.GHL,
            payload,
            event_type=payload.get('type', ''),
            event_id=payload.get('webhookId', ''),
        )
        with recording.processing(stored):
            try:
                if not sync_for_location_created(payload):
                    recording.webhook_status(stored, WebhookEvent.IGNORED)
            except GHLError as exc:
                # Still answered 200 (GHL retries anything else), but the delivery is kept as
                # failed with the reason, so a replay can finish the sync.
                logger.exception(
                    'ghl: webhook-triggered location sync failed',
                    extra={'context': {'webhook_event': stored.pk}},
                )
                recording.webhook_failed(stored, exc)

        return Response(status=status.HTTP_200_OK)


class SSOSessionView(APIView):
    """POST /api/ghl/sso/session/: open the session blob the embedded admin page got from GHL.

    The page calls this once on load so it knows who is viewing and which view to render. The
    blob itself is the authentication, so nothing else guards this.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        context = sso.context_from_blob(request.data.get('key') or '')
        return Response(
            {
                'user_name': context.user_name,
                'email': context.email,
                'role': context.role,
                'is_agency_owner': context.is_agency_owner,
                'company_id': context.company_id,
                'active_location': (
                    context.active_location if context.scope == sso.LOCATION else None
                ),
                'scope': context.scope,
            }
        )
