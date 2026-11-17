"""The inbound GHL webhook for Phase 4: what it records, what it queues, and what it refuses.

Signed the same way `apps/ghl/tests/test_webhooks.py` signs `LocationCreate` deliveries - a
keypair this test controls, with `GHL_WEBHOOK_PUBLIC_KEY` overridden to match, rather than
GHL's real private key (nobody outside GHL has it). The tasks are stubbed at the dispatch table,
so these are about the endpoint's own job - signature checking, recording, dispatch - not about
what the handlers then do; those have their own tests in `test_reverse_sync.py`.
"""

import json

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import WebhookEvent
from apps.ghl.tests.test_webhooks import _PUBLIC_KEY_PEM, sign
from apps.hcp import constants, ghl_webhooks
from apps.hcp.tests import ghl_fixtures

pytestmark = pytest.mark.django_db

URL = reverse('hcp:ghl-webhook')


@pytest.fixture(autouse=True)
def _use_test_keypair():
    with override_settings(GHL_WEBHOOK_PUBLIC_KEY=_PUBLIC_KEY_PEM):
        yield


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def queued(monkeypatch):
    """Record what the view queued instead of queueing it."""
    calls = []

    class FakeTask:
        def __init__(self, name):
            self.name = name

        def delay(self, payload, webhook_event_id):
            calls.append((self.name, payload, webhook_event_id))

    fake = {
        'contact_create': FakeTask('contact_create'),
        'contact_update': FakeTask('contact_update'),
        'appointment': FakeTask('appointment'),
        'lead': FakeTask('lead'),
        'estimate': FakeTask('estimate'),
        'job': FakeTask('job'),
        'line_item': FakeTask('line_item'),
        'delete_line_item': FakeTask('delete_line_item'),
    }
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_contact_create', fake['contact_create'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_contact_update', fake['contact_update'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_appointment', fake['appointment'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_lead', fake['lead'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_estimate', fake['estimate'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_job', fake['job'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'sync_ghl_job_line_item', fake['line_item'])
    monkeypatch.setattr(ghl_webhooks.tasks, 'delete_ghl_job_line_item', fake['delete_line_item'])
    monkeypatch.setattr(
        ghl_webhooks,
        'RECORD_DISPATCH',
        {
            (constants.LEADS_OBJECT, constants.GHL_RECORD_CREATE): fake['lead'],
            (constants.ESTIMATES_OBJECT, constants.GHL_RECORD_CREATE): fake['estimate'],
            (constants.JOBS_OBJECT, constants.GHL_RECORD_CREATE): fake['job'],
            (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_CREATE): fake['line_item'],
            (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_UPDATE): fake['line_item'],
            (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_DELETE): fake[
                'delete_line_item'
            ],
        },
    )
    return calls


def post(api, payload, signature=None):
    body = json.dumps(payload).encode()
    headers = {'HTTP_X_GHL_SIGNATURE': sign(body) if signature is None else signature}
    return api.post(URL, data=body, content_type='application/json', **headers)


# --- signature and malformed bodies ------------------------------------------------------


def test_a_bad_signature_is_rejected(api, queued):
    response = post(api, ghl_fixtures.contact_event(), signature='bm90LWEtc2lnbmF0dXJl')

    assert response.status_code == 401
    assert WebhookEvent.objects.get().status == WebhookEvent.REJECTED
    assert queued == []


def test_a_body_that_is_not_json_is_rejected(api):
    body = b'not json'
    response = api.post(
        URL, data=body, content_type='application/json', HTTP_X_GHL_SIGNATURE=sign(body)
    )

    assert response.status_code == 400


def test_a_delivery_missing_the_event_type_is_rejected(api, queued):
    payload = ghl_fixtures.contact_event()
    del payload['type']

    response = post(api, payload)

    assert response.status_code == 400
    assert queued == []


def test_a_delivery_missing_the_location_id_is_rejected(api, queued):
    payload = ghl_fixtures.contact_event()
    del payload['locationId']

    response = post(api, payload)

    assert response.status_code == 400


# --- recording and dispatch ---------------------------------------------------------------


def test_a_delivery_is_recorded_before_it_is_queued(api, queued):
    post(api, ghl_fixtures.contact_event())

    event = WebhookEvent.objects.get()
    assert event.source == WebhookEvent.GHL
    assert event.event_type == 'ContactCreate'
    assert event.payload['id'] == 'ghl-contact-1'


def test_the_stored_delivery_is_the_one_handed_to_the_task(api, queued):
    post(api, ghl_fixtures.contact_event())

    event = WebhookEvent.objects.get()
    assert queued[0][2] == event.pk


@pytest.mark.parametrize(
    'payload,expected',
    [
        (ghl_fixtures.contact_event('ContactCreate'), 'contact_create'),
        (ghl_fixtures.contact_event('ContactUpdate'), 'contact_update'),
        (ghl_fixtures.appointment_event('AppointmentUpdate'), 'appointment'),
        (ghl_fixtures.appointment_event('AppointmentDelete'), 'appointment'),
        (
            ghl_fixtures.record_event('RecordCreate', object_key=constants.LEADS_OBJECT),
            'lead',
        ),
        (
            ghl_fixtures.record_event('RecordCreate', object_key=constants.ESTIMATES_OBJECT),
            'estimate',
        ),
        (ghl_fixtures.record_event('RecordCreate', object_key=constants.JOBS_OBJECT), 'job'),
        (
            ghl_fixtures.record_event('RecordCreate', object_key=constants.JOB_LINE_ITEMS_OBJECT),
            'line_item',
        ),
        (
            ghl_fixtures.record_event('RecordUpdate', object_key=constants.JOB_LINE_ITEMS_OBJECT),
            'line_item',
        ),
        (
            ghl_fixtures.record_event('RecordDelete', object_key=constants.JOB_LINE_ITEMS_OBJECT),
            'delete_line_item',
        ),
    ],
)
def test_each_event_reaches_its_own_task(api, queued, payload, expected):
    response = post(api, payload)

    assert response.status_code == 200
    assert [name for name, _, _ in queued] == [expected]


def test_a_record_update_for_a_create_only_object_is_unhandled(api, queued):
    """Leads/Estimates/Jobs never dispatch an update - the asymmetry itself, not a per-event
    check, is the loop guard (see `reverse_sync.py`'s module docstring)."""
    response = post(
        api, ghl_fixtures.record_event('RecordUpdate', object_key=constants.LEADS_OBJECT)
    )

    assert response.status_code == 200
    assert WebhookEvent.objects.get().status == WebhookEvent.IGNORED
    assert queued == []


def test_an_estimate_appointment_delete_still_reaches_the_shared_appointment_task(api, queued):
    """Routing between estimate and job happens inside `GHLToHCPSync`, by the
    `AppointmentIntegration` row's own `appointment_type` - not at the webhook/dispatch layer."""
    response = post(api, ghl_fixtures.appointment_event('AppointmentDelete'))

    assert response.status_code == 200
    assert [name for name, _, _ in queued] == ['appointment']


def test_an_unhandled_event_type_is_recorded_ignored_and_still_answered_200(api, queued):
    response = post(api, ghl_fixtures.envelope('OpportunityCreate'))

    assert response.status_code == 200
    assert WebhookEvent.objects.get().status == WebhookEvent.IGNORED
    assert queued == []


# --- the dispatch table itself --------------------------------------------------------------


def test_task_for_resolves_every_handled_combination():
    assert ghl_webhooks.task_for(constants.GHL_CONTACT_CREATE, '') is not None
    assert ghl_webhooks.task_for(constants.GHL_CONTACT_UPDATE, '') is not None
    assert ghl_webhooks.task_for(constants.GHL_APPOINTMENT_UPDATE, '') is not None
    assert ghl_webhooks.task_for(constants.GHL_APPOINTMENT_DELETE, '') is not None
    for object_key in (
        constants.LEADS_OBJECT,
        constants.ESTIMATES_OBJECT,
        constants.JOBS_OBJECT,
    ):
        assert ghl_webhooks.task_for(constants.GHL_RECORD_CREATE, object_key) is not None
        assert ghl_webhooks.task_for(constants.GHL_RECORD_UPDATE, object_key) is None
    for event_type in (
        constants.GHL_RECORD_CREATE,
        constants.GHL_RECORD_UPDATE,
        constants.GHL_RECORD_DELETE,
    ):
        assert ghl_webhooks.task_for(event_type, constants.JOB_LINE_ITEMS_OBJECT) is not None


def test_task_for_an_unknown_event_is_none():
    assert ghl_webhooks.task_for('SomethingElse', '') is None
    assert ghl_webhooks.task_for('', '') is None
