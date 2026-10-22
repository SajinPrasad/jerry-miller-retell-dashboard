import json
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from retell.lib import verify

from .services import store_call

logger = logging.getLogger(__name__)


class RetellWebhookView(APIView):
    """POST /api/rebilling/webhooks/retell/: the account-level webhook URL set in Retell.

    Retell gives us 10 seconds and retries up to three times without a 2xx, so this answers 204
    with an empty body and keeps the work to two indexed lookups. A database failure is left to
    raise, so the retry is the thing that recovers it.
    """

    # A machine-to-machine webhook: the signature is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        # Read before anything parses the stream: the signature covers the raw body.
        raw_body = request.body
        signature = request.headers.get('X-Retell-Signature', '')
        if not verify(raw_body.decode(), settings.RETELL_API_KEY, signature):
            logger.warning('retell: webhook signature rejected')
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(raw_body)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if payload.get('event') == 'call_ended':
            store_call(payload.get('call') or {})
        return Response(status=status.HTTP_204_NO_CONTENT)
