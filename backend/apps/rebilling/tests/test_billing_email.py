"""Who pays, taken from GoHighLevel rather than from anyone typing it.

A sub-account's owner is read on every sync; their email becomes the client's `billing_email`,
and that is the only thing Stripe matches a customer by. There is no picker and no text box
anywhere in this flow, so what these tests are about is the copy staying honest: following GHL
when it changes, and never leaving a stale address pointed at a live card.
"""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.ghl.models import GHLOAuthToken
from apps.rebilling.models import Bill, ClientMapping
from apps.rebilling.services import (
    billing_email_for,
    client_detail,
    set_mapping,
    sync_billing_emails,
)

from .test_services import make_client

pytestmark = pytest.mark.django_db

MONTH_START = date(2026, 3, 1)
MONTH_END = date(2026, 3, 31)


def make_sub_account(
    location_id='loc1',
    name='Cool Breeze HVAC',
    email='owner@coolbreeze.test',
    first='Dana',
    last='Reyes',
):
    """A sub-account as a GoHighLevel sync leaves it, owner and all."""
    return GHLOAuthToken.objects.create(
        company_id='comp1',
        location_id=location_id,
        name=name,
        owner_first_name=first,
        owner_last_name=last,
        owner_email=email,
        access_token='token',
        expires_at=timezone.now() + timedelta(days=1),
    )


# --- the email follows the sub-account's owner ------------------------------------------------


def test_the_owners_email_becomes_the_clients_billing_email():
    make_sub_account()
    client = make_client('loc1')

    assert sync_billing_emails() == 1

    client.refresh_from_db()
    assert client.billing_email == 'owner@coolbreeze.test'


def test_a_changed_owner_email_clears_the_cached_stripe_customer():
    """The cached id was found by looking the *old* address up.

    `customer_id()` returns a cached id without checking anything, so keeping it would go on
    charging the previous payer's card for as long as the mapping existed.
    """
    make_sub_account(email='new@coolbreeze.test')
    client = make_client('loc1', billing_email='old@coolbreeze.test', stripe_customer_id='cus_old')

    sync_billing_emails()

    client.refresh_from_db()
    assert client.billing_email == 'new@coolbreeze.test'
    assert client.stripe_customer_id == ''


def test_an_unchanged_email_leaves_the_cached_customer_alone():
    """Otherwise every sync would throw away the lookup and make the next charge pay for it."""
    make_sub_account()
    client = make_client(
        'loc1', billing_email='owner@coolbreeze.test', stripe_customer_id='cus_known'
    )

    assert sync_billing_emails() == 0

    client.refresh_from_db()
    assert client.stripe_customer_id == 'cus_known'


def test_an_email_ghl_no_longer_holds_is_cleared_rather_than_kept():
    """A stale address would quietly match, and charge, whichever customer still has it."""
    make_sub_account(email='')
    client = make_client('loc1', billing_email='gone@coolbreeze.test', stripe_customer_id='cus_old')

    sync_billing_emails()

    client.refresh_from_db()
    assert client.billing_email == ''
    assert client.stripe_customer_id == ''


def test_a_sub_account_nobody_has_mapped_is_skipped():
    make_sub_account('loc-unmapped')

    assert sync_billing_emails() == 0
    assert not ClientMapping.objects.exists()


def test_setting_a_mapping_up_seeds_its_billing_email():
    """A client set up between syncs is billable straight away, not on the next refresh."""
    make_sub_account()

    mapping = set_mapping('loc1', 'Cool Breeze HVAC', {'retell_agent_ids': ['agent_1']})

    assert mapping.billing_email == 'owner@coolbreeze.test'


def test_a_mapping_for_a_sub_account_we_have_no_owner_for_is_still_created():
    mapping = set_mapping('loc-unknown', 'Summit Air', {'retell_agent_ids': ['agent_1']})

    assert mapping.billing_email == ''
    assert billing_email_for('loc-unknown') == ''


# --- what the admin page reads ------------------------------------------------------------------


def test_the_detail_view_shows_who_pays_and_that_stripe_matched_them():
    make_sub_account()
    client = make_client(
        'loc1', billing_email='owner@coolbreeze.test', stripe_customer_id='cus_known'
    )

    detail = client_detail(client, MONTH_START, MONTH_END)

    assert detail['owner_name'] == 'Dana Reyes'
    assert detail['billing_email'] == 'owner@coolbreeze.test'
    assert detail['stripe_customer_id'] == 'cus_known'
    assert detail['billing_failure_reason'] == ''


def test_the_detail_view_carries_the_reason_a_bill_could_not_be_charged():
    """With no customer matched, this message is the whole of what an admin can act on."""
    make_sub_account()
    client = make_client('loc1', billing_email='owner@coolbreeze.test')
    Bill.objects.create(
        client=client,
        billing_period='2026-03',
        total_cents=650,
        failure_reason='No Stripe customer found for billing email: owner@coolbreeze.test',
    )

    detail = client_detail(client, MONTH_START, MONTH_END)

    assert detail['stripe_customer_id'] == ''
    assert 'No Stripe customer found' in detail['billing_failure_reason']
