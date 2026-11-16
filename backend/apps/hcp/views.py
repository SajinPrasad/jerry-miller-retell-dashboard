"""The onboarding endpoints behind the Housecall Pro setup screens.

Authenticated the same way every other admin endpoint in this dashboard is: the GHL session
blob in `X-GHL-Session` (`apps/ghl/sso.py`), which is the embedded page's credential. An agency
session can configure any sub-account; a sub-account session can configure only its own. These
endpoints take an HCP API key, so leaving them open would be handing anyone who finds the URL
the ability to point a client's integration at an HCP company of their choosing.

Thin by design - each view opens the session, validates the body, and hands off to
`services/onboarding.py`, which is where the onboarding rules actually live.
"""

import logging

from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ghl import sso
from apps.ghl.exceptions import GHLError
from apps.hcp.exceptions import HCPAuthError, HCPConfigurationError, HCPError, HCPRateLimitError
from apps.hcp.models import HCPAccount
from apps.hcp.serializers import (
    CalendarSettingsSerializer,
    ConnectAccountSerializer,
    EmployeeMappingsSerializer,
)
from apps.hcp.services import onboarding

logger = logging.getLogger(__name__)


class _HCPView(APIView):
    """Shared session handling for the onboarding endpoints."""

    # The GHL session blob in `X-GHL-Session` is the authentication.
    authentication_classes: list = []
    permission_classes: list = []

    @staticmethod
    def guard(request, location_id: str) -> sso.SSOContext:
        """Open the session and refuse one sub-account reaching into another's setup."""
        context = sso.context_from_request(request)
        _HCPView.check_scope(context, location_id)
        return context

    @staticmethod
    def check_scope(context: sso.SSOContext, location_id: str) -> None:
        """Split out of `guard` for the one endpoint that learns its location from the body.

        That endpoint has to open the session *before* it validates the body - otherwise a
        caller with no session at all gets told which fields it got wrong, which is a reply
        only an authenticated caller has any business receiving.
        """
        if context.scope == sso.LOCATION and location_id != context.active_location:
            # Refuse rather than quietly return that location's own data: the difference
            # matters if it ever shows up in a log.
            raise PermissionDenied('This session cannot configure another sub-account.')

    @staticmethod
    def account(location_id: str) -> HCPAccount:
        account = HCPAccount.objects.filter(location_id=location_id).first()
        if account is None:
            raise NotFound('Housecall Pro is not connected for this sub-account.')
        return account


