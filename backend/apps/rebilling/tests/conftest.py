"""Shared fixtures for the rebilling tests: the Stripe and GoHighLevel fakes, and the API client.

Nothing a test in this package does may reach Stripe or GoHighLevel. `never_reach_stripe` makes
an unfaked Stripe call fail by name, `fake` stands in for the Stripe methods the code uses, and
`ghl_contact_writes` and `ghl_contact_tags` record writes to a client's GoHighLevel contact
instead of sending them.
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
        # Payment methods `PaymentMethod.list` answers with, per customer id. Anything unlisted
        # has no attached card at all.
        self.attached_cards = {}
        # What Invoice.retrieve answers for the status of an invoice, and whether Invoice.pay
        # collects. Retries start from an open invoice that Stripe has not collected yet.
        self.invoice_status = 'open'
        self.pay_error = None
        # Whether `Invoice.pay` collects when it does not raise. False is how Stripe answers a
        # payment the client has to act on themselves: no error, invoice still open.
        self.pay_collects = True
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

    def payment_method_list(self, **kwargs):
        self._record('PaymentMethod.list', kwargs)
        cards = self.attached_cards.get(kwargs.get('customer'), [])
        return SimpleNamespace(data=list(cards))

    def customer_modify(self, customer_id, **kwargs):
        self._record('Customer.modify', {'customer_id': customer_id, **kwargs})
        return SimpleNamespace(id=customer_id)

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
        if not self.pay_collects:
            return SimpleNamespace(id=invoice_id, status=self.invoice_status)
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
    monkeypatch.setattr(stripe.Customer, 'modify', faked.customer_modify)
    monkeypatch.setattr(stripe.PaymentIntent, 'retrieve', faked.payment_intent_retrieve)
    monkeypatch.setattr(stripe.PaymentMethod, 'list', faked.payment_method_list)
    monkeypatch.setattr(stripe.Invoice, 'create', faked.invoice_create)
    monkeypatch.setattr(stripe.InvoiceItem, 'create', faked.invoice_item_create)
    monkeypatch.setattr(stripe.Invoice, 'finalize_invoice', faked.invoice_finalize)
    monkeypatch.setattr(stripe.Invoice, 'retrieve', faked.invoice_retrieve)
    monkeypatch.setattr(stripe.Invoice, 'pay', faked.invoice_pay)
    monkeypatch.setattr(stripe.checkout.Session, 'create', faked.session_create)
    return faked


class _ContactWrites(list):
    """The recorded contact field writes, with a switch for making the next one fail."""

    fails_with = None


@pytest.fixture(autouse=True)
def ghl_contact_writes(monkeypatch):
    """Record every custom field write to a client's GoHighLevel billing contact.

    The rebilling status is a custom field on that one contact. Autouse because the write is a
    search-then-update over HTTP, and one made in a test that forgot to fake it would go to the
    real API. The recorded `(email, field_id, value)` triples are what the tests assert on;
    `fails_with` makes the next write raise, which is how the "GoHighLevel refused it" paths
    are reached.
    """
    writes = _ContactWrites()

    def record(self, email, field_id, value):
        if writes.fails_with is not None:
            raise writes.fails_with
        writes.append((email, field_id, value))

    monkeypatch.setattr(GHLOAuthService, 'set_contact_field', record)
    return writes


class _ContactTags(list):
    """The recorded contact tags, with a switch for making the next add fail."""

    fails_with = None


@pytest.fixture(autouse=True)
def ghl_contact_tags(monkeypatch):
    """Record every tag added to a client's GoHighLevel billing contact instead of sending it.

    Asking a client for a card adds a tag rather than writing a custom field, and that add is
    the same search-then-write over HTTP, so it is faked here for the same reason and autouse
    for the same reason. The recorded `(email, tag)` pairs are what the tests assert on.
    """
    tags = _ContactTags()

    def record(self, email, tag):
        if tags.fails_with is not None:
            raise tags.fails_with
        tags.append((email, tag))

    monkeypatch.setattr(GHLOAuthService, 'add_contact_tag', record)
    return tags


class _CustomValueWrites(list):
    """The recorded custom value writes, and the sub-account they are written into.

    `fails_with` makes the next write raise; `lookup_fails_with` makes the lookup before it
    raise, which is the "the app was never installed on that sub-account" path. `found` is what
    the lookup answers - set it to `None` for a sub-account with no such custom value at all.
    `live_value` is what GoHighLevel currently holds, which is what a pause captures.
    """

    def __init__(self):
        super().__init__()
        self.fails_with = None
        self.lookup_fails_with = None
        self.found = {'id': 'cv_agent1', 'name': 'retell_agent_id', 'value': ''}

    @property
    def live_value(self) -> str:
        return (self.found or {}).get('value', '')

    @live_value.setter
    def live_value(self, value: str) -> None:
        self.found = {'id': 'cv_agent1', 'name': 'retell_agent_id', 'value': value}


@pytest.fixture(autouse=True)
def ghl_custom_value_writes(monkeypatch):
    """Stand in for the client's own sub-account custom values: the lookup and the write.

    These live in the client's own sub-account rather than the main account's billing contact,
    and reaching them for real would additionally need a `GHLOAuthToken` row for that
    sub-account, which most rebilling tests never create. Autouse for the same reason
    `ghl_contact_writes` is: a call a test forgot to fake must not reach the real API. The
    recorded `(location_id, custom_value_id, value)` triples are what the tests assert on.
    """
    writes = _CustomValueWrites()

    def find(self, location_id, name):
        if writes.lookup_fails_with is not None:
            raise writes.lookup_fails_with
        return writes.found

    def record(self, location_id, custom_value_id, value, name=''):
        if writes.fails_with is not None:
            raise writes.fails_with
        writes.append((location_id, custom_value_id, value))

    monkeypatch.setattr(GHLOAuthService, 'find_custom_value', find)
    monkeypatch.setattr(GHLOAuthService, 'set_custom_value', record)
    return writes
