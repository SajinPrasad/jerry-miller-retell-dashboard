"""The onboarding endpoints: who may call them, what they accept, and what each failure returns.

Every request carries a GHL session blob in `X-GHL-Session`, the same credential the rebilling
admin endpoints use. `apps/ghl/tests/test_sso.py` owns the blob format; this file only cares
that these endpoints demand one and honour its scope.
"""

import httpx
import pytest
import respx
from django.conf import settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.ghl.tests.test_sso import VECTOR_AGENCY, make_agency, seal
from apps.hcp.models import HCPAccount, HCPCalendarSettings, HCPEmployeeMapping
from apps.hcp.services import onboarding
from apps.hcp.tests.conftest import LOCATION_ID
from apps.hcp.tests.test_onboarding import (
    COMPANY,
    ESTIMATE_CALENDAR,
    JOB_CALENDAR,
    FakeGHL,
    both_teams,
    make_mapping,
)

pytestmark = pytest.mark.django_db

COMPANY_URL = f'{settings.HCP_API_BASE_URL}/company'
EMPLOYEES_URL = f'{settings.HCP_API_BASE_URL}/employees'

# A session opened inside the sub-account these tests configure, rather than at agency level.
OWN_LOCATION = seal(
    {
        'userId': 'user-1',
        'companyId': 'comp_demo',
        'role': 'admin',
        'userName': 'Dana Reyes',
        'email': 'dana@coolbreeze.test',
        'activeLocation': LOCATION_ID,
    }
)
# A session opened inside a *different* sub-account.
OTHER_LOCATION = seal(
    {
        'userId': 'user-2',
        'companyId': 'comp_demo',
        'role': 'admin',
        'userName': 'Sam Fox',
        'email': 'sam@elsewhere.test',
        'activeLocation': 'loc-somewhere-else',
    }
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture(autouse=True)
def agency():
    """`context_from_blob` pins every session to the agency that installed the app."""
    return make_agency(company_id='comp_demo')


@pytest.fixture
def fake_ghl(monkeypatch):
    """Point the onboarding service's GHL reads at a fake, per test."""

    def use(service):
        monkeypatch.setattr('apps.hcp.services.onboarding.GHLOAuthService', lambda: service)
        return service

    return use


def url(name, location_id=LOCATION_ID):
    if name == 'connect-account':
        return reverse('hcp:connect-account')
    return reverse(f'hcp:{name}', args=[location_id])


def connect(api, blob=VECTOR_AGENCY, location_id=LOCATION_ID, api_key='hcp-key'):
    return api.post(
        url('connect-account'),
        {'location_id': location_id, 'api_key': api_key},
        format='json',
        HTTP_X_GHL_SESSION=blob,
    )


def put_mappings(api, rows, blob=VECTOR_AGENCY):
    return api.put(
        url('employee-mappings'), {'mappings': rows}, format='json', HTTP_X_GHL_SESSION=blob
    )


def put_calendars(api, blob=VECTOR_AGENCY, **overrides):
    """A calendar save that succeeds, so each test can change one thing about it.

    `default_user` is required, so it is filled in from the location's first mapping unless the
    test overrides it.
    """
    body = {
        'estimate_calendar_id': ESTIMATE_CALENDAR['id'],
        'job_calendar_id': JOB_CALENDAR['id'],
    }
    first = HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).order_by('pk').first()
    if first is not None:
        body['default_user'] = first.pk
    body.update(overrides)
    return api.put(url('calendar-settings'), body, format='json', HTTP_X_GHL_SESSION=blob)


# --- authentication and scope -------------------------------------------------------------


@pytest.mark.parametrize(
    'name,method',
    [
        ('status', 'get'),
        ('employees', 'get'),
        ('calendars', 'get'),
        ('employee-mappings', 'put'),
        ('calendar-settings', 'put'),
    ],
)
def test_every_endpoint_needs_a_session(api, name, method):
    assert getattr(api, method)(url(name)).status_code == 401


def test_connecting_needs_a_session(api):
    response = api.post(
        url('connect-account'),
        {'location_id': LOCATION_ID, 'api_key': 'hcp-key'},
        format='json',
    )

    assert response.status_code == 401
    assert not HCPAccount.objects.exists()


def test_connecting_with_no_session_is_401_before_the_body_is_judged(api):
    """A caller with no session is not told which fields it got wrong - that is a reply only an
    authenticated caller has any business receiving.
    """
    response = api.post(url('connect-account'), {}, format='json')

    assert response.status_code == 401
    assert 'api_key' not in response.json()


def test_a_session_from_another_agency_is_refused(api):
    """The blob's `companyId` is sealed by GHL, so this really does pin it to Jerry's agency."""
    blob = seal({'userId': 'u', 'companyId': 'comp_someone_else', 'activeLocation': LOCATION_ID})

    assert api.get(url('status'), HTTP_X_GHL_SESSION=blob).status_code == 403


