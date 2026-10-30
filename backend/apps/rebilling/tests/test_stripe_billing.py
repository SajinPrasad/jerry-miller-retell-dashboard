"""Phase 5: putting a calculated bill on a Stripe invoice and hearing back about it.

Every Stripe call is faked, and `never_reach_stripe` makes that a rule rather than a habit:
an unmocked call fails the test by name instead of quietly going out to Stripe. The webhook
signature is built by hand rather than with the SDK, so the test signs independently of the
code that verifies.
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

# The seam every Stripe request funnels through, whichever resource was asked for. Private, but
# it is the only place a guard can sit below all of them - see `never_reach_stripe`.
from stripe._http_client import HTTPClient

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


@pytest.fixture(autouse=True)
def never_reach_stripe(monkeypatch):
    """Fail loudly if anything in this module tries to actually call Stripe.

    `FakeStripe` only stands in for the methods it was told about, so a Stripe call newly added
    to the code reaches the network instead - which is how a test run ends up authenticating
    against the real Stripe with a test key. This shuts that door at the HTTP layer, below every
    resource, so an unmocked call fails by name instead of arriving as an auth error.
    """

    def blocked(self, method, url, *args, **kwargs):
        raise AssertionError(f'test tried to reach Stripe: {method.upper()} {url}')

    monkeypatch.setattr(HTTPClient, 'request_with_retries', blocked)
    monkeypatch.setattr(HTTPClient, 'request_stream_with_retries', blocked)


# A customer Stripe would charge: a card is set as the invoice default.
CHARGEABLE = SimpleNamespace(
    invoice_settings=SimpleNamespace(default_payment_method='pm_card1'), default_source=None
)
# A customer with no card at all - the shape that finalises fine and then fails at the card.
CARDLESS = SimpleNamespace(
    invoice_settings=SimpleNamespace(default_payment_method=None), default_source=None
)
# The older way a card is held. Stripe still collects against it, so this must pass too.
LEGACY_SOURCE = SimpleNamespace(
    invoice_settings=SimpleNamespace(default_payment_method=None), default_source='card_1'
)


class FakeStripe:
    """Stands in for the SDK's class methods, recording what each was called with."""

    def __init__(self):
        self.calls = []
        self.customers = []
        self.invoice_id = 'in_test1'
        # A method name whose next call raises, and customers whose invoice Stripe refuses.
        self.fail_on = None
        self.fail_customers = set()
        # What `Customer.retrieve` answers, per id. Anything unlisted is chargeable.
        self.customer_records = {}
        # What the payment intent behind a failed invoice says went wrong.
        self.payment_error = SimpleNamespace(code='card_declined')

    def _record(self, name, kwargs):
        target = kwargs.get('customer') or kwargs.get('customer_id')
        if self.fail_on == name or target in self.fail_customers:
            raise stripe.APIConnectionError('Stripe is unreachable.')
        self.calls.append((name, kwargs))

    def customer_list(self, **kwargs):
        self._record('Customer.list', kwargs)
        page = SimpleNamespace(data=list(self.customers))
        page.auto_paging_iter = lambda: iter(self.customers)
        return page

    def customer_retrieve(self, customer_id, **kwargs):
        self._record('Customer.retrieve', {'customer_id': customer_id, **kwargs})
        return self.customer_records.get(customer_id, CHARGEABLE)

    def payment_intent_retrieve(self, intent_id, **kwargs):
        self._record('PaymentIntent.retrieve', {'intent_id': intent_id, **kwargs})
        return SimpleNamespace(last_payment_error=self.payment_error)

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
    monkeypatch.setattr(stripe.Customer, 'retrieve', faked.customer_retrieve)
    monkeypatch.setattr(stripe.PaymentIntent, 'retrieve', faked.payment_intent_retrieve)
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


def test_a_customer_is_found_by_the_owners_email_and_remembered(fake):
    """The link is made from GoHighLevel's data. Nobody picks it and nobody types it."""
    fake.customers = [SimpleNamespace(id='cus_found')]
    client = make_billable_client()

    assert stripe_billing.customer_id(client) == 'cus_found'
    assert fake.of('Customer.list') == [{'email': 'ops@coolbreeze.test', 'limit': 1}]
    client.refresh_from_db()
    # Remembered, so a later charge costs no lookup.
    assert client.stripe_customer_id == 'cus_found'


