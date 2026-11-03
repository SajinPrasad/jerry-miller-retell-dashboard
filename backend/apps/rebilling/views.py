import hmac
import json
import logging

import stripe
from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from retell.lib import verify

from apps.ghl import sso
from apps.ghl.exceptions import GHLError
from apps.ghl.models import GHLOAuthToken
from apps.ghl.services import GHLOAuthService

from .exceptions import RetellError, TwilioError
from .models import ClientMapping
from .serializers import ClientMappingSerializer, ClientRatesSerializer
from .services import (
    RetellReconciliationService,
    TwilioUsageService,
    billing_facts,
    client_detail,
    client_rows,
    month_window,
    set_mapping,
    set_rates,
    store_call,
    sync_billing_emails,
)
from .stripe_billing import apply_event, clear_card_request, read_event, request_card

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


class StripeWebhookView(APIView):
    """POST /api/rebilling/webhooks/stripe/: how a charge's outcome gets back to us.

    Subscribed to `invoice.paid` and `invoice.payment_failed`. Stripe retries a webhook that
    does not answer 2xx, so this does one indexed lookup and one write and answers 204 empty -
    the same shape as the Retell webhook, for the same reason.
    """

    # A machine-to-machine webhook: the signature is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        # Read before anything parses the stream: the signature covers the raw body.
        raw_body = request.body
        signature = request.headers.get('Stripe-Signature', '')
        try:
            event = read_event(raw_body, signature)
        except stripe.SignatureVerificationError:
            logger.warning('stripe: webhook signature rejected')
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        apply_event(event)
        return Response(status=status.HTTP_204_NO_CONTENT)


class GHLCardWebhookView(APIView):
    """POST /api/rebilling/webhooks/ghl-card/: GoHighLevel saying a client has entered a card.

    Called by a workflow's Custom Webhook action, not by the marketplace app, so there is no
    `X-GHL-Signature` to check against GHL's own key - the workflow sends whatever headers it is
    configured with, and a shared secret is the only thing available. A blank
    `GHL_CARD_WEBHOOK_TOKEN` refuses every request rather than letting any through.

    What it is told is never taken on trust: Stripe is re-read, and only Stripe's answer clears
    the request. Answers quickly and empty, like the other two webhooks, because GHL retries
    anything that is not a 2xx.
    """

    # The shared secret in `X-Card-Webhook-Token` is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        expected = settings.GHL_CARD_WEBHOOK_TOKEN
        sent = request.headers.get('X-Card-Webhook-Token', '')
        if not expected or not hmac.compare_digest(sent, expected):
            logger.warning('rebilling: card webhook token rejected')
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(request.body)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        location_id = (payload or {}).get('locationId')
        if not location_id:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        client = ClientMapping.objects.filter(location_id=location_id).first()
        if client is None:
            # Not an error: this workflow may run for sub-accounts that are not billed here.
            logger.info('rebilling: card webhook for unmapped sub-account %s', location_id)
            return Response(status=status.HTTP_204_NO_CONTENT)

        if not clear_card_request(client):
            logger.warning(
                'rebilling: %s reported a saved card, but Stripe still has none', location_id
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


def _clients_response(context, request) -> Response:
    try:
        start, end = month_window(request.query_params.get('month'))
    except ValueError as exc:
        raise ValidationError({'month': str(exc)}) from exc

    # Only a sub-account session is narrowed to one client. An agency session widens to every
    # client regardless of what `activeLocation` happens to hold - GHL can report the agency's
    # own location there too.
    location_id = context.active_location if context.scope == sso.LOCATION else None
    return Response(
        {
            'scope': context.scope,
            'month': start.strftime('%Y-%m'),
            'twilio_billing_enabled': settings.TWILIO_BILLING_ENABLED,
            'clients': client_rows(start, end, location_id=location_id),
        }
    )


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
        return _clients_response(context, request)


class AdminClientsSyncView(APIView):
    """POST /api/rebilling/admin/clients/sync/: re-sync sub-accounts from GHL, then list them.

    What the "Refresh clients" button on the admin page calls. Agency scope only - a sub-account
    session has no "all clients" list to refresh.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request):
        context = sso.context_from_request(request)
        if context.scope == sso.LOCATION:
            raise PermissionDenied('This session cannot sync clients.')

        service = GHLOAuthService()
        try:
            service.sync_locations(service.get_agency())
        except GHLError:
            logger.exception('rebilling: admin client sync failed')
            return Response({'detail': 'GHL request failed.'}, status=status.HTTP_502_BAD_GATEWAY)

        # The owner's email travels with the sub-account, so who pays is refreshed in the same
        # step that refreshed the clients themselves.
        sync_billing_emails()
        return _clients_response(context, request)


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
            if context.scope == sso.LOCATION:
                # The client's own page, opened before an agency admin has set it up. A 404 here
                # would read as broken; this reads as "not yet", which is what it is.
                location = GHLOAuthToken.objects.filter(
                    is_agency=False, location_id=location_id
                ).first()
                return Response(
                    {
                        'month': start.strftime('%Y-%m'),
                        'location_id': location_id,
                        'client_name': location.name if location else '',
                        'mapped': False,
                        'twilio_billing_enabled': settings.TWILIO_BILLING_ENABLED,
                    }
                )
            raise NotFound('This client has not been mapped for billing yet.')

        return Response({'month': start.strftime('%Y-%m'), **client_detail(mapping, start, end)})


class AdminRetellAgentsView(APIView):
    """GET /api/rebilling/admin/retell-agents/: the Retell agents a client can be mapped to.

    What the agent picker in the mapping editor reads, so an agency admin chooses an agent off
    the Retell account instead of pasting an id. Agency scope only, because mapping itself is.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request):
        context = sso.context_from_request(request)
        if context.scope != sso.AGENCY:
            raise PermissionDenied('Only an agency admin can read the Retell agents.')

        try:
            agents = RetellReconciliationService().list_agents()
        except RetellError:
            logger.exception('rebilling: Retell agent list failed')
            return Response(
                {'detail': 'Retell request failed.'}, status=status.HTTP_502_BAD_GATEWAY
            )
        return Response({'agents': agents})


