"""Storing one Retell call against the client whose workspace it came from.

There is no matching left to test. A workspace belongs to exactly one client, so whichever
key read the call - or whichever webhook URL it arrived on - has already said whose it is.
What is left is what gets stored, whether it can be billed, and what happens when the call
names an agent this client has never had.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.test import override_settings

from apps.rebilling.models import (
    BILLABLE,
    UNBILLABLE,
    ClientMapping,
    RetellCall,
    RetellWorkspace,
)
from apps.rebilling.services import store_workspace_call

pytestmark = pytest.mark.django_db

# Stands in for a workspace's own API key everywhere in the suite. Never a real one, and
# never read by anything but a respx mock.
WORKSPACE_KEY = 'key_workspace_cool_breeze'


def make_client(location_id='loc1', **overrides):
    fields = {
        'location_id': location_id,
        'client_name': 'Cool Breeze HVAC',
        'retell_agent_ids': ['agent1'],
        'phone_numbers': ['+12137771234'],
    }
    fields.update(overrides)
    return ClientMapping.objects.create(**fields)


def make_workspace(client=None, api_key=WORKSPACE_KEY, **overrides):
    """A client with their own Retell workspace: the only way a call can reach us."""
    return RetellWorkspace.objects.create(
        client=client or make_client(),
        api_key=api_key,
        workspace_name='Cool Breeze HVAC',
        **overrides,
    )


def call_payload(**overrides):
    call = {
        'call_id': 'call1',
        'call_type': 'phone_call',
        'agent_id': 'agent1',
        'from_number': '+15550000001',
        'to_number': '+12137771234',
        'direction': 'inbound',
        'call_status': 'ended',
        'start_timestamp': 1714608475945,
        'end_timestamp': 1714608491736,
        'duration_ms': 15791,
        'call_cost': {'combined_cost': 42.4, 'total_duration_seconds': 15.8},
    }
    call.update(overrides)
    return call


# --- whose call it is ------------------------------------------------------------------------


def test_the_call_belongs_to_the_workspace_it_came_from():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload())

    assert RetellCall.objects.get(call_id='call1').client == workspace.client


def test_nothing_about_the_call_can_send_it_to_another_client():
    """Numbers and agents are not consulted at all: the workspace is the whole answer."""
    workspace = make_workspace()
    other = make_client('loc2', client_name='Branwynn', retell_agent_ids=['agent_other'])

    store_workspace_call(
        workspace,
        call_payload(agent_id='agent_other', to_number='+19999999999'),
    )

    assert RetellCall.objects.get(call_id='call1').client == workspace.client
    assert other.calls.count() == 0


def test_an_unknown_agent_is_kept_and_learned_rather_than_refused():
    """The GHL team adding an agent must not cost the client that agent's first calls."""
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload(agent_id='agent_new'))

    call = RetellCall.objects.get(call_id='call1')
    assert call.client == workspace.client
    assert call.agent_id == 'agent_new'
    workspace.client.refresh_from_db()
    assert workspace.client.retell_agent_ids == ['agent1', 'agent_new']


def test_a_known_agent_does_not_grow_the_list():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload())

    workspace.client.refresh_from_db()
    assert workspace.client.retell_agent_ids == ['agent1']


def test_a_call_for_a_client_whose_billing_is_paused_is_still_recorded():
    """`active` decides whether they are billed, not whether their calls are kept."""
    workspace = make_workspace(make_client(active=False))

    store_workspace_call(workspace, call_payload())

    assert RetellCall.objects.get(call_id='call1').client == workspace.client


# --- what gets stored ------------------------------------------------------------------------


def test_stores_the_call_fields():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload())

    call = RetellCall.objects.get(call_id='call1')
    assert call.duration_ms == 15791
    assert call.retell_cost_cents == 42  # combined_cost is in cents, rounded to whole cents
    assert call.call_status == 'ended'
    assert call.direction == 'inbound'
    assert call.started_at == datetime(2024, 5, 2, 0, 7, 55, 945000, tzinfo=UTC)
    assert call.ended_at == datetime(2024, 5, 2, 0, 8, 11, 736000, tzinfo=UTC)


def test_duplicate_webhook_is_ignored():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload())
    store_workspace_call(workspace, call_payload(duration_ms=999))

    call = RetellCall.objects.get()
    assert call.duration_ms == 15791


def test_call_without_an_id_is_ignored():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload(call_id=None))

    assert not RetellCall.objects.exists()


# --- whether it can be billed ------------------------------------------------------------------


def test_a_complete_call_is_billable_with_no_reason():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload())

    call = RetellCall.objects.get()
    assert call.billing_status == BILLABLE
    assert call.unbillable_reason == ''


def test_a_duration_in_seconds_is_stored_as_milliseconds():
    workspace = make_workspace()

    payload = call_payload()
    del payload['duration_ms']  # leaves call_cost.total_duration_seconds = 15.8
    store_workspace_call(workspace, payload)

    call = RetellCall.objects.get()
    assert call.duration_ms == 15_800
    assert call.billing_status == BILLABLE


def test_a_top_level_seconds_duration_is_stored_as_milliseconds():
    workspace = make_workspace()

    payload = call_payload(duration_seconds=12)
    del payload['duration_ms']
    del payload['call_cost']
    store_workspace_call(workspace, payload)

    assert RetellCall.objects.get().duration_ms == 12_000


def test_a_call_without_a_duration_is_kept_but_unbillable():
    workspace = make_workspace()

    payload = call_payload()
    del payload['duration_ms']
    del payload['call_cost']
    store_workspace_call(workspace, payload)

    call = RetellCall.objects.get()
    assert call.duration_ms is None
    assert call.retell_cost_cents is None
    assert call.billable_minutes == Decimal(0)
    assert call.billing_status == UNBILLABLE
    assert call.unbillable_reason == 'Retell has not reported a duration for this call.'


def test_a_call_that_has_not_ended_is_unbillable():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload(call_status='ongoing'))

    call = RetellCall.objects.get()
    assert call.billing_status == UNBILLABLE
    assert call.unbillable_reason == 'Call status is ongoing, not ended.'


def test_a_call_with_no_status_at_all_is_unbillable():
    workspace = make_workspace()

    store_workspace_call(workspace, call_payload(call_status=None))

    assert RetellCall.objects.get().unbillable_reason == 'Call status is missing, not ended.'


def test_a_call_without_a_start_time_is_unbillable():
    workspace = make_workspace()

    payload = call_payload()
    del payload['start_timestamp']
    store_workspace_call(workspace, payload)

    call = RetellCall.objects.get()
    assert call.billing_status == UNBILLABLE
    assert 'start time' in call.unbillable_reason


def test_billable_minutes_per_second():
    workspace = make_workspace()
    store_workspace_call(workspace, call_payload(duration_ms=90_000))

    assert RetellCall.objects.get().billable_minutes == Decimal('1.5')


@override_settings(RETELL_ROUNDING='round_up_minute')
def test_billable_minutes_rounded_up():
    workspace = make_workspace()
    store_workspace_call(workspace, call_payload(duration_ms=90_000))

    assert RetellCall.objects.get().billable_minutes == Decimal(2)