def test_an_email_matching_no_customer_is_never_given_a_blank_one(fake):
    """A customer created here would have no card on it.

    That turns "we do not know who pays" into a charge that fails at the card for a reason
    nobody reading it could act on. The address that matched nothing is reported instead.
    """
    client = make_billable_client()

    with pytest.raises(
        StripeBillingError,
        match='No Stripe customer found for billing email: ops@coolbreeze.test',
    ):
        stripe_billing.customer_id(client)

    # Only the lookup happened. `Customer.create` is not even stood in for, so a call to it
    # would hit `never_reach_stripe` and fail this test by name.
    assert fake.names() == ['Customer.list']
    client.refresh_from_db()
    assert client.stripe_customer_id == ''


def test_a_client_with_no_billing_email_from_ghl_cannot_be_billed(fake):
    """No email means GoHighLevel has no owner for the sub-account, or the sync has not run."""
    client = make_client('loc1')
    make_bill(client)

    result = stripe_billing.charge_bills(MONTH)

    assert result['failed'] == 1
    assert fake.calls == []
    assert 'no billing email' in Bill.objects.get(client=client).failure_reason


def test_an_unmatched_email_puts_stripes_reason_on_the_bill(fake):
    """What the admin page shows against the client, and the only thing anyone can act on."""
    bill = make_bill(make_billable_client())

    stripe_billing.charge_bills(MONTH)

    bill.refresh_from_db()
    assert bill.status == UNCHARGED
    assert bill.failure_reason == (
        'No Stripe customer found for billing email: ops@coolbreeze.test'
    )


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
        'too_small': 0,
        'failed': 0,
        'total_cents': 650,
    }
    invoice = fake.of('Invoice.create')[0]
    assert invoice['customer'] == 'cus_known'
    assert invoice['collection_method'] == 'charge_automatically'
    # A draft Stripe will not advance by itself until the lines are on it.
    assert invoice['auto_advance'] is False
    assert invoice['pending_invoice_items_behavior'] == 'exclude'
    assert invoice['metadata'] == {'bill_id': str(bill.pk), 'location_id': 'loc1'}

    bill.refresh_from_db()
    assert bill.status == CHARGING
    assert bill.stripe_invoice_id == 'in_test1'
    assert bill.paid_at is None
    # The clock a bill stuck in `charging` is measured against.
    assert bill.charged_at is not None


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
        'Customer.retrieve',
        'Invoice.create',
        'InvoiceItem.create',
        'InvoiceItem.create',
        'Invoice.finalize_invoice',
    ]
    finalise = fake.of('Invoice.finalize_invoice')[0]
    assert finalise['invoice_id'] == 'in_test1'
    # Collection, and the retry schedule with it, is handed to Stripe only at this point.
    assert finalise['auto_advance'] is True


def test_every_write_carries_an_idempotency_key_derived_from_the_bill(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client)

    stripe_billing.charge_bills(MONTH)

    keys = [kwargs['idempotency_key'] for _, kwargs in fake.calls if 'idempotency_key' in kwargs]
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


def test_a_client_stripe_cannot_match_fails_only_its_own_bill(fake):
    make_bill(make_billable_client('loc1'))
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
    # And why it stopped, on the bill rather than only in the log.
    assert bill.failure_reason


def test_the_command_reports_what_it_charged(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    make_bill(client)
    out = StringIO()

    call_command('charge_bills', '--month', MONTH, stdout=out)

    assert '1 bill(s) sent to Stripe' in out.getvalue()
    assert '650¢' in out.getvalue()
    assert '0 under the minimum' in out.getvalue()


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


def event_body(kind='invoice.paid', invoice_id='in_test1', created=1772323200, intent=None):
    invoice = {'id': invoice_id, 'object': 'invoice'}
    if intent:
        invoice['payment_intent'] = intent
    return json.dumps(
        {'id': 'evt_test1', 'type': kind, 'created': created, 'data': {'object': invoice}}
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


# --- Stripe's minimum charge --------------------------------------------------------------------


def test_a_bill_under_stripes_minimum_is_left_alone_rather_than_attempted(fake):
    """Stripe will not charge a card for under 50c.

    Sending one anyway produces a failure that reads like a decline, that no retry can clear,
    and that nobody can act on - so the bill is left `uncharged` and counted instead.
    """
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client, retell_cents=30, twilio_cents=0)

    result = stripe_billing.charge_bills(MONTH)

    assert result['too_small'] == 1
    assert result['charged'] == 0
    assert fake.calls == []
    bill.refresh_from_db()
    assert bill.status == UNCHARGED
    assert bill.stripe_invoice_id == ''


def test_a_bill_exactly_at_the_minimum_is_charged(fake):
    client = make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client, retell_cents=50, twilio_cents=0)

    result = stripe_billing.charge_bills(MONTH)

    assert result['charged'] == 1
    assert result['too_small'] == 0
    bill.refresh_from_db()
    assert bill.status == CHARGING


