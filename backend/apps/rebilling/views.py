import hmac
import json
import logging

import stripe
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from retell.lib import verify

from apps.ghl import sso
from apps.ghl.exceptions import GHLError
from apps.ghl.models import GHLOAuthToken
from apps.ghl.services import GHLOAuthService

from .exceptions import RetellError, TwilioError
from .models import ClientMapping, RetellWorkspace
from .serializers import (
    ClientMappingSerializer,
    ClientRatesSerializer,
    RetellWorkspaceSerializer,
)
from .services import (
    RetellReconciliationService,
    TwilioUsageService,
    billing_facts,
    client_detail,
    client_rows,
    month_window,
    set_mapping,
    set_rates,
    set_workspace_key,
    store_workspace_call,
    sync_billing_emails,
)
from .stripe_billing import apply_event, clear_card_request, read_event, request_card

logger = logging.getLogger(__name__)


class RetellKeyRefused(APIException):
    """Retell would not answer for the key just given. Wrong key, or Retell is down."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = (
        'Retell would not accept this key. Check it is the workspace API key with the webhook '
        'badge, then try again.'
    )


class RetellClientWebhookView(APIView):
    """POST /api/rebilling/webhooks/retell/<location_id>/: one client's own Retell workspace.

    The client is in the URL because it has to be: a workspace's webhooks are signed with that
    workspace's own API key, so nothing can be verified until we know whose key to verify it
    with. The signature is still the authentication - the id in the path only picks the key,
    and a guessed one buys nothing without it. That is also why the key itself is never the
    path: it would then be in every access log and referrer along the way.

    Retell gives us 10 seconds and retries up to three times without a 2xx, so this answers
    204 with an empty body and keeps the work to three indexed touches: the workspace lookup,
    the webhook stamp, and the call. A database failure is left to raise, so the retry is the
    thing that recovers it.

    The stamp is the only way to see the one failure nothing else can - a valid key that is
    not the webhook-badged one, which passes every test this app can run and then silently
    fails every signature Retell sends.
    """

    # A machine-to-machine webhook: the signature is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request, location_id):
        # Read before anything parses the stream: the signature covers the raw body.
        raw_body = request.body

        workspace = (
            RetellWorkspace.objects.select_related('client')
            .filter(client__location_id=location_id)
            .first()
        )
        if workspace is None:
            logger.warning('retell: webhook for a sub-account with no workspace: %s', location_id)
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        signature = request.headers.get('X-Retell-Signature', '')
        if not verify(raw_body.decode(), workspace.api_key, signature):
            logger.warning('retell: webhook signature rejected for %s', location_id)
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(raw_body)
        except ValueError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # Any event that verified proves this workspace's webhook reaches us and is signed
        # with the key we hold, which is exactly what the badge trap hides. Stamped before the
        # event is looked at, because `call_started` proves it just as well as `call_ended`.
        RetellWorkspace.objects.filter(pk=workspace.pk).update(last_webhook_at=timezone.now())

        if payload.get('event') == 'call_ended':
            store_workspace_call(workspace, payload.get('call') or {})
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

        return Response(
            {
                'month': start.strftime('%Y-%m'),
                **client_detail(mapping, start, end, _webhook_base(request)),
            }
        )


def _webhook_base(request) -> str:
    """The public origin this request arrived on, which the client's webhook URL hangs off.

    Taken from the request rather than from a setting of its own: this is the host Retell will
    have to reach, and it is the one the admin reading the page is already talking to. Behind
    the proxy, `SECURE_PROXY_SSL_HEADER` is what keeps the scheme honest.
    """
    return request.build_absolute_uri('/')


class AdminClientRetellKeyView(APIView):
    """The client's own Retell workspace key: `POST` tests one, `PUT` saves it.

    `/api/rebilling/admin/clients/<location_id>/retell-key/`, agency admins only - this
    is a credential that reads a whole workspace, and a sub-account has no business setting
    one, not even its own.

    **A save always re-tests.** The two verbs run the same check against Retell; the only
    difference is whether the key is kept. Gating the write on a flag the browser sends would
    make "tested" something the page asserts rather than something the server knows.

    Both answer with the **agent names** the key found. That is not decoration: a valid key
    belonging to the *wrong* client's workspace authenticates, lists, verifies that
    workspace's webhooks and bills this client for someone else's calls. No automated check
    can catch it. Someone reading the names is the check.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request, location_id):
        self._require_agency(request)
        return Response({'agents': self._agents(request, location_id)})

    def put(self, request, location_id):
        self._require_agency(request)

        location = GHLOAuthToken.objects.filter(is_agency=False, location_id=location_id).first()
        if location is None:
            raise NotFound('This sub-account has not been onboarded.')

        fields = self._fields(request)
        agents = self._agents(request, location_id, fields)
        workspace = set_workspace_key(
            location_id,
            location.name,
            fields['api_key'],
            fields.get('workspace_name', ''),
            agents,
        )
        return Response(
            {
                'location_id': location_id,
                'workspace_name': workspace.workspace_name,
                'key_last4': workspace.key_last4,
                'agents': agents,
            }
        )

    def _require_agency(self, request):
        context = sso.context_from_request(request)
        if context.scope != sso.AGENCY:
            raise PermissionDenied('Only an agency admin can set a Retell workspace key.')

    def _fields(self, request) -> dict:
        serializer = RetellWorkspaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def _agents(self, request, location_id, fields=None) -> list:
        fields = fields if fields is not None else self._fields(request)
        try:
            return RetellReconciliationService(api_key=fields['api_key']).list_agents()
        except RetellError:
            # One message for both halves of it: the key is wrong, or Retell is unreachable.
            # Nothing here can tell them apart, and guessing would send someone to the wrong
            # place. Logged at warning rather than exception - a mistyped key is not a fault.
            logger.warning('rebilling: Retell would not accept the key for %s', location_id)
            raise RetellKeyRefused() from None


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
        return Response(
            {
                'month': start.strftime('%Y-%m'),
                **client_detail(mapping, start, end, _webhook_base(request)),
            }
        )


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
    """PUT /api/rebilling/admin/clients/<location_id>/mapping/: a client's Twilio mapping.

    Agency admins only. Opened inside a sub-account the page shows usage, not settings, so a
    location session is refused here even for its own client - there is no request to let a
    client edit their own mapping.

    Retell is not set here: a client's agents come from their own workspace, and the key that
    reads it is set by `AdminClientRetellKeyView`.
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
                'phone_numbers': mapping.phone_numbers,
            }
        )
