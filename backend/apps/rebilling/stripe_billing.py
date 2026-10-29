"""Phase 5: charging a calculated bill through Stripe, off-session.

Phase 3 stops at the number. This starts from it and never recomputes it: `BillLine.bill_cents`
is an integer number of cents and so is Stripe's `amount`, so a bill reaches the card without a
single conversion on the way.

Nothing here charges twice. Every write carries an idempotency key derived from the `Bill`, so a
re-run of the monthly command re-reaches the same Stripe objects instead of making new ones, and
only bills still `uncharged` are picked up at all.

The charge is started here and answered somewhere else: Stripe attempts payment when the invoice
is finalised and tells us how it went by webhook. That is why `charging` is a real state rather
than a moment.

Docs: https://docs.stripe.com/invoicing/integration
      https://docs.stripe.com/invoicing/automatic-charging
"""

import logging
from datetime import UTC, datetime

import stripe
from django.conf import settings

from .exceptions import StripeBillingError
from .models import (
    AUTO_CHARGE,
    CHARGING,
    FAILED,
    PAID,
    RETELL_LINE,
    TWILIO_LINE,
    UNCHARGED,
    Bill,
    ClientMapping,
)
from .services import month_window

logger = logging.getLogger(__name__)

# Every amount sent to Stripe is an integer number of cents of this currency, which is the one
# Jerry bills in. Not a setting: it does not vary by environment, it varies by business.
CURRENCY = 'usd'

# What each line is called on the client's invoice. The client sees these words, so they say
# what was bought rather than which vendor supplied it.
LINE_DESCRIPTIONS = {
    RETELL_LINE: 'Voice AI minutes',
    TWILIO_LINE: 'Call minutes',
}

# The two Stripe events that decide a bill's fate. Everything else Stripe sends is ignored.
INVOICE_PAID = 'invoice.paid'
INVOICE_PAYMENT_FAILED = 'invoice.payment_failed'


