from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from django.conf import settings
from django.db import connection as db_connection
from django.utils import timezone

from apps.ghl.exceptions import GHLError, GHLNotConnectedError
from apps.ghl.models import GHLOAuthToken
from apps.ghl.services import GHLOAuthService

BASE = settings.GHL_API_BASE_URL


def token_response(**overrides):
    body = {
        'access_token': 'agency-access',
        'refresh_token': 'agency-refresh',
        'expires_in': 86399,
        'userType': 'Company',
        'companyId': 'comp1',
    }
    body.update(overrides)
    return body


def make_agency(**overrides):
    fields = {
        'company_id': 'comp1',
        'is_agency': True,
        'access_token': 'agency-access',
        'refresh_token': 'agency-refresh',
        'expires_at': timezone.now() + timedelta(hours=12),
    }
    fields.update(overrides)
    return GHLOAuthToken.objects.create(**fields)


def make_location(location_id='loc1', **overrides):
    fields = {
        'company_id': 'comp1',
        'location_id': location_id,
        'access_token': f'{location_id}-access',
        'expires_at': timezone.now() + timedelta(hours=12),
    }
    fields.update(overrides)
    return GHLOAuthToken.objects.create(**fields)


def mock_installed(*locations):
    items = [{'_id': location_id, 'name': name} for location_id, name in locations]
    return respx.get(f'{BASE}/oauth/installed-locations').mock(
        return_value=httpx.Response(200, json={'items': items})
    )


def _minted_token(request):
    location_id = parse_qs(request.content.decode())['locationId'][0]
    return httpx.Response(200, json={'access_token': f'{location_id}-minted', 'expires_in': 86399})


def mock_location_token():
    return respx.post(f'{BASE}/oauth/locationToken').mock(side_effect=_minted_token)


@pytest.fixture
def service():
    return GHLOAuthService(client=httpx.Client(base_url=BASE))


def test_install_url(service):
    url = urlparse(service.install_url())
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    assert f'{url.scheme}://{url.netloc}{url.path}' == (
        'https://marketplace.example.com/oauth/chooselocation'
    )
    assert query['client_id'] == 'app123-abc'
    assert query['redirect_uri'] == settings.GHL_REDIRECT_URI
    assert query['response_type'] == 'code'


@pytest.mark.django_db
@respx.mock
def test_exchange_code_stores_encrypted_agency_token(service):
    route = respx.post(f'{BASE}/oauth/token').mock(
        return_value=httpx.Response(200, json=token_response())
    )

    agency = service.exchange_code('the-code')

    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent['grant_type'] == ['authorization_code']
    assert sent['user_type'] == ['Company']
    assert agency.is_agency and agency.company_id == 'comp1' and agency.location_id == 'agency-loc'
    assert agency.refresh_token == 'agency-refresh'
    with db_connection.cursor() as cursor:
        cursor.execute('SELECT access_token FROM ghl_oauth_token')
        assert cursor.fetchone()[0] != 'agency-access'


@pytest.mark.django_db
@respx.mock
def test_reinstall_updates_the_single_agency_row(service):
    make_agency(is_active=False)
    respx.post(f'{BASE}/oauth/token').mock(
        return_value=httpx.Response(200, json=token_response(access_token='new'))
    )

    service.exchange_code('c')

    agency = GHLOAuthToken.objects.get(is_agency=True)
    assert agency.is_active and agency.access_token == 'new'


@pytest.mark.django_db
@respx.mock
@pytest.mark.parametrize('overrides', [{'userType': 'Location', 'locationId': 'loc1'}])
def test_exchange_code_rejects_location_install(service, overrides):
    respx.post(f'{BASE}/oauth/token').mock(
        return_value=httpx.Response(200, json=token_response(**overrides))
    )
    with pytest.raises(GHLError):
        service.exchange_code('c')
    assert not GHLOAuthToken.objects.exists()


@pytest.mark.django_db
@respx.mock
def test_exchange_code_http_error(service):
    respx.post(f'{BASE}/oauth/token').mock(return_value=httpx.Response(400))
    with pytest.raises(GHLError):
        service.exchange_code('c')


@pytest.mark.django_db
def test_get_agency_requires_active_install(service):
    make_agency(is_active=False)
    with pytest.raises(GHLNotConnectedError):
        service.get_agency()


