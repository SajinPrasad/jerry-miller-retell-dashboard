"""Phase 5: putting a calculated bill on a Stripe invoice and hearing back about it.

Every Stripe call is faked. Nothing here reaches the network, and the webhook signature is built
by hand rather than with the SDK, so the test signs independently of the code that verifies.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest
import stripe
from django.conf import settings
from django.core.management import call_command
from rest_framework.test import APIClient

from apps.rebilling import stripe_billing
from apps.rebilling.exceptions import StripeBillingError
from apps.rebilling.models import (
    CHARGING,
    FAILED,
    PAID,
    RETELL_LINE,
    SEND_INVOICE,
    TWILIO_LINE,
    UNCHARGED,
    Bill,
    BillLine,
    ClientMapping,
)

from .test_services import make_client

pytestmark = pytest.mark.django_db

MONTH = '2026-03'
URL = '/api/rebilling/webhooks/stripe/'


# --- fakes ------------------------------------------------------------------------------------


class FakeStripe:
    """Stands in for the SDK's class methods, recording what each was called with."""

    def __init__(self):
        self.calls = []
        self.customers = []
        self.invoice_id = 'in_test1'
        # A method name whose next call raises, and customers whose invoice Stripe refuses.
        self.fail_on = None
        self.fail_customers = set()

    def _record(self, name, kwargs):
        if self.fail_on == name or kwargs.get('customer') in self.fail_customers:
            raise stripe.APIConnectionError('Stripe is unreachable.')
        self.calls.append((name, kwargs))

    def customer_list(self, **kwargs):
        self._record('Customer.list', kwargs)
        return SimpleNamespace(data=list(self.customers))

    def customer_create(self, **kwargs):
        self._record('Customer.create', kwargs)
        return SimpleNamespace(id='cus_new')

    def invoice_create(self, **kwargs):
        self._record('Invoice.create', kwargs)
        return SimpleNamespace(id=self.invoice_id)

    def invoice_item_create(self, **kwargs):
        self._record('InvoiceItem.create', kwargs)
        return SimpleNamespace(id='ii_test1')

    def invoice_finalize(self, invoice_id, **kwargs):
        self._record('Invoice.finalize_invoice', {'invoice_id': invoice_id, **kwargs})
        return SimpleNamespace(id=invoice_id)

    def session_create(self, **kwargs):
        self._record('checkout.Session.create', kwargs)
        return SimpleNamespace(id='cs_test1', url='https://checkout.stripe.test/cs_test1')

    def of(self, name):
        """Every call made to one method, in order."""
        return [kwargs for called, kwargs in self.calls if called == name]

    def names(self):
        return [called for called, _ in self.calls]


@pytest.fixture
def fake(monkeypatch):
    faked = FakeStripe()
    monkeypatch.setattr(stripe.Customer, 'list', faked.customer_list)
    monkeypatch.setattr(stripe.Customer, 'create', faked.customer_create)
    monkeypatch.setattr(stripe.Invoice, 'create', faked.invoice_create)
    monkeypatch.setattr(stripe.InvoiceItem, 'create', faked.invoice_item_create)
    monkeypatch.setattr(stripe.Invoice, 'finalize_invoice', faked.invoice_finalize)
    monkeypatch.setattr(stripe.checkout.Session, 'create', faked.session_create)
    return faked


def make_billable_client(location_id='loc1', **overrides):
    fields = {'billing_email': 'ops@coolbreeze.test', **overrides}
    return make_client(location_id, **fields)


def make_bill(client, period=MONTH, retell_cents=500, twilio_cents=150, status=UNCHARGED):
    """A bill as Phase 3 leaves it: lines in integer cents, nothing charged yet."""
    bill = Bill.objects.create(
        client=client,
        billing_period=period,
        total_cents=retell_cents + twilio_cents,
        status=status,
    )
    BillLine.objects.create(
        bill=bill,
        line_type=RETELL_LINE,
        quantity_minutes=Decimal('20.00'),
        cost_cents=120,
        bill_cents=retell_cents,
    )
    BillLine.objects.create(
        bill=bill,
        line_type=TWILIO_LINE,
        quantity_minutes=Decimal('30.00'),
        cost_cents=90,
        bill_cents=twilio_cents,
    )
    return bill


# --- the Stripe customer ------------------------------------------------------------------------


def test_a_known_customer_is_never_looked_up_again(fake):
    client = make_billable_client(stripe_customer_id='cus_known')

    assert stripe_billing.customer_id(client) == 'cus_known'
    assert fake.calls == []


def test_an_existing_customer_is_found_by_email_rather_than_duplicated(fake):
    fake.customers = [SimpleNamespace(id='cus_existing')]
    client = make_billable_client()

    assert stripe_billing.customer_id(client) == 'cus_existing'
    assert fake.of('Customer.list') == [{'email': 'ops@coolbreeze.test', 'limit': 1}]
    assert fake.of('Customer.create') == []
    client.refresh_from_db()
    assert client.stripe_customer_id == 'cus_existing'


