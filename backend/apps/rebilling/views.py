import json
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from retell.lib import verify

from apps.ghl import sso

from .models import ClientMapping
from .services import client_detail, client_rows, month_window, store_call

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


class AdminClientsView(APIView):
    """GET /api/rebilling/admin/clients/: every onboarded client, with rates and usage.

    Read-only. Opened inside a sub-account the page is one client wide, so the session's own
    scope decides what comes back - the caller cannot widen it with a parameter.
    """

    # The GHL session blob in `X-GHL-Session` is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request):
        context = sso.context_from_request(request)
        try:
            start, end = month_window(request.query_params.get('month'))
        except ValueError as exc:
            raise ValidationError({'month': str(exc)}) from exc

        return Response(
            {
                'scope': context.scope,
                'month': start.strftime('%Y-%m'),
                'clients': client_rows(start, end, location_id=context.active_location or None),
            }
        )


class AdminClientDetailView(APIView):
    """GET /api/rebilling/admin/clients/<location_id>/: one client's mapping, rates and usage."""

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request, location_id):
        context = sso.context_from_request(request)
        if context.scope == sso.LOCATION and location_id != context.active_location:
            # A sub-account session asking after another client. Refuse rather than return an
            # empty page: the difference matters if it ever shows up in a log.
            raise PermissionDenied('This session cannot read another client.')

        try:
            start, end = month_window(request.query_params.get('month'))
        except ValueError as exc:
            raise ValidationError({'month': str(exc)}) from exc

        mapping = ClientMapping.objects.filter(location_id=location_id).first()
        if mapping is None:
            raise NotFound('This client has not been mapped for billing yet.')

        return Response({'month': start.strftime('%Y-%m'), **client_detail(mapping, start, end)})
