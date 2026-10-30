"""PUT /api/rebilling/admin/clients/<location_id>/mapping/: agency admins set up a client."""

import pytest
from django.urls import reverse

from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency
from apps.rebilling.models import ClientMapping
from apps.rebilling.tests.test_admin_views import make_location
from apps.rebilling.tests.test_services import make_client

pytestmark = pytest.mark.django_db

VALID_SID = 'AC' + '0' * 32
OTHER_SID = 'AC' + '1' * 32


def put_mapping(client, location_id, data, blob=VECTOR_AGENCY):
    return client.put(
        reverse('rebilling:admin-client-mapping', args=[location_id]),
        data=data,
        content_type='application/json',
        HTTP_X_GHL_SESSION=blob,
    )


# --- valid updates succeed ----------------------------------------------------------------


def test_sets_up_a_previously_unmapped_client(client):
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')

    response = put_mapping(
        client,
        'loc-new',
        {
            'twilio_subaccount_sid': VALID_SID,
            'retell_agent_ids': ['agent_1'],
            'phone_numbers': ['+12135551234'],
        },
    )

    assert response.status_code == 200
    mapping = ClientMapping.objects.get(location_id='loc-new')
    # The display name falls back to what Task 1's sync already knows; nothing here invents one.
    assert mapping.client_name == 'Polar Peak Heating'
    assert mapping.twilio_subaccount_sid == VALID_SID
    assert mapping.retell_agent_ids == ['agent_1']
    assert mapping.phone_numbers == ['+12135551234']


def test_updates_an_existing_mapping_without_touching_its_name(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1', client_name='Cool Breeze HVAC')

    response = put_mapping(
        client, 'loc1', {'twilio_subaccount_sid': OTHER_SID, 'retell_agent_ids': ['agent_2']}
    )

    assert response.status_code == 200
    mapping = ClientMapping.objects.get(location_id='loc1')
    assert mapping.client_name == 'Cool Breeze HVAC'
    assert mapping.twilio_subaccount_sid == OTHER_SID
    assert mapping.retell_agent_ids == ['agent_2']


def test_a_field_left_out_of_the_request_is_left_alone(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1', twilio_subaccount_sid=VALID_SID)

    put_mapping(client, 'loc1', {'retell_agent_ids': ['agent_9']})

    mapping = ClientMapping.objects.get(location_id='loc1')
    assert mapping.twilio_subaccount_sid == VALID_SID
    assert mapping.retell_agent_ids == ['agent_9']


def test_a_blank_twilio_sid_clears_it(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1', twilio_subaccount_sid=VALID_SID)

    response = put_mapping(client, 'loc1', {'twilio_subaccount_sid': ''})

    assert response.status_code == 200
    assert ClientMapping.objects.get(location_id='loc1').twilio_subaccount_sid == ''


def test_the_stripe_customer_cannot_be_set_by_hand(client):
    """It is matched from the sub-account owner's email, so there is nothing to type here.

    An id sent anyway is ignored rather than honoured: a hand-set customer would survive the
    owner's email changing and go on charging the previous payer.
    """
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1')

    response = put_mapping(client, 'loc1', {'stripe_customer_id': 'cus_someoneElse'})

    assert response.status_code == 200
    assert ClientMapping.objects.get(location_id='loc1').stripe_customer_id == ''


def test_agent_ids_and_phone_numbers_are_deduplicated(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1')

    response = put_mapping(
        client,
        'loc1',
        {'retell_agent_ids': ['agent_1', 'agent_1'], 'phone_numbers': ['+12135551234'] * 2},
    )

    assert response.data['retell_agent_ids'] == ['agent_1']
    assert response.data['phone_numbers'] == ['+12135551234']


# --- invalid data is rejected -------------------------------------------------------------


def test_a_malformed_twilio_sid_is_rejected(client):
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')

    response = put_mapping(client, 'loc-new', {'twilio_subaccount_sid': 'not-a-sid'})

    assert response.status_code == 400
    assert not ClientMapping.objects.filter(location_id='loc-new').exists()


def test_a_blank_agent_id_is_rejected(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1')

    response = put_mapping(client, 'loc1', {'retell_agent_ids': ['agent_1', '  ']})

    assert response.status_code == 400
    # Nothing was half-applied: the existing agent list is untouched.
    assert ClientMapping.objects.get(location_id='loc1').retell_agent_ids == ['agent1']


def test_an_unusable_phone_number_is_rejected(client):
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')

    response = put_mapping(client, 'loc-new', {'phone_numbers': ['12']})

    assert response.status_code == 400
    assert not ClientMapping.objects.filter(location_id='loc-new').exists()


def test_mapping_an_unonboarded_location_404s(client):
    make_agency()

    response = put_mapping(client, 'loc-never-onboarded', {'twilio_subaccount_sid': VALID_SID})

    assert response.status_code == 404
    assert not ClientMapping.objects.filter(location_id='loc-never-onboarded').exists()


# --- unauthorized callers are blocked ------------------------------------------------------


def test_401s_without_a_session(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')

    response = client.put(
        reverse('rebilling:admin-client-mapping', args=['loc1']),
        data={'twilio_subaccount_sid': VALID_SID},
        content_type='application/json',
    )

    assert response.status_code == 401


def test_403s_for_another_agency(client):
    make_agency(company_id='comp_jerry')
    make_location('loc1', 'Cool Breeze HVAC')

    assert put_mapping(client, 'loc1', {'twilio_subaccount_sid': VALID_SID}).status_code == 403


def test_a_sub_account_session_cannot_edit_mapping(client):
    """Mapping is an agency-admin job. The page shows a client their own usage, not settings."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1', client_name='Cool Breeze HVAC')

    response = put_mapping(
        client, 'loc1', {'twilio_subaccount_sid': VALID_SID}, blob=VECTOR_LOCATION
    )

    assert response.status_code == 403