def test_a_customer_is_created_when_the_email_matches_nothing(fake):
    client = make_billable_client()

    assert stripe_billing.customer_id(client) == 'cus_new'
    assert fake.of('Customer.create') == [
        {
            'email': 'ops@coolbreeze.test',
            'name': 'Cool Breeze HVAC',
            'metadata': {'location_id': 'loc1'},
        }
    ]
    client.refresh_from_db()
    assert client.stripe_customer_id == 'cus_new'


def test_a_client_with_no_billing_email_cannot_be_billed(fake):
    client = make_client('loc1')

    with pytest.raises(StripeBillingError):
        stripe_billing.customer_id(client)
    assert fake.calls == []


def test_setup_link_returns_a_hosted_page_that_takes_no_payment(fake):
    client = make_billable_client(stripe_customer_id='cus_known')

    url = stripe_billing.setup_link(client)

    assert url == 'https://checkout.stripe.test/cs_test1'
    assert fake.of('checkout.Session.create') == [
        {
            'mode': 'setup',
            'customer': 'cus_known',
            'currency': 'usd',
            'success_url': f'{settings.FRONTEND_URL}/admin',
        }
    ]


# --- charging -----------------------------------------------------------------------------------


def test_an_uncharged_bill_becomes_a_finalised_stripe_invoice(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client)

    result = stripe_billing.charge_bills(MONTH)

    assert result == {
        'period': MONTH,
        'charged': 1,
        'skipped': 0,
        'failed': 0,
        'total_cents': 650,
    }
    invoice = fake.of('Invoice.create')[0]
    assert invoice['customer'] == 'cus_known'
    assert invoice['collection_method'] == 'charge_automatically'
    assert invoice['auto_advance'] is True
    assert invoice['metadata'] == {'bill_id': str(bill.pk), 'location_id': 'loc1'}

    bill.refresh_from_db()
    assert bill.status == CHARGING
    assert bill.stripe_invoice_id == 'in_test1'
    assert bill.paid_at is None