def test_a_sub_account_cannot_read_another_sub_accounts_setup(api, account):
    assert api.get(url('status'), HTTP_X_GHL_SESSION=OTHER_LOCATION).status_code == 403


def test_a_sub_account_cannot_connect_another_sub_account(api):
    assert connect(api, blob=OTHER_LOCATION).status_code == 403
    assert not HCPAccount.objects.exists()


def test_a_sub_account_can_read_its_own_setup(api, account):
    response = api.get(url('status'), HTTP_X_GHL_SESSION=OWN_LOCATION)

    assert response.status_code == 200
    assert response.json()['state'] == onboarding.NEEDS_EMPLOYEE_MAPPING


# --- connect ------------------------------------------------------------------------------


@respx.mock
def test_connecting_validates_the_key_and_returns_the_new_status(api):
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))

    response = connect(api)

    assert response.status_code == 201
    assert response.json()['state'] == onboarding.NEEDS_EMPLOYEE_MAPPING
    assert response.json()['account']['company_name'] == 'Cool Breeze HVAC'
    assert HCPAccount.objects.get(location_id=LOCATION_ID).is_active is False


@respx.mock
def test_an_invalid_api_key_is_a_400_not_a_502(api):
    """The one failure a human at the screen can fix unaided, so it must be distinguishable."""
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(401, json={'error': 'nope'}))

    response = connect(api, api_key='wrong-key')

    assert response.status_code == 400
    assert response.json()['code'] == 'hcp_invalid_api_key'
    assert not HCPAccount.objects.exists()


@respx.mock
def test_hcp_being_down_is_a_502(api):
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(500, text='boom'))

    response = connect(api)

    assert response.status_code == 502
    assert response.json()['code'] == 'hcp_unavailable'


@respx.mock
def test_a_rate_limit_is_a_429_carrying_retry_after(api):
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(429, headers={'Retry-After': '45'}, json={})
    )

    response = connect(api)

    assert response.status_code == 429
    assert response['Retry-After'] == '45'


@respx.mock
def test_connecting_one_hcp_company_to_a_second_sub_account_is_a_400(api, account):
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(200, json={**COMPANY, 'id': account.hcp_company_id})
    )

    response = connect(api, location_id='loc-other')

    assert response.status_code == 400
    assert 'already connected' in response.json()['detail']


def test_connecting_without_an_api_key_is_a_400(api):
    response = api.post(
        url('connect-account'),
        {'location_id': LOCATION_ID},
        format='json',
        HTTP_X_GHL_SESSION=VECTOR_AGENCY,
    )

    assert response.status_code == 400
    assert 'api_key' in response.json()


# --- employees and mappings ---------------------------------------------------------------


@respx.mock
def test_the_employees_endpoint_returns_both_directories(api, account, fake_ghl):
    respx.get(EMPLOYEES_URL).mock(
        return_value=httpx.Response(
            200, json={'employees': [{'id': 'emp-1', 'first_name': 'Ana', 'last_name': 'Ruiz'}]}
        )
    )
    fake_ghl(FakeGHL(users=[{'id': 'user-1', 'name': 'Ana R'}]))

    body = api.get(url('employees'), HTTP_X_GHL_SESSION=VECTOR_AGENCY).json()

    assert body['hcp_employees'][0]['name'] == 'Ana Ruiz'
    assert body['ghl_users'][0]['name'] == 'Ana R'
    assert body['mappings'] == []


def test_the_employees_endpoint_404s_before_the_account_is_connected(api, fake_ghl):
    assert api.get(url('employees'), HTTP_X_GHL_SESSION=VECTOR_AGENCY).status_code == 404


@respx.mock
def test_hcp_failing_the_directory_read_is_a_502(api, account, fake_ghl):
    respx.get(EMPLOYEES_URL).mock(return_value=httpx.Response(503, text='maintenance'))
    fake_ghl(FakeGHL())

    assert api.get(url('employees'), HTTP_X_GHL_SESSION=VECTOR_AGENCY).status_code == 502


def test_saving_mappings_returns_the_updated_status(api, account):
    response = put_mappings(
        api, [{'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-1', 'ghl_user_name': 'Ana'}]
    )

    assert response.status_code == 200
    assert response.json()['state'] == onboarding.NEEDS_CALENDAR_SETTINGS
    assert response.json()['employee_mappings'][0]['ghl_user_name'] == 'Ana'
    assert HCPEmployeeMapping.objects.filter(location_id=LOCATION_ID).count() == 1


def test_mapping_the_same_employee_twice_is_a_400(api, account):
    response = put_mappings(
        api,
        [
            {'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-1'},
            {'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-2'},
        ],
    )

    assert response.status_code == 400
    assert 'Housecall Pro employee' in str(response.json())
    assert not HCPEmployeeMapping.objects.exists()


