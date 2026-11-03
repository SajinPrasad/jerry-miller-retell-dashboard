"""Shared fixtures for the rebilling tests: the Stripe and GoHighLevel fakes, and the API client.

Nothing a test in this package does may reach Stripe or GoHighLevel. `never_reach_stripe` makes
an unfaked Stripe call fail by name, `fake` stands in for the Stripe methods the code uses, and
`ghl_writes` records status writes to a client's GoHighLevel custom value instead of sending them.
"""

from types import SimpleNamespace

import pytest
import stripe
from rest_framework.test import APIClient

# The seam every Stripe request funnels through, whichever resource was asked for. Private, but
# it is the only place a guard can sit below all of them - see `never_reach_stripe`.
from stripe._http_client import HTTPClient

from apps.ghl.services import GHLOAuthService


@pytest.fixture
def api():
    return APIClient()


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
        # What Invoice.retrieve answers for the status of an invoice, and whether Invoice.pay
        # collects. Retries start from an open invoice that Stripe has not collected yet.
        self.invoice_status = 'open'
        self.pay_error = None
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

    def invoice_retrieve(self, invoice_id, **kwargs):
        self._record('Invoice.retrieve', {'invoice_id': invoice_id, **kwargs})
        return SimpleNamespace(id=invoice_id, status=self.invoice_status)

    def invoice_pay(self, invoice_id, **kwargs):
        self._record('Invoice.pay', {'invoice_id': invoice_id, **kwargs})
        if self.pay_error is not None:
            raise self.pay_error
        self.invoice_status = 'paid'
        return SimpleNamespace(id=invoice_id, status='paid')

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
    monkeypatch.setattr(stripe.Invoice, 'retrieve', faked.invoice_retrieve)
    monkeypatch.setattr(stripe.Invoice, 'pay', faked.invoice_pay)
    monkeypatch.setattr(stripe.checkout.Session, 'create', faked.session_create)
    return faked


@pytest.fixture(autouse=True)
def ghl_writes(monkeypatch):
    """Record every write to a client's GoHighLevel custom value instead of sending it.

    Nothing in this module may reach GoHighLevel: a status write is a list-then-update over
    HTTP, and an unfaked one would go out to the real API. The recorded `(location, value)`
    pairs are what the tests assert on.
    """
    writes = []

    def record(self, location_id, name, value):
        writes.append((location_id, name, value))

    monkeypatch.setattr(GHLOAuthService, 'set_custom_value', record)
    return writes


class _ContactWrites(list):
    """The recorded contact field writes, with a switch for making the next one fail."""

    fails_with = None


@pytest.fixture(autouse=True)
def ghl_contact_writes(monkeypatch):
    """The same for the card status field on a client's billing contact.

    Autouse for the same reason as `ghl_writes`: the write is a search-then-update over HTTP,
    and a card request made in a test that forgot to fake it would go to the real API. The
    recorded `(email, field_id, value)` triples are what the tests assert on; `fails_with` makes
    the next write raise, which is how the "GoHighLevel refused it" paths are reached.
    """
    writes = _ContactWrites()

    def record(self, email, field_id, value):
        if writes.fails_with is not None:
            raise writes.fails_with
        writes.append((email, field_id, value))

    monkeypatch.setattr(GHLOAuthService, 'set_contact_field', record)
    return writes
