"""The per-client Retell webhook: `POST /api/rebilling/webhooks/retell/<location_id>/`.

The client id is in the path because it has to be. Retell signs a workspace's webhooks with
that workspace's own API key, so nothing can be verified until we know whose key to verify it
with - and the key itself must never be the thing in the URL.
"""

import hashlib
import hmac
import json
import time

import pytest
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.rebilling.models import RetellCall, RetellWorkspace

from .test_services import WORKSPACE_KEY, call_payload, make_client, make_workspace

pytestmark = pytest.mark.django_db


def url(location_id='loc1'):
    return reverse('rebilling:retell-client-webhook', args=[location_id])


def sign(body: bytes, key: str = WORKSPACE_KEY, timestamp: int | None = None) -> str:
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


def post(api, payload, location_id='loc1', signature=None, **header_overrides):
    body = json.dumps(payload).encode()
    headers = {'HTTP_X_RETELL_SIGNATURE': sign(body) if signature is None else signature}
    headers.update(header_overrides)
    return api.post(url(location_id), data=body, content_type='application/json', **headers)


def ended(**overrides):
    return {'event': 'call_ended', 'call': call_payload(**overrides)}


def test_stores_a_signed_call_ended(api):
    workspace = make_workspace()

    response = post(api, ended())

    assert response.status_code == 204
    assert response.content == b''
    stored = RetellCall.objects.get(call_id='call1')
    assert stored.client == workspace.client
    assert stored.agent_id == 'agent1'


def test_the_url_decides_whose_call_it_is(api):
    """Two clients, and nothing in the payload says which: only the path does."""
    make_workspace(make_client('loc1', client_name='Cool Breeze'))
    other = make_workspace(
        make_client('loc2', client_name='Branwynn', retell_agent_ids=['agent1']),
        api_key='key_workspace_branwynn',
    )

    body = json.dumps(ended()).encode()
    response = api.post(
        url('loc2'),
        data=body,
        content_type='application/json',
        HTTP_X_RETELL_SIGNATURE=sign(body, key='key_workspace_branwynn'),
    )

    assert response.status_code == 204
    assert RetellCall.objects.get(call_id='call1').client == other.client


def test_the_signature_is_checked_against_this_client_s_own_key(api):
    """Another workspace's perfectly valid key is not this workspace's signature."""
    make_workspace()

    body = json.dumps(ended()).encode()
    response = api.post(
        url('loc1'),
        data=body,
        content_type='application/json',
        HTTP_X_RETELL_SIGNATURE=sign(body, key='key_workspace_branwynn'),
    )

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_a_bad_signature(api):
    make_workspace()

    response = post(api, ended(), signature=sign(json.dumps(ended()).encode(), key='wrong-key'))

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_a_missing_signature(api):
    make_workspace()

    response = post(api, ended(), signature='')

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_a_signature_over_a_body_that_was_changed(api):
    make_workspace()
    signature = sign(json.dumps(ended()).encode())

    response = post(api, ended(duration_ms=999_999), signature=signature)

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_rejects_an_expired_signature(api):
    """Retell's window is 5 minutes, which is what stops a captured call being replayed."""
    make_workspace()
    body = json.dumps(ended()).encode()
    stale = int(time.time() * 1000) - 6 * 60 * 1000

    response = post(api, ended(), signature=sign(body, timestamp=stale))

    assert response.status_code == 401


def test_a_client_with_no_workspace_is_refused(api):
    """No key, nothing to verify against. Refused rather than stored unverified."""
    make_client()

    response = post(api, ended())

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_an_unknown_client_id_is_refused_without_a_lookup_of_the_body(api):
    make_workspace()

    response = post(api, ended(), location_id='nobody')

    assert response.status_code == 401
    assert not RetellCall.objects.exists()


def test_ignores_other_events_but_still_records_the_webhook(api):
    """Any verified event proves the badged key is the one we hold - see the badge trap."""
    workspace = make_workspace()

    response = post(api, {'event': 'call_started', 'call': call_payload()})

    assert response.status_code == 204
    assert not RetellCall.objects.exists()
    workspace.refresh_from_db()
    assert workspace.last_webhook_at is not None


def test_a_rejected_webhook_is_not_recorded_as_one(api):
    workspace = make_workspace()

    post(api, ended(), signature='')

    workspace.refresh_from_db()
    assert workspace.last_webhook_at is None


def test_retried_webhook_is_stored_once(api):
    make_workspace()

    assert post(api, ended()).status_code == 204
    assert post(api, ended()).status_code == 204

    assert RetellCall.objects.count() == 1


def test_an_unknown_agent_is_stored_against_this_client_anyway(api):
    """Never refused: the workspace says whose it is, our agent list is only a cache."""
    workspace = make_workspace()

    response = post(api, ended(agent_id='agent_brand_new'))

    assert response.status_code == 204
    assert RetellCall.objects.get(call_id='call1').client == workspace.client
    workspace.client.refresh_from_db()
    assert 'agent_brand_new' in workspace.client.retell_agent_ids


def test_rejects_a_body_that_is_not_json(api):
    make_workspace()
    body = b'not json'

    response = api.post(
        url(),
        data=body,
        content_type='application/json',
        HTTP_X_RETELL_SIGNATURE=sign(body),
    )

    assert response.status_code == 400


def test_the_stored_key_is_encrypted_at_rest():
    """The column holds Fernet ciphertext; only Python ever sees the key itself.

    Read through the cursor rather than the ORM, which would decrypt it on the way out and
    pass whatever was in the column.
    """
    make_workspace()

    with connection.cursor() as cursor:
        cursor.execute('SELECT api_key FROM rebilling_retell_workspace')
        stored = cursor.fetchone()[0]

    assert WORKSPACE_KEY not in stored
    assert RetellWorkspace.objects.get().api_key == WORKSPACE_KEY
