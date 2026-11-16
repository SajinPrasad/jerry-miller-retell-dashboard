"""The inbound HCP webhook endpoint: what it records, what it queues, and what it refuses.

The tasks are stubbed at the dispatch table, so these are about the endpoint's own job -
recording the delivery before anything acts on it, and answering quickly - not about what the
handlers then do. Those have their own tests.
"""

import json

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import WebhookEvent
from apps.hcp import webhooks
from apps.hcp.tests import fixtures

pytestmark = pytest.mark.django_db

URL = reverse('hcp:webhook')


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

    monkeypatch.setattr(
        webhooks,
        'DISPATCH',
        [
            (webhooks.constants.CUSTOMER_EVENTS, FakeTask('customer')),
            (webhooks.constants.LEAD_EVENTS, FakeTask('lead')),
            (webhooks.constants.ESTIMATE_EVENTS, FakeTask('estimate')),
            (webhooks.constants.JOB_EVENTS, FakeTask('job')),
            (webhooks.constants.APPOINTMENT_EVENTS, FakeTask('appointment')),
        ],
    )
    return calls


def post(api, payload):
    return api.post(URL, data=json.dumps(payload), content_type='application/json')


# --- the handshake ---------------------------------------------------------------------


def test_the_configuration_handshake_is_answered_200(api, queued):
    """HCP verifies a newly-configured URL by POSTing exactly this and wanting a 200."""
    response = post(api, {'foo': 'bar'})

    assert response.status_code == 200
    assert response.json() == {'message': 'success'}


def test_the_handshake_is_not_recorded_as_a_delivery(api, queued):
    """One stored row per URL check would bury the real events."""
    post(api, {'foo': 'bar'})

    assert not WebhookEvent.objects.exists()


def test_the_handshake_queues_nothing(api, queued):
    post(api, {'foo': 'bar'})

    assert queued == []


# --- malformed deliveries ---------------------------------------------------------------


def test_a_body_that_is_not_json_is_a_400(api, queued):
    response = api.post(URL, data='not json', content_type='application/json')

    assert response.status_code == 400
    assert queued == []


def test_a_json_array_is_a_400(api, queued):
    response = post(api, ['not', 'an', 'object'])

    assert response.status_code == 400


def test_a_rejected_body_is_recorded_without_its_content(api, queued):
    post(api, ['not', 'an', 'object'])

    event = WebhookEvent.objects.get()
    assert event.source == WebhookEvent.HCP
    assert event.status == WebhookEvent.REJECTED
    assert event.payload is None


def test_a_delivery_with_no_company_id_is_a_400(api, queued):
    response = post(api, {'event': 'customer.created', 'customer': {'id': 'c-1'}})

    assert response.status_code == 400
    assert queued == []


def test_a_delivery_with_no_event_is_a_400(api, queued):
    response = post(api, {'company_id': fixtures.COMPANY_ID, 'customer': {'id': 'c-1'}})

    assert response.status_code == 400


# --- recording and dispatch --------------------------------------------------------------


def test_a_delivery_is_recorded_before_it_is_queued(api, queued):
    """The payload is stored first, so a handler bug costs a retry rather than the event."""
    post(api, fixtures.customer_event())

    event = WebhookEvent.objects.get()
    assert event.source == WebhookEvent.HCP
    assert event.event_type == 'customer.created'
    assert event.payload['customer']['id'] == 'hcp-customer-1'


def test_the_stored_delivery_is_the_one_handed_to_the_task(api, queued):
    post(api, fixtures.customer_event())

    event = WebhookEvent.objects.get()
    assert queued[0][2] == event.pk


def test_the_entity_id_is_kept_as_the_event_id(api, queued):
    """HCP has no envelope-level id, so the entity's own is what makes two deliveries about
    the same job findable together.
    """
    post(api, fixtures.job_event())

    assert WebhookEvent.objects.get().event_id == 'hcp-job-1'


@pytest.mark.parametrize(
    'payload,expected',
    [
        (fixtures.customer_event('customer.created'), 'customer'),
        (fixtures.customer_event('customer.updated'), 'customer'),
        (fixtures.lead_event('lead.created'), 'lead'),
        (fixtures.lead_event('lead.updated'), 'lead'),
        (fixtures.lead_event('lead.converted'), 'lead'),
        (fixtures.lead_event('lead.lost'), 'lead'),
        (fixtures.estimate_event('estimate.created'), 'estimate'),
        (fixtures.estimate_event('estimate.sent'), 'estimate'),
        (fixtures.estimate_event('estimate.option.approval_status_changed'), 'estimate'),
        (fixtures.job_event('job.created'), 'job'),
        (fixtures.job_event('job.completed'), 'job'),
        (fixtures.job_event('job.paid'), 'job'),
        (fixtures.appointment_event('job.appointment.scheduled'), 'appointment'),
        (fixtures.appointment_event('job.appointment.rescheduled'), 'appointment'),
        (fixtures.appointment_event('job.appointment.appointment_discarded'), 'appointment'),
    ],
)
def test_each_event_family_reaches_its_own_task(api, queued, payload, expected):
    response = post(api, payload)

    assert response.status_code == 200
    assert [name for name, _, _ in queued] == [expected]


def test_an_appointment_event_does_not_land_on_the_job_task(api, queued):
    """Both start `job.`, and an appointment handled as a job would rewrite the job instead of
    booking anything.
    """
    post(api, fixtures.appointment_event())

    assert queued[0][0] == 'appointment'


def test_an_unhandled_event_is_recorded_ignored_and_still_answered_200(api, queued):
    response = post(api, fixtures.envelope('invoice.something_new', invoice={'id': 'i-1'}))

    assert response.status_code == 200
    assert WebhookEvent.objects.get().status == WebhookEvent.IGNORED
    assert queued == []


def test_an_unknown_company_is_still_recorded_and_queued(api, queued):
    """Whether the company is connected is the task's call, because the task is what has to be
    replayable once it is.
    """
    response = post(api, fixtures.envelope('customer.created', customer={'id': 'c-1'}))

    assert response.status_code == 200
    assert WebhookEvent.objects.count() == 1
    assert len(queued) == 1


# --- the dispatch table itself -----------------------------------------------------------


def test_job_and_appointment_event_names_do_not_overlap():
    """The dispatch is on exact names, so an overlap would make the ordering load-bearing."""
    from apps.hcp import constants

    assert not (constants.JOB_EVENTS & constants.APPOINTMENT_EVENTS)


def test_every_known_event_resolves_to_a_task():
    from apps.hcp import constants

    every = (
        constants.CUSTOMER_EVENTS
        | constants.LEAD_EVENTS
        | constants.ESTIMATE_EVENTS
        | constants.JOB_EVENTS
        | constants.APPOINTMENT_EVENTS
    )

    assert all(webhooks.task_for(name) is not None for name in every)


def test_an_event_we_do_not_handle_resolves_to_nothing():
    assert webhooks.task_for('customer.deleted') is None
    assert webhooks.task_for('') is None
