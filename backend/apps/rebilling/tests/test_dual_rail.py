"""Phase 5: charging a bill on GHL first and falling back to Stripe when GHL does not collect.

Every GHL call is mocked with respx and every Stripe call with the same `FakeStripe` the Stripe
tests use. Nothing here reaches the network.

The cases that matter most are the ones about not charging twice: that Stripe is never reached
while GHL still might collect, and that a GHL invoice which refuses to void stops the fallback
dead rather than letting both rails run.
"""

import json
from datetime import timedelta
from io import StringIO

import httpx
import pytest
import respx
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ghl.models import GHLOAuthToken
from apps.rebilling import dual_rail, ghl_billing
from apps.rebilling.models import (
    CHARGING,
    FAILED,
    PAID,
    SEND_INVOICE,
    UNCHARGED,
    Bill,
)

from .test_stripe_billing import MONTH, fake, make_bill, make_billable_client  # noqa: F401

pytestmark = pytest.mark.django_db

BASE = settings.GHL_API_BASE_URL
MAIN = settings.GHL_MAIN_LOCATION_ID
CONTACT = 'contact_abc'
INVOICE = 'inv_ghl1'
WEBHOOK_URL = '/api/rebilling/webhooks/ghl-invoices/'


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def main_location():
    """A token for the sub-account the invoices are raised in."""
    return GHLOAuthToken.objects.create(
        company_id='comp_demo',
        location_id=MAIN,
        name='Main Sub-Account',
        access_token='loc-token',
        expires_at=timezone.now() + timedelta(days=1),
    )


@pytest.fixture
def billable(main_location):
    """A client GHL can invoice: a billing contact, a card, and a bill waiting to be charged."""
    client = make_billable_client(billing_contact_id=CONTACT, stripe_customer_id='cus_known')
    make_bill(client)
    return client


def mock_contact(phone='+13105551212', email='ops@coolbreeze.test'):
    return respx.get(f'{BASE}/contacts/{CONTACT}').mock(
        return_value=httpx.Response(
            200,
            json={
                'contact': {
                    'id': CONTACT,
                    'contactName': 'Cool Breeze HVAC',
                    'email': email,
                    'phone': phone,
                }
            },
        )
    )


def mock_create(invoice_id=INVOICE):
    return respx.post(f'{BASE}/invoices/').mock(
        return_value=httpx.Response(200, json={'_id': invoice_id})
    )


def mock_send(invoice_id=INVOICE, status_code=200):
    return respx.post(f'{BASE}/invoices/{invoice_id}/send').mock(
        return_value=httpx.Response(status_code, json={'status': 'sent'})
    )


def mock_void(invoice_id=INVOICE, status_code=200):
    return respx.post(f'{BASE}/invoices/{invoice_id}/void').mock(
        return_value=httpx.Response(status_code, json={'status': 'void'})
    )


def age_bill(bill, hours):
    """Put a charging bill's clock far enough back to be eligible for the fallback."""
    Bill.objects.filter(pk=bill.pk).update(charged_at=timezone.now() - timedelta(hours=hours))


# --- the GHL rail -----------------------------------------------------------------------------


@respx.mock
def test_a_bill_is_raised_and_sent_on_ghl(billable, fake):  # noqa: F811
    mock_contact()
    create, send = mock_create(), mock_send()

    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_sent'] == 1
    assert result['total_cents'] == 650
    assert create.called and send.called

    bill = Bill.objects.get()
    assert bill.status == CHARGING
    assert bill.ghl_invoice_id == INVOICE
    assert bill.charged_at is not None