def test_each_bill_line_becomes_an_invoice_item_in_the_same_integer_cents(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    make_bill(client, retell_cents=500, twilio_cents=150)

    stripe_billing.charge_bills(MONTH)

    items = fake.of('InvoiceItem.create')
    assert [item['amount'] for item in items] == [500, 150]
    assert all(isinstance(item['amount'], int) for item in items)
    assert all(item['currency'] == 'usd' and item['invoice'] == 'in_test1' for item in items)
    assert items[0]['description'] == 'Voice AI minutes (20.00 min)'
    assert items[1]['description'] == 'Call minutes (30.00 min)'


def test_the_invoice_is_finalised_which_is_what_charges_the_card(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    make_bill(client)

    stripe_billing.charge_bills(MONTH)

    # The lines have to be on the invoice before it is finalised, or they are not billed.
    assert fake.names() == [
        'Invoice.create',
        'InvoiceItem.create',
        'InvoiceItem.create',
        'Invoice.finalize_invoice',
    ]
    assert fake.of('Invoice.finalize_invoice')[0]['invoice_id'] == 'in_test1'


def test_every_write_carries_an_idempotency_key_derived_from_the_bill(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client)

    stripe_billing.charge_bills(MONTH)

    keys = [kwargs['idempotency_key'] for _, kwargs in fake.calls]
    assert keys == [
        f'bill-{bill.pk}-invoice',
        f'bill-{bill.pk}-retell',
        f'bill-{bill.pk}-twilio',
        f'bill-{bill.pk}-finalize',
    ]


def test_running_the_month_again_charges_nothing_twice(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    make_bill(client)

    stripe_billing.charge_bills(MONTH)
    calls_after_first_run = len(fake.calls)
    second = stripe_billing.charge_bills(MONTH)

    assert second['charged'] == 0
    assert len(fake.calls) == calls_after_first_run


def test_a_client_not_on_auto_charge_is_skipped_rather_than_charged(fake):
    client = make_billable_client(stripe_customer_id='cus_known', billing_mode=SEND_INVOICE)
    bill = make_bill(client)

    result = stripe_billing.charge_bills(MONTH)

    assert result['skipped'] == 1
    assert result['charged'] == 0
    assert fake.calls == []
    bill.refresh_from_db()
    assert bill.status == UNCHARGED


def test_another_months_bill_is_left_alone(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    other = make_bill(client, period='2026-02')

    stripe_billing.charge_bills(MONTH)

    other.refresh_from_db()
    assert other.status == UNCHARGED
    assert fake.calls == []


def test_one_clients_failure_does_not_cost_the_others_their_charge(fake):
    first = make_billable_client('loc1', stripe_customer_id='cus_1')
    second = make_billable_client('loc2', stripe_customer_id='cus_2')
    bill_one = make_bill(first)
    bill_two = make_bill(second)
    fake.fail_customers = {'cus_1'}

    result = stripe_billing.charge_bills(MONTH)

    assert result['failed'] == 1
    assert result['charged'] == 1
    bill_one.refresh_from_db()
    bill_two.refresh_from_db()
    assert bill_one.status == UNCHARGED
    assert bill_two.status == CHARGING


def test_a_client_without_an_email_fails_only_its_own_bill(fake):
    make_bill(make_client('loc1'))
    make_bill(make_billable_client('loc2', stripe_customer_id='cus_2'))

    result = stripe_billing.charge_bills(MONTH)

    assert result['failed'] == 1
    assert result['charged'] == 1


def test_the_invoice_id_survives_a_failure_after_the_invoice_exists(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client)
    fake.fail_on = 'Invoice.finalize_invoice'

    result = stripe_billing.charge_bills(MONTH)

    assert result['failed'] == 1
    bill.refresh_from_db()
    # Still uncharged, but traceable to the Stripe object that was already created.
    assert bill.status == UNCHARGED
    assert bill.stripe_invoice_id == 'in_test1'


def test_the_command_reports_what_it_charged(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    make_bill(client)
    out = StringIO()

    call_command('charge_bills', '--month', MONTH, stdout=out)

    assert '1 bill(s) sent to Stripe' in out.getvalue()
    assert '650¢' in out.getvalue()


def test_the_command_rejects_a_month_it_cannot_read(fake):
    with pytest.raises(Exception, match='YYYY-MM'):
        call_command('charge_bills', '--month', 'March', stdout=StringIO())


# --- the webhook ----------------------------------------------------------------------------------


def sign(body: bytes, secret: str = settings.STRIPE_WEBHOOK_SECRET, timestamp=None) -> str:
    """The header Stripe sends: t=<epoch seconds>,v1=<HMAC-SHA256 of "timestamp.body">.

    Docs: https://docs.stripe.com/webhooks#verify-manually
    """
    if timestamp is None:
        timestamp = int(time.time())
    digest = hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={digest}'


@pytest.fixture
def api():
    return APIClient()


def event_body(kind='invoice.paid', invoice_id='in_test1', created=1772323200):
    return json.dumps(
        {
            'id': 'evt_test1',
            'type': kind,
            'created': created,
            'data': {'object': {'id': invoice_id, 'object': 'invoice'}},
        }
    ).encode()


def post(api, body, signature=None):
    return api.post(
        URL,
        data=body,
        content_type='application/json',
        HTTP_STRIPE_SIGNATURE=sign(body) if signature is None else signature,
    )


def charging_bill(invoice_id='in_test1'):
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client, status=CHARGING)
    bill.stripe_invoice_id = invoice_id
    bill.save(update_fields=['stripe_invoice_id'])
    return bill


def test_invoice_paid_marks_the_bill_paid_at_stripes_own_time(api):
    bill = charging_bill()

    response = post(api, event_body('invoice.paid', created=1772323200))

    assert response.status_code == 204
    assert response.content == b''
    bill.refresh_from_db()
    assert bill.status == PAID
    assert bill.paid_at == datetime(2026, 3, 1, tzinfo=UTC)


def test_invoice_payment_failed_marks_the_bill_failed(api):
    bill = charging_bill()

    response = post(api, event_body('invoice.payment_failed'))

    assert response.status_code == 204
    bill.refresh_from_db()
    assert bill.status == FAILED
    assert bill.paid_at is None


def test_a_bad_signature_is_rejected_and_changes_nothing(api):
    bill = charging_bill()

    response = post(api, event_body(), signature='t=1,v1=deadbeef')

    assert response.status_code == 401
    bill.refresh_from_db()
    assert bill.status == CHARGING


def test_a_signature_from_the_wrong_secret_is_rejected(api):
    charging_bill()
    body = event_body()

    response = post(api, body, signature=sign(body, secret='whsec_someone_else'))

    assert response.status_code == 401


def test_a_body_that_is_not_an_event_is_a_400(api):
    response = post(api, b'not json at all')

    assert response.status_code == 400


def test_an_event_for_an_invoice_we_did_not_send_is_ignored(api):
    bill = charging_bill('in_ours')

    response = post(api, event_body(invoice_id='in_somebody_elses'))

    assert response.status_code == 204
    bill.refresh_from_db()
    assert bill.status == CHARGING


def test_an_event_type_we_do_not_act_on_changes_nothing(api):
    bill = charging_bill()

    response = post(api, event_body('invoice.finalized'))

    assert response.status_code == 204
    bill.refresh_from_db()
    assert bill.status == CHARGING


def test_a_second_paid_event_is_harmless(api):
    bill = charging_bill()
    body = event_body('invoice.paid')

    assert post(api, body).status_code == 204
    assert post(api, body).status_code == 204

    bill.refresh_from_db()
    assert bill.status == PAID
    assert Bill.objects.filter(status=PAID).count() == 1


def test_no_client_mapping_is_created_by_any_of_this(api):
    charging_bill()
    post(api, event_body())

    assert ClientMapping.objects.count() == 1