def test_mapping_two_employees_to_one_ghl_user_is_a_400(api, account):
    """Two technicians sharing a GHL user is how their assignments silently merge."""
    response = put_mappings(
        api,
        [
            {'hcp_employee_id': 'emp-1', 'ghl_user_id': 'user-1'},
            {'hcp_employee_id': 'emp-2', 'ghl_user_id': 'user-1'},
        ],
    )

    assert response.status_code == 400
    assert 'GoHighLevel user' in str(response.json())
    assert not HCPEmployeeMapping.objects.exists()


def test_a_mapping_row_missing_its_ghl_user_is_a_400(api, account):
    response = put_mappings(api, [{'hcp_employee_id': 'emp-1'}])

    assert response.status_code == 400


def test_saving_mappings_404s_before_the_account_is_connected(api):
    assert put_mappings(api, []).status_code == 404


# --- calendars ------------------------------------------------------------------------------


def test_the_calendars_endpoint_offers_only_round_robin(api, account, fake_ghl):
    fake_ghl(
        FakeGHL(
            calendars=[
                ESTIMATE_CALENDAR,
                {'id': 'cal-personal', 'name': 'Mine', 'calendarType': 'personal'},
            ]
        )
    )

    body = api.get(url('calendars'), HTTP_X_GHL_SESSION=VECTOR_AGENCY).json()

    assert [c['id'] for c in body['calendars']] == ['cal-est']


def test_saving_calendar_settings_returns_the_updated_status(api, account, fake_ghl):
    make_mapping('emp-1', 'user-1')
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = put_calendars(api)

    assert response.status_code == 200
    assert response.json()['state'] == onboarding.READY_TO_ACTIVATE
    assert response.json()['calendar_settings']['job_calendar_name'] == 'Install'


def test_a_user_missing_from_a_calendar_team_is_a_400_naming_them(api, account, fake_ghl):
    make_mapping('emp-1', 'user-1')
    make_mapping('emp-2', 'user-2')
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = put_calendars(api)

    assert response.status_code == 400
    assert 'User user-2' in response.json()['detail']
    assert response.json()['missing_team_members']['estimate_calendar_id'] == [
        {'ghl_user_id': 'user-2', 'ghl_user_name': 'User user-2'}
    ]
    assert not HCPCalendarSettings.objects.exists()


def test_fallback_users_are_saved(api, account, fake_ghl):
    mapping = make_mapping('emp-1', 'user-1')
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = put_calendars(api, default_user=mapping.pk)

    assert response.status_code == 200
    assert response.json()['calendar_settings']['default_user']['ghl_user_id'] == 'user-1'


def test_naming_the_same_calendar_twice_is_a_400(api, account, fake_ghl):
    make_mapping('emp-1', 'user-1')
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = put_calendars(api, job_calendar_id=ESTIMATE_CALENDAR['id'])

    assert response.status_code == 400
    assert 'two different calendars' in response.json()['detail']


def test_calendar_settings_before_any_mapping_is_a_400(api, account, fake_ghl):
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = put_calendars(api)

    assert response.status_code == 400
    assert 'Map at least one' in response.json()['detail']


def test_calendar_settings_404_before_the_account_is_connected(api, fake_ghl):
    assert put_calendars(api).status_code == 404


def test_a_calendar_settings_body_missing_a_calendar_is_a_400(api, account, fake_ghl):
    make_mapping('emp-1', 'user-1')
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = api.put(
        url('calendar-settings'),
        {'estimate_calendar_id': ESTIMATE_CALENDAR['id']},
        format='json',
        HTTP_X_GHL_SESSION=VECTOR_AGENCY,
    )

    assert response.status_code == 400
    assert 'job_calendar_id' in response.json()


def test_a_calendar_settings_body_with_no_default_user_is_a_400(api, account, fake_ghl):
    make_mapping('emp-1', 'user-1')
    fake_ghl(FakeGHL(teams=both_teams('user-1')))

    response = put_calendars(api, default_user=None)

    assert response.status_code == 400
    assert 'default user' in response.json()['detail']
    assert not HCPCalendarSettings.objects.exists()


# --- status -------------------------------------------------------------------------------


def test_status_of_an_unconnected_sub_account_is_a_state_not_a_404(api):
    response = api.get(url('status', 'loc-untouched'), HTTP_X_GHL_SESSION=VECTOR_AGENCY)

    assert response.status_code == 200
    assert response.json()['state'] == onboarding.NOT_CONNECTED


def test_the_status_response_never_carries_the_api_key(api, account):
    response = api.get(url('status'), HTTP_X_GHL_SESSION=VECTOR_AGENCY)

    assert account.api_key not in response.content.decode()