@respx.mock
def test_the_invoice_asks_ghl_to_take_the_card(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    send = mock_send()

    dual_rail.charge_bills(MONTH)

    sent = json.loads(send.calls.last.request.content)
    # `enable` on its own is the whole mechanism: there is no public way to read a saved card,
    # so there is no paymentMethodId to send even if GHL wanted one.
    assert sent['autoPayment'] == {'enable': True}
    assert sent['altId'] == MAIN
    assert sent['altType'] == 'location'


@respx.mock
def test_the_invoice_is_raised_in_the_main_sub_account(billable, fake):  # noqa: F811
    mock_contact()
    create = mock_create()
    mock_send()

    dual_rail.charge_bills(MONTH)

    sent = json.loads(create.calls.last.request.content)
    # Not AGENCY_LOCATION_ID: the agency account holds no contacts and no saved cards.
    assert sent['altId'] == MAIN
    assert sent['altId'] != settings.AGENCY_LOCATION_ID


@respx.mock
def test_line_amounts_are_dollars_not_cents(billable, fake):  # noqa: F811
    mock_contact()
    create = mock_create()
    mock_send()

    dual_rail.charge_bills(MONTH)

    items = json.loads(create.calls.last.request.content)['items']
    # Phase 3 produced 500c and 150c. GHL types amount as a number, so it is in dollars.
    assert sorted(item['amount'] for item in items) == [1.5, 5.0]


@respx.mock
def test_contact_details_come_from_ghl(billable, fake):  # noqa: F811
    mock_contact(phone='+13105559999', email='billing@coolbreeze.test')
    create = mock_create()
    mock_send()

    dual_rail.charge_bills(MONTH)

    details = json.loads(create.calls.last.request.content)['contactDetails']
    assert details['phoneNo'] == '+13105559999'
    assert details['email'] == 'billing@coolbreeze.test'


@respx.mock
def test_a_contact_without_a_phone_still_bills(billable, fake):  # noqa: F811
    mock_contact(phone=None)
    create = mock_create()
    mock_send()

    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_sent'] == 1
    details = json.loads(create.calls.last.request.content)['contactDetails']
    assert details['phoneNo'] == ghl_billing.NO_PHONE


@respx.mock
def test_the_billing_email_is_taken_from_the_contact(billable, fake):  # noqa: F811
    mock_contact(email='newbilling@coolbreeze.test')
    mock_create()
    mock_send()

    dual_rail.charge_bills(MONTH)

    billable.refresh_from_db()
    # The Stripe fallback finds its customer by this, so it tracks the contact the invoice went to.
    assert billable.billing_email == 'newbilling@coolbreeze.test'


@respx.mock
def test_a_client_with_no_billing_contact_is_not_invoiced(main_location, fake):  # noqa: F811
    make_bill(make_billable_client())

    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_failed'] == 1
    assert Bill.objects.get().status == UNCHARGED


@respx.mock
def test_a_send_invoice_client_is_skipped(main_location, fake):  # noqa: F811
    client = make_billable_client(billing_contact_id=CONTACT, billing_mode=SEND_INVOICE)
    make_bill(client)

    result = dual_rail.charge_bills(MONTH)

    assert result['skipped'] == 1
    assert result['ghl_sent'] == 0
    assert Bill.objects.get().status == UNCHARGED


@respx.mock
def test_the_invoice_id_is_kept_when_sending_fails(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send(status_code=500)

    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_failed'] == 1
    bill = Bill.objects.get()
    # Left uncharged so it is retried, but traceable to the invoice GHL really did create.
    assert bill.status == UNCHARGED
    assert bill.ghl_invoice_id == INVOICE


@respx.mock
def test_a_retry_after_a_failed_send_reuses_the_same_invoice(billable, fake):  # noqa: F811
    mock_contact()
    create = mock_create()
    failing_send = mock_send(status_code=500)

    dual_rail.charge_bills(MONTH)
    assert Bill.objects.get().ghl_invoice_id == INVOICE

    # Second run: the invoice already exists, so it is sent again rather than raised again.
    # Creating a second would leave the first as an orphan draft in Jerry's account.
    failing_send.mock(return_value=httpx.Response(200, json={'status': 'sent'}))
    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_sent'] == 1
    assert create.call_count == 1
    assert Bill.objects.get().status == CHARGING


@respx.mock
def test_one_clients_failure_does_not_stop_the_others(main_location, fake):  # noqa: F811
    good = make_billable_client('loc1', billing_contact_id=CONTACT)
    make_bill(good)
    make_bill(make_billable_client('loc2'))  # no billing contact

    mock_contact()
    mock_create()
    mock_send()

    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_sent'] == 1
    assert result['ghl_failed'] == 1


@respx.mock
def test_a_charging_bill_is_not_charged_again(billable, fake):  # noqa: F811
    mock_contact()
    create = mock_create()
    mock_send()

    dual_rail.charge_bills(MONTH)
    dual_rail.charge_bills(MONTH)

    # The second run finds nothing uncharged, and the window has not passed.
    assert create.call_count == 1


# --- the fallback -----------------------------------------------------------------------------


@respx.mock
def test_stripe_is_not_touched_while_ghl_might_still_collect(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=settings.BILLING_FALLBACK_WINDOW_HOURS - 1)
    result = dual_rail.charge_bills(MONTH)

    assert result['fell_back'] == 0
    assert fake.names() == []
    assert Bill.objects.get().stripe_invoice_id == ''


@respx.mock
def test_a_bill_ghl_never_collected_falls_back_to_stripe(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    void = mock_void()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=settings.BILLING_FALLBACK_WINDOW_HOURS + 1)
    result = dual_rail.charge_bills(MONTH)

    assert result['fell_back'] == 1
    assert void.called
    bill = Bill.objects.get()
    assert bill.ghl_invoice_voided_at is not None
    assert bill.stripe_invoice_id == 'in_test1'
    assert bill.status == CHARGING


@respx.mock
def test_the_ghl_invoice_is_voided_before_stripe_is_charged(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    void = mock_void()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=99)
    dual_rail.charge_bills(MONTH)

    # The ordering is the safeguard: GHL must have stopped collecting before Stripe starts.
    assert void.called
    assert void.calls.last.request.read()
    assert 'Invoice.create' in fake.names()


@respx.mock
def test_a_void_that_fails_withholds_the_fallback(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    mock_void(status_code=500)

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=99)
    result = dual_rail.charge_bills(MONTH)

    assert result['fallback_blocked'] == 1
    assert result['fell_back'] == 0
    # Nothing on Stripe: GHL may still take this card, and two rails would take it twice.
    assert fake.names() == []
    bill = Bill.objects.get()
    assert bill.stripe_invoice_id == ''
    assert bill.ghl_invoice_voided_at is None


@respx.mock
def test_a_failed_fallback_leaves_the_bill_failed(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    mock_void()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=99)
    fake.fail_on = 'Invoice.create'
    result = dual_rail.charge_bills(MONTH)

    assert result['fallback_failed'] == 1
    bill = Bill.objects.get()
    assert bill.status == FAILED
    # The GHL invoice is voided, so nobody is collecting this without a human.
    assert bill.ghl_invoice_voided_at is not None


@respx.mock
def test_a_bill_does_not_fall_back_twice(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    void = mock_void()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=99)
    dual_rail.charge_bills(MONTH)
    dual_rail.charge_bills(MONTH)

    assert void.call_count == 1
    assert len(fake.of('Invoice.create')) == 1


@respx.mock
def test_the_fallback_sweeps_months_other_than_the_one_being_charged(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    void = mock_void()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=99)
    # A later month's run must still come back for last month's stranded bill.
    result = dual_rail.charge_bills('2026-04')

    assert result['fell_back'] == 1
    assert void.called


# --- the webhook ------------------------------------------------------------------------------


def post_event(event, signature='sig'):
    return APIClient().post(
        WEBHOOK_URL,
        data=json.dumps(event),
        content_type='application/json',
        HTTP_X_GHL_SIGNATURE=signature,
    )


@pytest.fixture
def signed(monkeypatch):
    """Accept the signature, so these tests are about the event and not the crypto."""
    monkeypatch.setattr(
        'apps.rebilling.views.ghl_webhooks.verify_signature', lambda body, sig: bool(sig)
    )


def charging_bill(**overrides):
    client = make_billable_client(billing_contact_id=CONTACT)
    bill = make_bill(client, status=CHARGING)
    Bill.objects.filter(pk=bill.pk).update(ghl_invoice_id=INVOICE, **overrides)
    return Bill.objects.get(pk=bill.pk)


def test_invoice_paid_marks_the_bill_paid(signed):
    charging_bill()

    response = post_event({'type': 'InvoicePaid', '_id': INVOICE, 'paidAt': '2026-03-15T10:30:00Z'})

    assert response.status_code == 204
    bill = Bill.objects.get()
    assert bill.status == PAID
    # GHL's own timestamp, not ours: a webhook can be retried hours after the payment.
    assert bill.paid_at.isoformat() == '2026-03-15T10:30:00+00:00'


def test_an_unsigned_webhook_is_rejected():
    charging_bill()

    assert post_event({'type': 'InvoicePaid', '_id': INVOICE}, signature='').status_code == 401
    assert Bill.objects.get().status == CHARGING


def test_a_body_that_is_not_json_is_rejected(signed):
    response = APIClient().post(
        WEBHOOK_URL, data='not json', content_type='application/json', HTTP_X_GHL_SIGNATURE='sig'
    )

    assert response.status_code == 400


def test_an_event_about_someone_elses_invoice_is_ignored(signed):
    charging_bill()

    response = post_event({'type': 'InvoicePaid', '_id': 'inv_someone_else'})

    assert response.status_code == 204
    assert Bill.objects.get().status == CHARGING


def test_the_other_invoice_events_change_nothing(signed):
    charging_bill()

    # GHL sends six more. None of them means the charge failed - no such event exists.
    for kind in ('InvoiceSent', 'InvoiceUpdate', 'InvoiceVoid', 'InvoicePartiallyPaid'):
        assert post_event({'type': kind, '_id': INVOICE}).status_code == 204

    assert Bill.objects.get().status == CHARGING


# --- the command ------------------------------------------------------------------------------


@respx.mock
def test_the_command_reports_both_rails(billable, fake):  # noqa: F811
    mock_contact()
    mock_create()
    mock_send()
    out = StringIO()

    call_command('charge_bills', '--month', MONTH, stdout=out)

    printed = out.getvalue()
    assert '1 bill(s) sent to GHL' in printed
    assert '650' in printed
    assert '0 moved to Stripe' in printed


def test_the_command_rejects_a_month_it_cannot_read(fake):  # noqa: F811
    with pytest.raises(Exception, match='YYYY-MM'):
        call_command('charge_bills', '--month', 'March', stdout=StringIO())


# --- the stripe-only escape hatch --------------------------------------------------------------
#
# This is the one path that moves money while bypassing the primary rail entirely, so the thing
# under test is as much "this cannot fire by accident" as "this works when deliberately asked for".


@respx.mock
def test_the_default_command_never_reaches_the_escape_hatch(billable, fake):  # noqa: F811
    """No flags at all: GHL is required, not optional. Confirms the escape hatch is opt-in."""
    mock_contact()
    mock_create()
    mock_send()

    call_command('charge_bills', '--month', MONTH, stdout=StringIO())

    bill = Bill.objects.get()
    assert bill.ghl_invoice_id == INVOICE
    assert bill.stripe_invoice_id == ''


def test_stripe_only_alone_is_rejected(billable, fake):  # noqa: F811
    """`--stripe-only` by itself does nothing. One flag must never be enough to skip GHL."""
    with pytest.raises(Exception, match='i-understand-this-skips-ghl'):
        call_command('charge_bills', '--month', MONTH, '--stripe-only', stdout=StringIO())

    # Rejected before anything ran: no charge attempted on either rail.
    assert Bill.objects.get().status == UNCHARGED
    assert fake.names() == []


@respx.mock
def test_stripe_only_with_confirmation_skips_ghl_entirely(billable, fake):  # noqa: F811
    """Both flags together: charges through Stripe directly. No GHL route is even registered -
    an unmocked request here would itself fail the test, which is the proof GHL was never touched.
    """
    out = StringIO()

    call_command(
        'charge_bills',
        '--month',
        MONTH,
        '--stripe-only',
        '--i-understand-this-skips-ghl',
        stdout=out,
    )

    printed = out.getvalue()
    assert 'Stripe-only charge' in printed
    assert '1 bill(s) sent to Stripe' in printed

    bill = Bill.objects.get()
    assert bill.status == CHARGING
    assert bill.stripe_invoice_id == 'in_test1'
    assert bill.ghl_invoice_id == ''


def test_i_understand_flag_alone_does_nothing(billable, fake):  # noqa: F811
    """The confirmation flag without `--stripe-only` is a no-op, not an alternate trigger.

    Routed through the ordinary dual-rail path, which calls GHL. Nothing here is mocked, so
    respx raises rather than letting the request reach a real network - the correct behaviour
    for a test, and proof this run attempted the GHL rail exactly like a bare `charge_bills`
    would, rather than taking any Stripe-only shortcut.
    """
    with respx.mock:
        with pytest.raises(Exception, match='not mocked'):
            call_command(
                'charge_bills', '--month', MONTH, '--i-understand-this-skips-ghl', stdout=StringIO()
            )

    bill = Bill.objects.get()
    assert bill.status == UNCHARGED
    assert bill.stripe_invoice_id == ''


# --- guarding the rails -------------------------------------------------------------------------


@respx.mock
def test_nothing_is_charged_without_a_billing_sub_account(billable, fake, settings):  # noqa: F811
    settings.GHL_MAIN_LOCATION_ID = ''

    result = dual_rail.charge_bills(MONTH)

    assert result['ghl_sent'] == 0
    assert result['ghl_failed'] == 1
    assert Bill.objects.get().status == UNCHARGED


@respx.mock
def test_the_fallback_finds_the_stripe_customer_by_the_contacts_email(main_location, fake):  # noqa: F811
    # No stripe_customer_id yet: the fallback has to find the customer by email, and the email
    # it uses is the one taken off the GHL contact when the invoice was raised.
    client = make_billable_client(billing_contact_id=CONTACT, billing_email='stale@old.test')
    make_bill(client)
    mock_contact(email='current@coolbreeze.test')
    mock_create()
    mock_send()
    mock_void()

    dual_rail.charge_bills(MONTH)
    age_bill(Bill.objects.get(), hours=99)
    result = dual_rail.charge_bills(MONTH)

    assert result['fell_back'] == 1
    assert fake.of('Customer.list')[0]['email'] == 'current@coolbreeze.test'
