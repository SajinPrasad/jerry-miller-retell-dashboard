"""Phase 7: whether a client has a card, asking them for one, and hearing that they answered.

Nothing here reaches Stripe or GoHighLevel. `fake` stands in for the Stripe calls, and the
autouse `ghl_contact_writes` records the contact field write the request makes.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx
from django.conf import settings as django_settings
from django.urls import reverse

from apps.ghl.exceptions import GHLError
from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency
from apps.rebilling.models import ClientMapping
from apps.rebilling.stripe_billing import (
    CARD_ON_FILE,
    CARD_UNKNOWN,
    NO_BILLING_EMAIL,
    NO_CARD,
    NO_CUSTOMER,
    clear_card_request,
    customer_facts,
    request_card,
)
from apps.rebilling.tests.conftest import CARDLESS, LEGACY_SOURCE
from apps.rebilling.tests.test_services import make_client

pytestmark = pytest.mark.django_db

EMAIL = 'owner@coolbreeze.test'
REQUESTED_AT = datetime(2026, 3, 10, 14, 20, tzinfo=UTC)

# A customer whose default payment method came back expanded, which is what Stripe answers the
# card read with: the method itself, not the `pm_…` id the rest of the suite's fakes carry.
WITH_CARD = SimpleNamespace(
    name='Cool Breeze HVAC LLC',
    invoice_settings=SimpleNamespace(
        default_payment_method=SimpleNamespace(card=SimpleNamespace(brand='visa', last4='4242'))
    ),
    default_source=None,
)


@pytest.fixture(autouse=True)
def no_retell_calls():
    """`client_detail` names each mapped agent off Retell, and `make_client` maps one.

    Its own router rather than a global `respx.mock`, so an unmocked call raises here instead
    of going out to Retell - the same guard `never_reach_stripe` is for Stripe.
    """
    router = respx.mock(assert_all_called=False)
    router.post(f'{django_settings.RETELL_API_BASE_URL}/v2/list-agents').mock(
        return_value=httpx.Response(200, json={'items': [], 'has_more': False})
    )
    with router:
        yield router


def card_request(api, location_id='loc1', blob=VECTOR_AGENCY):
    return api.post(
        reverse('rebilling:admin-client-card-request', args=[location_id]),
        HTTP_X_GHL_SESSION=blob,
    )


def card_saved(api, location_id='loc1', token='card-webhook-token'):
    return api.post(
        reverse('rebilling:ghl-card-webhook'),
        data=json.dumps({'locationId': location_id, 'status': 'card saved'}),
        content_type='application/json',
        HTTP_X_CARD_WEBHOOK_TOKEN=token,
    )


# --- what Stripe says ---------------------------------------------------------------------


def test_a_matched_customer_with_a_card_reports_the_brand_and_last_four(fake):
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = WITH_CARD

    facts = customer_facts(client)

    assert facts['stripe_customer_name'] == 'Cool Breeze HVAC LLC'
    assert facts['card'] == {'status': CARD_ON_FILE, 'brand': 'visa', 'last4': '4242'}


def test_the_card_is_read_on_the_call_the_name_was_already_being_read_on(fake):
    """The whole reason the status is not stored: it costs no Stripe call of its own."""
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = WITH_CARD

    customer_facts(client)

    assert len(fake.of('Customer.retrieve')) == 1
    assert fake.of('Customer.retrieve')[0]['expand'] == ['invoice_settings.default_payment_method']


def test_a_customer_with_no_card_at_all_reports_no_card(fake):
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = CARDLESS

    assert customer_facts(client)['card']['status'] == NO_CARD


def test_a_card_held_the_older_way_still_counts_as_a_card(fake):
    """Stripe collects against `default_source`, so claiming there is no card would be wrong."""
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = LEGACY_SOURCE

    assert customer_facts(client)['card'] == {'status': CARD_ON_FILE, 'brand': '', 'last4': ''}


def test_an_email_matching_no_stripe_customer_reports_no_customer(fake):
    client = make_client('loc1', billing_email=EMAIL)

    facts = customer_facts(client)

    assert facts['card']['status'] == NO_CUSTOMER
    assert facts['stripe_customer_id'] == ''


def test_a_matched_customer_is_remembered_so_the_next_view_costs_one_call(fake):
    client = make_client('loc1', billing_email=EMAIL)
    fake.customers = [SimpleNamespace(id='cus_found')]

    assert customer_facts(client)['stripe_customer_id'] == 'cus_found'
    client.refresh_from_db()
    assert client.stripe_customer_id == 'cus_found'


def test_a_client_with_no_billing_email_is_never_looked_up(fake):
    """There is nothing to look one up by, and nowhere to send a form - so no Stripe call."""
    client = make_client('loc1')

    assert customer_facts(client)['card']['status'] == NO_BILLING_EMAIL
    assert fake.names() == []


def test_an_unreachable_stripe_is_unknown_and_never_no_card(fake):
    """ "No card" would send someone chasing a client who has a perfectly good one."""
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.fail_on = 'Customer.retrieve'

    facts = customer_facts(client)

    assert facts['card']['status'] == CARD_UNKNOWN
    assert facts['stripe_customer_id'] == 'cus_known'


def test_an_unreachable_stripe_during_the_match_is_unknown_too(fake):
    client = make_client('loc1', billing_email=EMAIL)
    fake.fail_on = 'Customer.list'

    assert customer_facts(client)['card']['status'] == CARD_UNKNOWN


# --- asking for a card --------------------------------------------------------------------


def test_requesting_a_card_writes_the_status_onto_the_billing_contact(fake, ghl_contact_writes):
    client = make_client('loc1', billing_email=EMAIL)

    request_card(client)

    assert ghl_contact_writes == [(EMAIL, 'cf_card_status', 'card not found')]
    client.refresh_from_db()
    assert client.card_requested_at is not None


def test_requesting_a_card_for_a_client_with_no_email_writes_nothing(fake, ghl_contact_writes):
    client = make_client('loc1')

    with pytest.raises(GHLError, match='no billing email'):
        request_card(client)

    assert ghl_contact_writes == []
    client.refresh_from_db()
    assert client.card_requested_at is None


def test_a_refused_write_leaves_the_client_unasked(fake, ghl_contact_writes):
    """Nothing reached GoHighLevel, so recording that we asked would be a lie."""
    client = make_client('loc1', billing_email=EMAIL)
    ghl_contact_writes.fails_with = GHLError('custom field not found')

    with pytest.raises(GHLError):
        request_card(client)

    client.refresh_from_db()
    assert client.card_requested_at is None


# --- the endpoint behind the button -------------------------------------------------------


def test_an_agency_admin_can_request_a_card_and_sees_the_refreshed_status(
    api, fake, ghl_contact_writes
):
    make_agency()
    make_client('loc1', billing_email=EMAIL)

    response = card_request(api)

    assert response.status_code == 200
    assert response.json()['card']['status'] == NO_CUSTOMER
    assert response.json()['card']['requested_at'] is not None
    assert ghl_contact_writes == [(EMAIL, 'cf_card_status', 'card not found')]


def test_the_endpoint_reports_a_card_attached_between_the_page_loading_and_the_press(
    api, fake, ghl_contact_writes
):
    make_agency()
    make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = WITH_CARD

    body = card_request(api).json()

    assert body['card'] == {
        'status': CARD_ON_FILE,
        'brand': 'visa',
        'last4': '4242',
        'requested_at': body['card']['requested_at'],
    }


def test_a_sub_account_cannot_request_a_card_even_for_itself(api, fake, ghl_contact_writes):
    """This sends the client an email. A client chasing itself is not a thing to allow."""
    make_agency()
    make_client('loc1', billing_email=EMAIL)

    response = card_request(api, blob=VECTOR_LOCATION)

    assert response.status_code == 403
    assert ghl_contact_writes == []


def test_a_card_request_for_an_unmapped_client_is_a_404(api, fake, ghl_contact_writes):
    make_agency()

    assert card_request(api).status_code == 404
    assert ghl_contact_writes == []


def test_a_refused_write_is_a_502_saying_what_gohighlevel_said(api, fake, ghl_contact_writes):
    make_agency()
    client = make_client('loc1', billing_email=EMAIL)
    ghl_contact_writes.fails_with = GHLError('No contact in agency-loc with the email ...')

    response = card_request(api)

    assert response.status_code == 502
    assert 'No contact' in response.json()['detail']
    client.refresh_from_db()
    assert client.card_requested_at is None


# --- hearing that a card was saved --------------------------------------------------------


def test_a_saved_card_clears_the_request(api, fake):
    client = make_client(
        'loc1',
        billing_email=EMAIL,
        stripe_customer_id='cus_known',
        card_requested_at=REQUESTED_AT,
    )
    fake.customer_records['cus_known'] = WITH_CARD

    assert card_saved(api).status_code == 204
    client.refresh_from_db()
    assert client.card_requested_at is None


def test_the_webhook_is_believed_about_nothing(api, fake):
    """It says a card was saved; Stripe says there is none. Stripe is the one that knows."""
    client = make_client(
        'loc1',
        billing_email=EMAIL,
        stripe_customer_id='cus_known',
        card_requested_at=REQUESTED_AT,
    )
    fake.customer_records['cus_known'] = CARDLESS

    assert card_saved(api).status_code == 204
    client.refresh_from_db()
    assert client.card_requested_at == REQUESTED_AT


def test_a_redelivered_webhook_changes_nothing(api, fake):
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = WITH_CARD

    assert card_saved(api).status_code == 204
    assert card_saved(api).status_code == 204
    client.refresh_from_db()
    assert client.card_requested_at is None


def test_a_webhook_with_no_token_is_refused(api, fake):
    make_client('loc1', billing_email=EMAIL, card_requested_at=REQUESTED_AT)

    response = api.post(
        reverse('rebilling:ghl-card-webhook'),
        data=json.dumps({'locationId': 'loc1'}),
        content_type='application/json',
    )

    assert response.status_code == 401


def test_a_webhook_with_the_wrong_token_is_refused(api, fake):
    make_client('loc1', billing_email=EMAIL, card_requested_at=REQUESTED_AT)

    assert card_saved(api, token='not-the-token').status_code == 401


def test_an_unconfigured_token_refuses_every_request(api, fake, settings):
    """Blank must mean "nobody gets in", not "everybody does"."""
    settings.GHL_CARD_WEBHOOK_TOKEN = ''
    make_client('loc1', billing_email=EMAIL, card_requested_at=REQUESTED_AT)

    assert card_saved(api, token='').status_code == 401
    assert card_saved(api).status_code == 401


def test_a_webhook_with_no_location_is_a_400(api, fake):
    response = api.post(
        reverse('rebilling:ghl-card-webhook'),
        data=json.dumps({'status': 'card saved'}),
        content_type='application/json',
        HTTP_X_CARD_WEBHOOK_TOKEN='card-webhook-token',
    )

    assert response.status_code == 400


def test_an_unreadable_webhook_body_is_a_400(api, fake):
    response = api.post(
        reverse('rebilling:ghl-card-webhook'),
        data='not json',
        content_type='application/json',
        HTTP_X_CARD_WEBHOOK_TOKEN='card-webhook-token',
    )

    assert response.status_code == 400


def test_a_webhook_for_a_sub_account_we_do_not_bill_is_not_an_error(api, fake):
    """This workflow may well run for sub-accounts that are not billed here."""
    assert card_saved(api, location_id='loc-unknown').status_code == 204
    assert fake.names() == []


def test_clear_card_request_leaves_a_client_who_was_never_asked_alone(fake):
    client = make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.customer_records['cus_known'] = WITH_CARD

    assert clear_card_request(client) is True
    assert ClientMapping.objects.get(pk=client.pk).card_requested_at is None


def test_an_unreachable_stripe_does_not_clear_a_request(fake):
    client = make_client(
        'loc1',
        billing_email=EMAIL,
        stripe_customer_id='cus_known',
        card_requested_at=REQUESTED_AT,
    )
    fake.fail_on = 'Customer.retrieve'

    assert clear_card_request(client) is False
    assert ClientMapping.objects.get(pk=client.pk).card_requested_at == REQUESTED_AT


# --- what the client's own page carries ---------------------------------------------------


def test_the_detail_view_carries_the_card_and_when_it_was_asked_for(api, fake):
    make_agency()
    make_client(
        'loc1',
        billing_email=EMAIL,
        stripe_customer_id='cus_known',
        card_requested_at=REQUESTED_AT,
    )
    fake.customer_records['cus_known'] = WITH_CARD

    response = api.get(
        reverse('rebilling:admin-client-detail', args=['loc1']),
        {'month': '2026-03'},
        HTTP_X_GHL_SESSION=VECTOR_AGENCY,
    )

    assert response.json()['card'] == {
        'status': CARD_ON_FILE,
        'brand': 'visa',
        'last4': '4242',
        'requested_at': REQUESTED_AT.isoformat(),
    }


def test_stripe_being_down_does_not_take_the_clients_page_down(api, fake):
    make_agency()
    make_client('loc1', billing_email=EMAIL, stripe_customer_id='cus_known')
    fake.fail_on = 'Customer.retrieve'

    response = api.get(
        reverse('rebilling:admin-client-detail', args=['loc1']),
        {'month': '2026-03'},
        HTTP_X_GHL_SESSION=VECTOR_AGENCY,
    )

    assert response.status_code == 200
    assert response.json()['card']['status'] == CARD_UNKNOWN