def _hcp_failure(exc: HCPError, action: str) -> Response:
    """One place deciding what each kind of HCP failure looks like to the page.

    The three cases need three different things from whoever is looking at the screen: paste a
    new key, wait, or try again / tell someone. Collapsing them into one 502 would make the
    first - the only one a human can fix unaided - indistinguishable from the other two.
    """
    if isinstance(exc, HCPAuthError):
        return Response(
            {'detail': str(exc), 'code': 'hcp_invalid_api_key'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, HCPRateLimitError):
        response = Response(
            {'detail': str(exc), 'code': 'hcp_rate_limited'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        if exc.retry_after is not None:
            response['Retry-After'] = str(exc.retry_after)
        return response

    logger.exception('hcp: %s failed', action)
    return Response(
        {'detail': 'Housecall Pro request failed.', 'code': 'hcp_unavailable'},
        status=status.HTTP_502_BAD_GATEWAY,
    )


def _configuration_failure(exc: HCPConfigurationError) -> Response:
    return Response({'detail': exc.message, **exc.details}, status=status.HTTP_400_BAD_REQUEST)


def _ghl_failure(action: str) -> Response:
    logger.exception('hcp: %s failed', action)
    return Response(
        {'detail': 'GoHighLevel request failed.', 'code': 'ghl_unavailable'},
        status=status.HTTP_502_BAD_GATEWAY,
    )


class ConnectAccountView(_HCPView):
    """POST /api/hcp/accounts/: step 1, connect a sub-account to a Housecall Pro company.

    The key is validated against HCP before anything is stored, so a typo is a 400 on this
    request rather than an account that looks connected and fails on its first sync.
    """

    def post(self, request):
        # The session first, then the body: this is the one endpoint whose location comes from
        # the body, and a caller with no session must not be told what the body should contain.
        context = sso.context_from_request(request)
        payload = ConnectAccountSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        location_id = payload.validated_data['location_id']
        self.check_scope(context, location_id)

        try:
            account = onboarding.connect_account(location_id, payload.validated_data['api_key'])
        except HCPConfigurationError as exc:
            return _configuration_failure(exc)
        except HCPError as exc:
            return _hcp_failure(exc, 'connect account')

        return Response(
            onboarding.onboarding_status(account.location_id),
            status=status.HTTP_201_CREATED,
        )


class StatusView(_HCPView):
    """GET /api/hcp/accounts/<location_id>/status/: how far onboarding has got, and its config.

    Answers for a sub-account nobody has connected yet (`state: "not_connected"`) rather than
    404ing: the page has to render the first step from somewhere.
    """

    def get(self, request, location_id):
        self.guard(request, location_id)
        return Response(onboarding.onboarding_status(location_id))


class EmployeesView(_HCPView):
    """GET /api/hcp/accounts/<location_id>/employees/: step 2's two directories.

    HCP employees and GHL users side by side, plus whatever is already mapped. Read live from
    both systems on every open, so a technician who joined this morning is selectable without
    anyone pressing a sync button first.
    """

    def get(self, request, location_id):
        self.guard(request, location_id)
        account = self.account(location_id)
        try:
            return Response(onboarding.employee_directory(account))
        except HCPError as exc:
            return _hcp_failure(exc, 'employee directory')
        except GHLError:
            return _ghl_failure('employee directory')


class EmployeeMappingsView(_HCPView):
    """PUT /api/hcp/accounts/<location_id>/employee-mappings/: save step 2's whole table."""

    def put(self, request, location_id):
        self.guard(request, location_id)
        self.account(location_id)

        payload = EmployeeMappingsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        onboarding.save_employee_mappings(location_id, payload.validated_data['mappings'])
        return Response(onboarding.onboarding_status(location_id))


class CalendarsView(_HCPView):
    """GET /api/hcp/accounts/<location_id>/calendars/: the round-robin calendars to choose from.

    Only round-robin: estimate and job appointments are assigned across a team, which is the one
    thing the other GHL calendar types do not do.
    """

    def get(self, request, location_id):
        self.guard(request, location_id)
        self.account(location_id)
        try:
            calendars = onboarding.round_robin_calendars(location_id)
        except GHLError:
            return _ghl_failure('calendar list')
        return Response({'calendars': calendars})


class CalendarSettingsView(_HCPView):
    """PUT /api/hcp/accounts/<location_id>/calendar-settings/: save step 3.

    Refuses the save when a mapped user is missing from either calendar's team. That check is
    the point of the endpoint: GHL rejects an appointment for a non-team user one at a time,
    while the lead/estimate/job it belongs to syncs fine - so without it the account looks
    healthy and silently drops appointments.
    """

    def put(self, request, location_id):
        self.guard(request, location_id)
        self.account(location_id)

        payload = CalendarSettingsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            onboarding.save_calendar_settings(
                location_id,
                estimate_calendar_id=data['estimate_calendar_id'],
                job_calendar_id=data['job_calendar_id'],
                default_user_id=data['default_user'],
                estimate_default_user_id=data['estimate_default_user'],
                job_default_user_id=data['job_default_user'],
            )
        except HCPConfigurationError as exc:
            return _configuration_failure(exc)
        except GHLError:
            return _ghl_failure('calendar settings')

        return Response(onboarding.onboarding_status(location_id))