class AdminTwilioSubaccountsView(APIView):
    """GET /api/rebilling/admin/twilio-subaccounts/: the subaccounts a client can be mapped to.

    The subaccount picker's other half. Agency scope only, for the same reason.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request):
        context = sso.context_from_request(request)
        if context.scope != sso.AGENCY:
            raise PermissionDenied('Only an agency admin can read the Twilio subaccounts.')

        try:
            subaccounts = TwilioUsageService().list_subaccounts()
        except TwilioError:
            logger.exception('rebilling: Twilio subaccount list failed')
            return Response(
                {'detail': 'Twilio request failed.'}, status=status.HTTP_502_BAD_GATEWAY
            )
        return Response({'subaccounts': subaccounts})


class AdminClientRatesView(APIView):
    """PUT /api/rebilling/admin/clients/<location_id>/rates/: set a client's per-minute rates.

    Agency admins only, like the mapping write beside it: a sub-account reads its rates on its
    own page and cannot change what it is charged.

    The new rates start **today**, as a new `RateHistory` row - they do not reach back over a
    period that has already been billed, and they do not reach back over a period being viewed
    either. Reading a past month straight after saving therefore still shows that month's own
    rate, which is the honest answer and the reason this returns the whole refreshed detail
    rather than just what it wrote: the page then shows the server's figures for the period on
    screen instead of guessing which of them the save moved.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def put(self, request, location_id):
        context = sso.context_from_request(request)
        if context.scope != sso.AGENCY:
            raise PermissionDenied('Only an agency admin can edit client rates.')

        try:
            start, end = month_window(request.query_params.get('month'))
        except ValueError as exc:
            raise ValidationError({'month': str(exc)}) from exc

        mapping = ClientMapping.objects.filter(location_id=location_id).first()
        if mapping is None:
            # A rate belongs to a client mapping; there is nothing to hang one on yet.
            raise NotFound('This client has not been mapped for billing yet.')

        serializer = ClientRatesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        set_rates(mapping, **serializer.validated_data)
        return Response({'month': start.strftime('%Y-%m'), **client_detail(mapping, start, end)})


class AdminClientCardRequestView(APIView):
    """POST /api/rebilling/admin/clients/<location_id>/card-request/: ask a client for a card.

    Agency admins only, like the other two writes. This one sends an email to a client, so a
    sub-account must not be able to set its own off - and a client cannot usefully chase itself.

    Answers with the client's refreshed card status, re-read from Stripe rather than assumed, so
    a client who attached a card between the page loading and the button being pressed comes
    back as having one instead of as having been chased.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request, location_id):
        context = sso.context_from_request(request)
        if context.scope != sso.AGENCY:
            raise PermissionDenied('Only an agency admin can request a card.')

        mapping = ClientMapping.objects.filter(location_id=location_id).first()
        if mapping is None:
            raise NotFound('This client has not been mapped for billing yet.')

        try:
            request_card(mapping)
        except GHLError as exc:
            logger.exception('rebilling: card request failed for %s', location_id)
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'card': billing_facts(mapping)['card']})


class AdminClientMappingView(APIView):
    """PUT /api/rebilling/admin/clients/<location_id>/mapping/: set up or edit a client's mapping.

    Agency admins only. Opened inside a sub-account the page shows usage, not settings, so a
    location session is refused here even for its own client - there is no request to let a
    client edit their own mapping.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def put(self, request, location_id):
        context = sso.context_from_request(request)
        if context.scope != sso.AGENCY:
            raise PermissionDenied('Only an agency admin can edit client mapping.')

        location = GHLOAuthToken.objects.filter(is_agency=False, location_id=location_id).first()
        if location is None:
            raise NotFound('This sub-account has not been onboarded.')

        serializer = ClientMappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mapping = set_mapping(location_id, location.name, serializer.validated_data)
        return Response(
            {
                'location_id': mapping.location_id,
                'client_name': mapping.client_name,
                'twilio_subaccount_sid': mapping.twilio_subaccount_sid,
                'retell_agent_ids': mapping.retell_agent_ids,
                'phone_numbers': mapping.phone_numbers,
            }
        )
