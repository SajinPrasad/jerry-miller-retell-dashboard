import json

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings
from rest_framework.test import APIClient

from apps.ghl.models import GHLOAuthToken

from .test_services import (
    BASE,
    make_agency,
    make_location,
    mock_installed,
    mock_location_token,
    token_response,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


def test_onboarding_when_not_connected(api):
    body = api.get('/api/ghl/onboarding/').json()
    assert body['agency'] is None
    assert body['locations'] == []
    assert body['install_url'].startswith('https://marketplace.example.com/oauth/chooselocation')


def test_onboarding_lists_agency_and_locations_without_tokens(api):
    make_agency()
    make_location('loc1', name='Cool Breeze HVAC')

    body = api.get('/api/ghl/onboarding/').json()

    assert body['agency']['company_id'] == 'comp1'
    assert [loc['name'] for loc in body['locations']] == ['Cool Breeze HVAC']
    assert 'access' not in json.dumps(body)


@respx.mock
def test_callback_connects_and_syncs(api):
    respx.post(f'{BASE}/oauth/token').mock(return_value=httpx.Response(200, json=token_response()))
    mock_installed(('loc1', 'Cool Breeze HVAC'))
    mock_location_token()

    response = api.get('/api/oauth/callback/', {'code': 'abc'})

    assert response.status_code == 302
    assert response['Location'] == 'http://frontend.test/onboarding?status=connected'
    assert GHLOAuthToken.objects.get(is_agency=True).company_id == 'comp1'
    assert GHLOAuthToken.objects.get(location_id='loc1').is_active


def test_callback_without_code(api):
    response = api.get('/api/oauth/callback/', {'error': 'access_denied'})
    assert response['Location'] == 'http://frontend.test/onboarding?error=install_cancelled'


@respx.mock
def test_callback_connects_even_when_the_initial_sync_fails(api):
    respx.post(f'{BASE}/oauth/token').mock(return_value=httpx.Response(200, json=token_response()))
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))

    response = api.get('/api/oauth/callback/', {'code': 'abc'})

    assert response['Location'] == 'http://frontend.test/onboarding?status=connected'
    assert GHLOAuthToken.objects.get(is_agency=True).company_id == 'comp1'


@respx.mock
def test_callback_failure(api):
    respx.post(f'{BASE}/oauth/token').mock(
        return_value=httpx.Response(200, json=token_response(userType='Location'))
    )
    response = api.get('/api/oauth/callback/', {'code': 'abc'})
    assert response['Location'] == 'http://frontend.test/onboarding?error=install_failed'
    assert not GHLOAuthToken.objects.exists()


def test_sync_when_not_connected(api):
    assert api.post('/api/ghl/onboarding/sync/').status_code == 409


@respx.mock
def test_sync_returns_updated_payload(api):
    make_agency()
    mock_installed(('loc1', 'Cool Breeze HVAC'))
    mock_location_token()

    response = api.post('/api/ghl/onboarding/sync/')

    assert response.status_code == 200
    # The agency's own main location is synced alongside the sub-accounts, so "Refresh clients"
    # puts it in the Clients table ready to be mapped and billed like any other client.
    returned = {row['location_id'] for row in response.json()['locations']}
    assert returned == {'loc1', settings.AGENCY_LOCATION_ID}


@respx.mock
def test_sync_ghl_failure(api):
    make_agency()
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))
    assert api.post('/api/ghl/onboarding/sync/').status_code == 502


# --- SaaS client count (external website) -----------------------------------------------


SAAS_CLIENT_COUNT_URL = '/api/ghl/saas-client-count/'


def _with_custom_value_id(custom_value_id='cv_saas_count'):
    return override_settings(GHL_NUMBER_OF_SAAS_CLIENTS_CUSTOM_VALUE_ID=custom_value_id)


def test_saas_client_count_rejects_a_location_id_that_is_not_the_agency(api):
    with _with_custom_value_id():
        response = api.get(SAAS_CLIENT_COUNT_URL, {'location_id': 'some-other-location'})

    assert response.status_code == 403


def test_saas_client_count_rejects_a_missing_location_id(api):
    with _with_custom_value_id():
        response = api.get(SAAS_CLIENT_COUNT_URL)

    assert response.status_code == 403


def test_saas_client_count_is_unavailable_when_the_custom_value_id_is_not_configured(api):
    # The default in settings/base.py when GHL_NUMBER_OF_SAAS_CLIENTS_CUSTOM_VALUE_ID is unset.
    with override_settings(GHL_NUMBER_OF_SAAS_CLIENTS_CUSTOM_VALUE_ID=''):
        response = api.get(SAAS_CLIENT_COUNT_URL, {'location_id': settings.AGENCY_LOCATION_ID})

    assert response.status_code == 503


@respx.mock
def test_saas_client_count_reads_the_configured_value(api):
    make_location(settings.AGENCY_LOCATION_ID)
    respx.get(f'{BASE}/locations/{settings.AGENCY_LOCATION_ID}/customValues/cv_saas_count').mock(
        return_value=httpx.Response(
            200, json={'customValue': {'id': 'cv_saas_count', 'value': '42'}}
        )
    )

    with _with_custom_value_id():
        response = api.get(SAAS_CLIENT_COUNT_URL, {'location_id': settings.AGENCY_LOCATION_ID})

    assert response.status_code == 200
    assert response.json() == {'value': '42'}


@respx.mock
def test_saas_client_count_on_a_ghl_failure_is_a_502(api):
    make_location(settings.AGENCY_LOCATION_ID)
    respx.get(f'{BASE}/locations/{settings.AGENCY_LOCATION_ID}/customValues/cv_saas_count').mock(
        return_value=httpx.Response(500)
    )

    with _with_custom_value_id():
        response = api.get(SAAS_CLIENT_COUNT_URL, {'location_id': settings.AGENCY_LOCATION_ID})

    assert response.status_code == 502


def test_saas_client_count_for_an_agency_with_no_token_is_a_502(api):
    """The agency's main location is synced like any other client (`_add_main_location`); if it
    somehow has no token yet, that is GHL being unreachable, not a wrong caller.
    """
    with _with_custom_value_id():
        response = api.get(SAAS_CLIENT_COUNT_URL, {'location_id': settings.AGENCY_LOCATION_ID})

    assert response.status_code == 502
