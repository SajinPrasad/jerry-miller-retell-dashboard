from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.test import override_settings

from apps.rebilling.models import ClientMapping, RetellCall, UnassignedCall
from apps.rebilling.services import store_call

pytestmark = pytest.mark.django_db


def make_client(location_id='loc1', **overrides):
    fields = {
        'location_id': location_id,
        'client_name': 'Cool Breeze HVAC',
        'retell_agent_ids': ['agent1'],
        'phone_numbers': ['+12137771234'],
    }
    fields.update(overrides)
    return ClientMapping.objects.create(**fields)


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


def test_matches_by_agent_id():
    client = make_client()

    store_call(call_payload(to_number='+19999999999'))

    assert RetellCall.objects.get(call_id='call1').client == client


def test_stores_the_call_fields():
    make_client()

    store_call(call_payload())

    call = RetellCall.objects.get(call_id='call1')
    assert call.duration_ms == 15791
    assert call.retell_cost_cents == 42  # combined_cost is in cents, rounded to whole cents
    assert call.call_status == 'ended'
    assert call.direction == 'inbound'
    assert call.started_at == datetime(2024, 5, 2, 0, 7, 55, 945000, tzinfo=UTC)
    assert call.ended_at == datetime(2024, 5, 2, 0, 8, 11, 736000, tzinfo=UTC)


def test_inbound_matches_on_the_dialled_number():
    client = make_client(retell_agent_ids=[])

    store_call(call_payload(direction='inbound', to_number='+12137771234'))

    assert RetellCall.objects.get(call_id='call1').client == client


def test_outbound_matches_on_the_calling_number():
    client = make_client(retell_agent_ids=[])

    store_call(
        call_payload(direction='outbound', from_number='+12137771234', to_number='+15550000001')
    )

    assert RetellCall.objects.get(call_id='call1').client == client


def test_number_match_ignores_formatting():
    client = make_client(retell_agent_ids=[], phone_numbers=['+1 (213) 777-1234'])

    store_call(call_payload(agent_id='unmapped'))

    assert RetellCall.objects.get(call_id='call1').client == client


def test_agent_match_wins_over_a_number_match():
    by_number = make_client('loc1', retell_agent_ids=[], phone_numbers=['+12137771234'])
    by_agent = make_client('loc2', client_name='Branwynn', retell_agent_ids=['agent1'])

    store_call(call_payload())

    assert RetellCall.objects.get(call_id='call1').client == by_agent
    assert by_number.calls.count() == 0


def test_unmatched_call_is_stored_as_unassigned():
    make_client()

    store_call(call_payload(agent_id='other-agent', to_number='+19999999999'))

    assert not RetellCall.objects.exists()
    unassigned = UnassignedCall.objects.get(call_id='call1')
    assert unassigned.agent_id == 'other-agent'
    assert unassigned.duration_ms == 15791
    assert unassigned.retell_cost_cents == 42


def test_inactive_client_does_not_match():
    make_client(active=False)

    store_call(call_payload())

    assert UnassignedCall.objects.filter(call_id='call1').exists()


def test_web_call_without_numbers_is_unassigned():
    make_client(retell_agent_ids=[])

    store_call({'call_id': 'call1', 'call_type': 'web_call', 'call_status': 'ended'})

    assert UnassignedCall.objects.filter(call_id='call1').exists()


def test_duplicate_webhook_is_ignored():
    make_client()

    store_call(call_payload())
    store_call(call_payload(duration_ms=999))

    call = RetellCall.objects.get()
    assert call.duration_ms == 15791


def test_duplicate_of_an_unassigned_call_is_ignored():
    store_call(call_payload())
    store_call(call_payload())

    assert UnassignedCall.objects.count() == 1
    assert not RetellCall.objects.exists()


def test_call_without_an_id_is_ignored():
    make_client()

    store_call(call_payload(call_id=None))

    assert not RetellCall.objects.exists()
    assert not UnassignedCall.objects.exists()


def test_missing_duration_and_cost_are_stored_as_null():
    make_client()

    payload = call_payload()
    del payload['duration_ms']
    del payload['call_cost']
    store_call(payload)

    call = RetellCall.objects.get()
    assert call.duration_ms is None
    assert call.retell_cost_cents is None
    assert call.billable_minutes == Decimal(0)


def test_billable_minutes_per_second():
    make_client()
    store_call(call_payload(duration_ms=90_000))

    assert RetellCall.objects.get().billable_minutes == Decimal('1.5')


@override_settings(RETELL_ROUNDING='round_up_minute')
def test_billable_minutes_rounded_up():
    make_client()
    store_call(call_payload(duration_ms=90_000))

    assert RetellCall.objects.get().billable_minutes == Decimal(2)
