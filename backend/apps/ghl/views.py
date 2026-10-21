import logging
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import GHLError, GHLNotConnectedError
from .models import GHLOAuthToken
from .serializers import GHLOAuthTokenSerializer
from .services import GHLOAuthService

logger = logging.getLogger(__name__)


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
            service.sync_locations(agency)
        except GHLError:
            logger.exception('ghl: install failed')
            return _redirect_to_onboarding(error='install_failed')
        return _redirect_to_onboarding(status='connected')