@pytest.mark.django_db
@respx.mock
def test_agency_token_is_refreshed_when_expiring(service):
    agency = make_agency(expires_at=timezone.now() + timedelta(seconds=30))
    route = respx.post(f'{BASE}/oauth/token').mock(
        return_value=httpx.Response(
            200, json=token_response(access_token='new-access', refresh_token='new-refresh')
        )
    )

    assert service.get_agency_token(agency) == 'new-access'

    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent['refresh_token'] == ['agency-refresh']
    agency.refresh_from_db()
    assert agency.refresh_token == 'new-refresh'
    assert not agency.needs_refresh


@pytest.mark.django_db
@respx.mock
def test_fresh_agency_token_is_reused(service):
    route = respx.post(f'{BASE}/oauth/token')
    assert service.get_agency_token(make_agency()) == 'agency-access'
    assert not route.called


@pytest.mark.django_db
@respx.mock
def test_sync_creates_location_rows_with_tokens(service):
    agency = make_agency()
    installed = mock_installed(('loc1', 'Cool Breeze HVAC'), ('loc2', 'Branwynn'))
    minted = mock_location_token()

    service.sync_locations(agency)

    params = installed.calls.last.request.url.params
    assert params['companyId'] == 'comp1' and params['appId'] == 'app123'
    assert installed.calls.last.request.headers['Version'] == 'v3'
    assert minted.calls.last.request.headers['Authorization'] == 'Bearer agency-access'
    loc1 = GHLOAuthToken.objects.get(location_id='loc1')
    assert not loc1.is_agency and loc1.is_active
    assert loc1.name == 'Cool Breeze HVAC'
    assert loc1.access_token == 'loc1-minted'
    assert loc1.refresh_token == ''
    assert GHLOAuthToken.objects.filter(is_agency=False).count() == 2


@pytest.mark.django_db
@respx.mock
def test_sync_follows_pagination(service):
    agency = make_agency()
    route = respx.get(f'{BASE}/oauth/installed-locations').mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    'items': [{'_id': 'loc1', 'name': 'A'}],
                    'pagination': {'hasNextPage': True, 'nextPageToken': 'p2'},
                },
            ),
            httpx.Response(200, json={'items': [{'_id': 'loc2', 'name': 'B'}]}),
        ]
    )
    mock_location_token()

    service.sync_locations(agency)

    assert route.calls[1].request.url.params['pageToken'] == 'p2'
    assert GHLOAuthToken.objects.filter(is_agency=False).count() == 2


@pytest.mark.django_db
@respx.mock
def test_sync_keeps_fresh_tokens_and_deactivates_uninstalled(service):
    agency = make_agency()
    make_location('loc1', name='Old name')
    make_location('gone')
    mock_installed(('loc1', 'Cool Breeze HVAC'))
    minted = mock_location_token()

    service.sync_locations(agency)

    assert not minted.called
    loc1 = GHLOAuthToken.objects.get(location_id='loc1')
    assert loc1.access_token == 'loc1-access' and loc1.name == 'Cool Breeze HVAC'
    assert not GHLOAuthToken.objects.get(location_id='gone').is_active
    assert GHLOAuthToken.objects.get(is_agency=True).is_active


@pytest.mark.django_db
@respx.mock
def test_sync_skips_a_location_whose_token_fails(service):
    agency = make_agency()
    mock_installed(('loc1', 'A'), ('loc2', 'B'))
    respx.post(f'{BASE}/oauth/locationToken').mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={'access_token': 'ok'})]
    )

    service.sync_locations(agency)

    locations = GHLOAuthToken.objects.filter(is_agency=False)
    assert list(locations.values_list('location_id', flat=True)) == ['loc2']


@pytest.mark.django_db
@respx.mock
def test_location_token_is_reminted_when_expiring(service):
    make_agency()
    make_location('loc1', expires_at=timezone.now())
    mock_location_token()

    assert service.get_location_token('loc1') == 'loc1-minted'
    assert not GHLOAuthToken.objects.get(location_id='loc1').needs_refresh


@pytest.mark.django_db
@respx.mock
def test_fresh_location_token_is_reused(service):
    make_location('loc1')
    route = mock_location_token()
    assert service.get_location_token('loc1') == 'loc1-access'
    assert not route.called