def test_a_bill_too_small_to_charge_does_not_stop_the_others(fake):
    make_bill(
        make_billable_client('loc1', stripe_customer_id='cus_1'), retell_cents=10, twilio_cents=0
    )
    make_bill(make_billable_client('loc2', stripe_customer_id='cus_2'))

    result = stripe_billing.charge_bills(MONTH)

    assert result['too_small'] == 1
    assert result['charged'] == 1


# --- there has to be a card before an invoice is raised -------------------------------------


def test_a_customer_with_no_card_is_refused_before_an_invoice_exists(fake):
    """A cardless customer finalises fine and then fails at the card.

    That failure arrives looking exactly like a decline, when the real answer is that no card
    was ever attached - and GHL can attach one without making it the invoice default. Checking
    first keeps the reason honest and keeps a doomed invoice out of Jerry's Stripe account.
    """
    client = make_billable_client(stripe_customer_id='cus_nocard')
    bill = make_bill(client)
    fake.customer_records = {'cus_nocard': CARDLESS}

    result = stripe_billing.charge_bills(MONTH)

    assert result['failed'] == 1
    assert fake.of('Invoice.create') == []
    bill.refresh_from_db()
    assert bill.status == UNCHARGED
    assert bill.stripe_invoice_id == ''
    assert 'no default payment method' in bill.failure_reason


def test_a_card_held_the_older_way_still_counts_as_chargeable(fake):
    """Stripe falls back to `default_source`, so this check has to as well."""
    client = make_billable_client(stripe_customer_id='cus_legacy')
    make_bill(client)
    fake.customer_records = {'cus_legacy': LEGACY_SOURCE}

    assert stripe_billing.charge_bills(MONTH)['charged'] == 1


# --- what the webhook may and may not do ------------------------------------------------------


def test_a_late_failure_never_un_pays_a_collected_bill(api):
    """Stripe's own retries can collect a bill that already failed, and events are not ordered.

    A `payment_failed` that arrives after the money is in must not flip the bill back.
    """
    bill = charging_bill()
    post(api, event_body('invoice.paid'))

    response = post(api, event_body('invoice.payment_failed'))

    assert response.status_code == 204
    bill.refresh_from_db()
    assert bill.status == PAID
    assert bill.paid_at is not None


def test_a_failure_records_stripes_own_reason(api, fake):
    """`authentication_required` and `card_declined` arrive as the same event.

    Only the code separates "ask the client to confirm this card" from "get a new card".
    """
    bill = charging_bill()
    fake.payment_error = SimpleNamespace(code='authentication_required')

    post(api, event_body('invoice.payment_failed', intent='pi_test1'))

    bill.refresh_from_db()
    assert bill.status == FAILED
    assert bill.failure_reason == 'authentication_required'


def test_a_failure_whose_reason_cannot_be_read_is_still_recorded(api):
    bill = charging_bill()

    post(api, event_body('invoice.payment_failed'))

    bill.refresh_from_db()
    assert bill.status == FAILED
    assert bill.failure_reason == 'payment_failed'


def test_being_paid_clears_the_reason_an_earlier_attempt_failed(api, fake):
    bill = charging_bill()
    post(api, event_body('invoice.payment_failed', intent='pi_test1'))

    post(api, event_body('invoice.paid'))

    bill.refresh_from_db()
    assert bill.status == PAID
    assert bill.failure_reason == ''


# --- the guard under all of it ------------------------------------------------------------------


def test_an_unmocked_stripe_call_fails_the_test_rather_than_going_out():
    """What stops a newly added Stripe call from quietly reaching the real API in a test run.

    Without this, an unmocked call authenticates against Stripe with the test key and fails
    somewhere far from the cause - which is exactly how it behaved before the guard.
    """
    stripe_billing._configure()

    with pytest.raises(AssertionError, match='tried to reach Stripe'):
        stripe.Customer.retrieve('cus_unmocked')
