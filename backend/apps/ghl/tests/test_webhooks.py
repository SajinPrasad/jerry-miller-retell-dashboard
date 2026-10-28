"""The `LocationCreate` webhook: GHL telling us a new sub-account was created."""

import base64
import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.test import override_settings
from django.urls import reverse

from apps.ghl.models import GHLOAuthToken

from .test_services import BASE, make_agency, mock_installed, mock_location_token

pytestmark = pytest.mark.django_db

URL_NAME = 'ghl:location-created-webhook'

# A keypair this test controls, so it can sign payloads the same way GHL does, without needing
# GHL's real private key (nobody outside GHL has it).
_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY_PEM = (
    _PRIVATE_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
)


def sign(body: bytes) -> str:
    return base64.b64encode(_PRIVATE_KEY.sign(body)).decode()


def post(api, payload: dict, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {'HTTP_X_GHL_SIGNATURE': sign(body) if signature is None else signature}
    return api.post(reverse(URL_NAME), data=body, content_type='application/json', **headers)


@pytest.fixture(autouse=True)
def _use_test_keypair():
    with override_settings(GHL_WEBHOOK_PUBLIC_KEY=_PUBLIC_KEY_PEM):
        yield


@respx.mock
def test_location_create_syncs_the_new_sub_account(client):
    make_agency()
    mock_installed(('loc-new', 'Polar Peak Heating'))
    mock_location_token()

    response = post(client, {'type': 'LocationCreate', 'id': 'loc-new', 'companyId': 'comp1'})

    assert response.status_code == 200
    location = GHLOAuthToken.objects.get(location_id='loc-new')
    assert location.is_active
    assert location.name == 'Polar Peak Heating'


@respx.mock
def test_rejects_a_bad_signature(client):
    make_agency()
    installed = mock_installed(('loc-new', 'Polar Peak Heating'))

    response = post(
        client,
        {'type': 'LocationCreate', 'id': 'loc-new', 'companyId': 'comp1'},
        signature=sign(b'tampered'),
    )

    assert response.status_code == 401
    assert not installed.called
    assert not GHLOAuthToken.objects.filter(location_id='loc-new').exists()


@respx.mock
def test_rejects_a_missing_signature(client):
    installed = mock_installed(('loc-new', 'Polar Peak Heating'))

    response = post(client, {'type': 'LocationCreate', 'id': 'loc-new'}, signature='')

    assert response.status_code == 401
    assert not installed.called


@respx.mock
def test_ignores_other_event_types(client):
    make_agency()
    installed = mock_installed(('loc-new', 'Polar Peak Heating'))

    response = post(client, {'type': 'AppInstall', 'locationId': 'loc-new'})

    assert response.status_code == 200
    assert not installed.called
    assert not GHLOAuthToken.objects.filter(location_id='loc-new').exists()


def test_rejects_a_body_that_is_not_json(client):
    body = b'not json'
    response = client.post(
        reverse(URL_NAME),
        data=body,
        content_type='application/json',
        HTTP_X_GHL_SIGNATURE=sign(body),
    )
    assert response.status_code == 400


@respx.mock
def test_returns_200_even_when_the_sync_fails(client):
    make_agency()
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))

    response = post(client, {'type': 'LocationCreate', 'id': 'loc-new', 'companyId': 'comp1'})

    assert response.status_code == 200
