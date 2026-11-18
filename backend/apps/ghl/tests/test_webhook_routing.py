"""The one GHL webhook URL routes by the event's own type, and replay runs each route again.

`LocationCreate` goes to the location sync; the Phase 4 events go to their `apps.hcp` reverse-sync
task; anything else is recorded as ignored. Both routes record under `WebhookEvent.GHL`, so replay
must choose the route by the payload, the same way the view does. The earlier URLs are checked to
reach the same view.
"""

import base64
import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.test import override_settings
from django.urls import reverse

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.audit.replay import replay
from apps.ghl.models import GHLOAuthToken
from apps.ghl.tests.test_services import BASE, make_agency, mock_installed, mock_location_token
from apps.hcp import reverse_tasks
from apps.hcp.tests import ghl_fixtures

pytestmark = pytest.mark.django_db

_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY_PEM = (
    _PRIVATE_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
)


def sign(body: bytes) -> str:
    return base64.b64encode(_PRIVATE_KEY.sign(body)).decode()


@pytest.fixture(autouse=True)
def _use_test_keypair():
    with override_settings(GHL_WEBHOOK_PUBLIC_KEY=_PUBLIC_KEY_PEM):
        yield


def post_to(client, url_name, payload):
    body = json.dumps(payload).encode()
    return client.post(
        reverse(url_name),
        data=body,
        content_type='application/json',
        HTTP_X_GHL_SIGNATURE=sign(body),
    )


def location_create(location_id='loc-new'):
    return {'type': 'LocationCreate', 'id': location_id, 'companyId': 'comp1'}


class RecordingTask:
    """Stands in for a Phase 4 Celery task: records a queued call and a direct (replay) call."""

    def __init__(self, calls, outcome='created'):
        self.calls = calls
        self.outcome = outcome

    def delay(self, payload, webhook_event_id):
        self.calls.append(('delay', payload, webhook_event_id))

    def __call__(self, payload, webhook_event_id=None):
        self.calls.append(('call', payload, webhook_event_id))
        return self.outcome


@pytest.fixture
def contact_task(monkeypatch):
    calls = []
    monkeypatch.setattr(reverse_tasks, 'sync_ghl_contact_create', RecordingTask(calls))
    return calls


# --- one URL, routed by the event's own type ------------------------------------------------


@respx.mock
def test_the_canonical_url_takes_a_location_create(client, contact_task):
    make_agency()
    mock_installed(('loc-new', 'Polar Peak Heating'))
    mock_location_token()

    response = post_to(client, 'ghl:webhooks', location_create())

    assert response.status_code == 200
    assert GHLOAuthToken.objects.get(location_id='loc-new').is_active
    assert contact_task == []
    assert WebhookEvent.objects.get().status == WebhookEvent.PROCESSED


@pytest.mark.parametrize(
    'url_name', ['ghl:webhooks', 'ghl:location-created-webhook', 'hcp:ghl-webhook']
)
def test_every_url_reaches_the_same_view_and_queues_the_same_task(client, contact_task, url_name):
    response = post_to(client, url_name, ghl_fixtures.contact_event('ContactCreate'))

    assert response.status_code == 200
    assert [kind for kind, *_ in contact_task] == ['delay']


def test_a_phase_4_event_is_recorded_as_ghl_and_queued_to_its_task(client, contact_task):
    payload = ghl_fixtures.contact_event('ContactCreate')

    response = post_to(client, 'ghl:webhooks', payload)

    assert response.status_code == 200
    event = WebhookEvent.objects.get()
    assert event.source == WebhookEvent.GHL
    assert event.event_type == 'ContactCreate'
    assert contact_task == [('delay', payload, event.pk)]


@respx.mock
def test_a_location_create_sync_failure_is_kept_as_failed_and_answered_200(client):
    make_agency()
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))

    response = post_to(client, 'ghl:webhooks', location_create())

    assert response.status_code == 200
    assert WebhookEvent.objects.get().status == WebhookEvent.FAILED


def test_an_event_nothing_handles_is_recorded_ignored(client, contact_task):
    response = post_to(client, 'ghl:webhooks', {'type': 'OpportunityCreate'})

    assert response.status_code == 200
    assert WebhookEvent.objects.get().status == WebhookEvent.IGNORED
    assert contact_task == []


# --- replay: each route runs again from the stored payload ----------------------------------


@respx.mock
def test_replaying_a_location_create_runs_the_sync_again():
    make_agency()
    mock_installed(('loc-new', 'Polar Peak Heating'))
    mock_location_token()
    event = recording.webhook_received(
        WebhookEvent.GHL, location_create(), event_type='LocationCreate', event_id='wh-1'
    )

    assert replay(event) == 'synced'

    assert GHLOAuthToken.objects.get(location_id='loc-new').is_active
    event.refresh_from_db()
    assert event.status == WebhookEvent.PROCESSED


def test_replaying_a_phase_4_event_runs_its_task_here(contact_task):
    payload = ghl_fixtures.contact_event('ContactCreate')
    event = recording.webhook_received(
        WebhookEvent.GHL, payload, event_type='ContactCreate', event_id='wh-2'
    )

    assert replay(event) == 'created'

    assert contact_task == [('call', payload, None)]
    event.refresh_from_db()
    assert event.status == WebhookEvent.PROCESSED


def test_replaying_an_event_nothing_handles_leaves_it_ignored(contact_task):
    event = recording.webhook_received(
        WebhookEvent.GHL, {'type': 'OpportunityCreate'}, event_type='OpportunityCreate'
    )

    assert replay(event) == 'ignored'

    assert contact_task == []
    event.refresh_from_db()
    assert event.status == WebhookEvent.IGNORED


def test_a_phase_4_replay_that_raises_marks_the_delivery_failed(monkeypatch):
    def boom(payload, webhook_event_id=None):
        raise RuntimeError('GHL is down')

    monkeypatch.setattr(reverse_tasks, 'sync_ghl_contact_create', boom)
    event = recording.webhook_received(
        WebhookEvent.GHL, ghl_fixtures.contact_event('ContactCreate'), event_type='ContactCreate'
    )

    with pytest.raises(RuntimeError):
        replay(event)

    event.refresh_from_db()
    assert event.status == WebhookEvent.FAILED