def _configure() -> None:
    """Point the SDK at the account the bills are charged through.

    Read from settings at call time rather than at import, so the test settings and a key
    rotated in the environment both take effect without reloading the module.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY


def customer_id(client: ClientMapping) -> str:
    """The client's Stripe customer id, found by email the first time and remembered after.

    Looked up before being created: a client who already pays Jerry through this Stripe account
    must not end up with a second customer record, because the card is on the first one.
    """
    if client.stripe_customer_id:
        return client.stripe_customer_id

    if not client.billing_email:
        raise StripeBillingError(f'{client.location_id} has no billing email to bill.')

    _configure()
    found = stripe.Customer.list(email=client.billing_email, limit=1)
    customer = found.data[0] if found.data else _create_customer(client)

    client.stripe_customer_id = customer.id
    client.save(update_fields=['stripe_customer_id'])
    return customer.id


def _create_customer(client: ClientMapping):
    return stripe.Customer.create(
        email=client.billing_email,
        name=client.client_name,
        # So a customer opened in the Stripe dashboard can be traced back to a sub-account.
        metadata={'location_id': client.location_id},
    )


def setup_link(client: ClientMapping) -> str:
    """A Stripe-hosted page where the client attaches a card, for when there is none on file.

    Setup mode takes no payment. It exists because an off-session charge needs a payment method
    that was agreed to on-session once, and this is that once.
    """
    _configure()
    session = stripe.checkout.Session.create(
        mode='setup',
        customer=customer_id(client),
        currency=CURRENCY,
        success_url=f'{settings.FRONTEND_URL}/admin',
    )
    return session.url


def charge_bills(month: str | None = None) -> dict:
    """Charge every uncharged bill for one calendar month. Absent `month` means this month.

    A client whose `billing_mode` is not `auto_charge` is skipped rather than charged: that
    field is how someone says "do not take this one's card", and it has to mean it. One client's
    failure is logged and counted, never allowed to end the run for the others.
    """
    start, _ = month_window(month)
    period = f'{start.year:04d}-{start.month:02d}'

    result = {'period': period, 'charged': 0, 'skipped': 0, 'failed': 0, 'total_cents': 0}
    bills = Bill.objects.filter(billing_period=period, status=UNCHARGED).select_related('client')

    for bill in bills:
        if bill.client.billing_mode != AUTO_CHARGE:
            result['skipped'] += 1
            continue
        try:
            charge_on_stripe(bill)
        except (StripeBillingError, stripe.StripeError):
            logger.warning(
                'billing: could not charge %s for %s',
                bill.client.location_id,
                period,
                exc_info=True,
            )
            result['failed'] += 1
            continue
        result['charged'] += 1
        result['total_cents'] += bill.total_cents

    return result


def charge_on_stripe(bill: Bill) -> None:
    """Put one bill on a Stripe invoice and finalise it, which is what starts the charge.

    Called both by this module's own monthly run and by `dual_rail` when a GHL invoice has gone
    unpaid long enough to fall back, which is why it is named rather than private.

    The invoice id is saved the moment it exists, before the lines are added, so a failure
    halfway through leaves a bill that can be traced to a real Stripe object rather than an
    orphan nobody can find.
    """
    customer = customer_id(bill.client)
    key = f'bill-{bill.pk}'

    invoice = stripe.Invoice.create(
        customer=customer,
        collection_method='charge_automatically',
        # Stripe collects and, on a failure, runs its own retry schedule.
        auto_advance=True,
        description=f'Voice usage, {bill.billing_period}',
        metadata={'bill_id': str(bill.pk), 'location_id': bill.client.location_id},
        idempotency_key=f'{key}-invoice',
    )
    bill.stripe_invoice_id = invoice.id
    bill.save(update_fields=['stripe_invoice_id'])

    for line in bill.lines.all():
        stripe.InvoiceItem.create(
            customer=customer,
            invoice=invoice.id,
            currency=CURRENCY,
            # Already cents. Phase 3's integer is Stripe's integer.
            amount=line.bill_cents,
            description=f'{LINE_DESCRIPTIONS[line.line_type]} ({line.quantity_minutes} min)',
            idempotency_key=f'{key}-{line.line_type}',
        )

    # Finalising a `charge_automatically` invoice is what makes Stripe attempt the card.
    stripe.Invoice.finalize_invoice(invoice.id, idempotency_key=f'{key}-finalize')

    bill.status = CHARGING
    bill.save(update_fields=['status'])
    logger.info(
        'billing: charging %s for %s on %s',
        bill.client.location_id,
        bill.billing_period,
        invoice.id,
    )


def read_event(payload: bytes, signature: str):
    """Verify a webhook's signature and return the event it carries.

    Raises `stripe.SignatureVerificationError` if the signature is wrong or too old, `ValueError`
    if the body is not an event at all. The view turns those into 401 and 400.
    """
    return stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)


def apply_event(event) -> bool:
    """Record what Stripe says happened to a bill. True if a bill was updated.

    An event about an invoice we did not send is not an error: Stripe delivers every event the
    endpoint is subscribed to, and this account may be charging for other things too.
    """
    kind = event['type']
    if kind not in (INVOICE_PAID, INVOICE_PAYMENT_FAILED):
        return False

    # An invoice event always carries the invoice, and the SDK hands it over as a Stripe object
    # rather than a plain dict, so this reads it by attribute.
    invoice_id = event['data']['object'].id
    bill = Bill.objects.filter(stripe_invoice_id=invoice_id).first()
    if bill is None:
        logger.info('billing: %s for unknown invoice %s, ignored', kind, invoice_id)
        return False

    if kind == INVOICE_PAID:
        bill.status = PAID
        # Stripe's own timestamp, not ours: a webhook can be retried hours after the payment.
        bill.paid_at = datetime.fromtimestamp(event['created'], tz=UTC)
    else:
        bill.status = FAILED
        logger.warning(
            'billing: charge failed for %s, bill %s', bill.client.location_id, bill.billing_period
        )

    bill.save(update_fields=['status', 'paid_at'])
    return True
