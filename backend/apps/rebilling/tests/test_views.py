import hashlib
import hmac
import json
import time

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.rebilling.models import RetellCall, UnassignedCall

from .test_services import call_payload, make_client

pytestmark = pytest.mark.django_db

URL = '/api/rebilling/webhooks/retell/'


def sign(body: bytes, key: str = settings.RETELL_API_KEY, timestamp: int | None = None) -> str:
    """The header Retell sends: v=<epoch ms>,d=<HMAC-SHA256 of body + timestamp>.

    Written out rather than taken from the SDK, so the test signs independently of the code that
    verifies. Docs: https://docs.retellai.com/features/secure-webhook
    """
    if timestamp is None:
        timestamp = int(time.time() * 1000)
    digest = hmac.new(
        key.encode(), body.decode().encode() + str(timestamp).encode(), hashlib.sha256
    ).hexdigest()
    return f'v={timestamp},d={digest}'


@pytest.fixture
def api():
    return APIClient()


def post(api, payload, signature=None, **header_overrides):
    body = json.dumps(payload).encode()
    headers = {'HTTP_X_RETELL_SIGNATURE': sign(body) if signature is None else signature}
    headers.update(header_overrides)
    return api.post(URL, data=body, content_type='application/json', **headers)


def ended(**overrides):
    return {'event': 'call_ended', 'call': call_payload(**overrides)}


def test_stores_a_signed_call_ended(api):
    make_client()

    response = post(api, ended())

    assert response.status_code == 204
    assert response.content == b''
    assert RetellCall.objects.get(call_id='call1').agent_id == 'agent1'


def test_rejects_a_bad_signature(api):
    make_client()

    response = post(api, ended(), signature=sign(json.dumps(ended()).encode(), key='wrong-key'))

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_a_missing_signature(api):
    response = post(api, ended(), signature='')

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_a_signature_over_a_body_that_was_changed(api):
    make_client()
    signature = sign(json.dumps(ended()).encode())

    response = post(api, ended(duration_ms=999_999), signature=signature)

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_an_expired_signature(api):
    """Retell's window is 5 minutes, which is what stops a captured call being replayed."""
    body = json.dumps(ended()).encode()
    stale = int(time.time() * 1000) - 6 * 60 * 1000

    response = post(api, ended(), signature=sign(body, timestamp=stale))

    assert response.status_code == 401


def test_ignores_other_events(api):
    make_client()

    response = post(api, {'event': 'call_started', 'call': call_payload()})

    assert response.status_code == 204
    assert not RetellCall.objects.exists()
    assert not UnassignedCall.objects.exists()


def test_retried_webhook_is_stored_once(api):
    make_client()

    assert post(api, ended()).status_code == 204
    assert post(api, ended()).status_code == 204

    assert RetellCall.objects.count() == 1


def test_unmatched_call_is_stored_as_unassigned(api):
    response = post(api, ended())

    assert response.status_code == 204
    assert UnassignedCall.objects.filter(call_id='call1').exists()


def test_rejects_a_body_that_is_not_json(api):
    body = b'not json'
    response = api.post(
        URL,
        data=body,
        content_type='application/json',
        HTTP_X_RETELL_SIGNATURE=sign(body),
    )

    assert response.status_code == 400
