import json

import httpx
import pytest
import respx
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
    assert response.json()['locations'][0]['location_id'] == 'loc1'


@respx.mock
def test_sync_ghl_failure(api):
    make_agency()
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))
    assert api.post('/api/ghl/onboarding/sync/').status_code == 502
