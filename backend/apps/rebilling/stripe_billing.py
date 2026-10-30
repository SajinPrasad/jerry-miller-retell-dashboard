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
from django.utils import timezone

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

# Stripe will not charge a card for less than this. A bill under it is left uncharged rather
# than sent: finalising one produces a failure that reads like a decline, that no retry can ever
# clear, and that nobody can act on. Stripe's figure for USD, which is the only currency here.
MINIMUM_CHARGE_CENTS = 50

# What a failed charge is recorded as when Stripe's own code for it cannot be read.
FALLBACK_FAILURE_REASON = 'payment_failed'


def _configure() -> None:
    """Point the SDK at the account the bills are charged through.

    Read from settings at call time rather than at import, so the test settings and a key
    rotated in the environment both take effect without reloading the module.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY


def customer_id(client: ClientMapping) -> str:
    """The Stripe customer this client's bills are charged to, matched by their owner's email.

    Nobody picks this and nobody types it. The address comes from the sub-account's owner in
    GoHighLevel, and the customer it matches on Jerry's Stripe account - the same account
    GoHighLevel is connected to, so the card is already on it - is remembered the first time, so
    later charges cost no lookup.

    **A customer that matches nothing is never created.** A customer created here would have no
    card on it, which turns "we do not know who pays" into a charge that fails at the card for a
    reason nobody reading it could act on. Refusing instead puts the address that matched
    nothing on the bill, where the admin page shows it against the client.
    """
    if client.stripe_customer_id:
        return client.stripe_customer_id

    if not client.billing_email:
        raise StripeBillingError(
            f'{client.location_id} has no billing email from GoHighLevel to match a customer by.'
        )

    _configure()
    found = stripe.Customer.list(email=client.billing_email, limit=1)
    if not found.data:
        raise StripeBillingError(
            f'No Stripe customer found for billing email: {client.billing_email}'
        )

    client.stripe_customer_id = found.data[0].id
    client.save(update_fields=['stripe_customer_id'])
    return client.stripe_customer_id


def _assert_chargeable(customer: str) -> None:
    """Refuse to raise an invoice for a customer Stripe has no card to charge.

    `charge_automatically` collects against the customer's default payment method, falling back
    to the legacy `default_source`. With neither set, the invoice still finalises and the
    payment then fails at once - and that failure reads exactly like a decline, when the real
    answer is that no card was ever attached. GHL can attach a card without making it the
    invoice default, so this is not a rare case on a shared account.

    Nothing is chosen or written here. The card Stripe would pick is still the card Stripe
    picks; this only asks whether one exists, before a doomed invoice is created.
    """
    _configure()
    record = stripe.Customer.retrieve(customer)
    invoice_settings = getattr(record, 'invoice_settings', None)
    default_method = getattr(invoice_settings, 'default_payment_method', None)
    if default_method or getattr(record, 'default_source', None):
        return
    raise StripeBillingError(
        f'Stripe customer {customer} has no default payment method, so nothing can be charged.'
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

    A bill under Stripe's minimum is counted in `too_small` and left `uncharged`: there is
    nothing to retry and nothing to fix - see `MINIMUM_CHARGE_CENTS`.
    """
    start, _ = month_window(month)
    period = f'{start.year:04d}-{start.month:02d}'

    result = {
        'period': period,
        'charged': 0,
        'skipped': 0,
        'too_small': 0,
        'failed': 0,
        'total_cents': 0,
    }
    bills = Bill.objects.filter(billing_period=period, status=UNCHARGED).select_related('client')

    for bill in bills:
        if bill.client.billing_mode != AUTO_CHARGE:
            result['skipped'] += 1
            continue
        if bill.total_cents < MINIMUM_CHARGE_CENTS:
            logger.info(
                'billing: %s owes %s cents for %s, under the %s-cent Stripe minimum; '
                'left uncharged',
                bill.client.location_id,
                bill.total_cents,
                period,
                MINIMUM_CHARGE_CENTS,
            )
            result['too_small'] += 1
            continue
        try:
            _charge_one(bill)
        except (StripeBillingError, stripe.StripeError) as exc:
            logger.warning(
                'billing: could not charge %s for %s',
                bill.client.location_id,
                period,
                exc_info=True,
            )
            # On the bill, not only in the log: "why was this one not charged" is a question
            # the admin page and Phase 6's alerting both have to answer later.
            bill.failure_reason = str(exc)[:255]
            bill.save(update_fields=['failure_reason'])
            result['failed'] += 1
            continue
        result['charged'] += 1
        result['total_cents'] += bill.total_cents

    return result


def _charge_one(bill: Bill) -> None:
    """Put one bill on a Stripe invoice and finalise it, which is what starts the charge.

    The invoice id is saved the moment it exists, before the lines are added, so a failure
    halfway through leaves a bill that can be traced to a real Stripe object rather than an
    orphan nobody can find.
    """
    customer = customer_id(bill.client)
    _assert_chargeable(customer)
    key = f'bill-{bill.pk}'

    invoice = stripe.Invoice.create(
        customer=customer,
        collection_method='charge_automatically',
        # A draft Stripe will not advance on its own. The lines go on next, and only the
        # explicit finalise below starts collection. With `auto_advance=True` here instead, a
        # crash between this call and the last line would leave Stripe to finalise a part-built
        # invoice an hour later by itself - charging the client for some of the month's usage
        # while the bill here still read `uncharged`.
        auto_advance=False,
        # This bill's lines and nothing else. The account is shared with Jerry's other billing,
        # so a pending invoice item left on the customer must never ride along on a voice
        # invoice. Stripe's default is already `exclude`; saying it means a change to that
        # default cannot quietly put someone else's charge on ours.
        pending_invoice_items_behavior='exclude',
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

    # Finalising a `charge_automatically` invoice is what makes Stripe attempt the card, and
    # `auto_advance` is what hands the retry schedule to Stripe from this point on.
    stripe.Invoice.finalize_invoice(
        invoice.id, auto_advance=True, idempotency_key=f'{key}-finalize'
    )

    bill.status = CHARGING
    # The card has now been attempted. Only the webhook moves this bill on from here, so this
    # stamp is what makes a webhook that never arrives visible as a bill stuck in `charging`.
    bill.charged_at = timezone.now()
    bill.failure_reason = ''
    bill.save(update_fields=['status', 'charged_at', 'failure_reason'])
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
        # Stripe's own retries can collect a bill that already failed once, so a paid bill must
        # not keep the reason the earlier attempt failed.
        bill.failure_reason = ''
    elif bill.status == PAID:
        # Webhooks are not ordered, and a failed attempt Stripe later collected can still be
        # redelivered afterwards. Money already taken is never un-taken on an older event.
        logger.info(
            'billing: %s arrived for already-paid bill %s, ignored', kind, bill.billing_period
        )
        return False
    else:
        bill.status = FAILED
        bill.failure_reason = _failure_reason(event)
        logger.warning(
            'billing: charge failed for %s, bill %s (%s)',
            bill.client.location_id,
            bill.billing_period,
            bill.failure_reason,
        )

    bill.save(update_fields=['status', 'paid_at', 'failure_reason'])
    return True


def _failure_reason(event) -> str:
    """Stripe's own code for why the card was not charged.

    `authentication_required` - the card needs its holder present to re-authorise it - and
    `card_declined` both arrive as the same `invoice.payment_failed`, and only this code tells
    them apart. One needs the client to confirm a payment, the other needs a new card entirely.
    The code lives on the payment intent rather than on the invoice, so this reads it: one
    call, only ever on the failure path, and the bill is still marked failed if it cannot be
    read.
    """
    intent_id = getattr(event['data']['object'], 'payment_intent', None)
    if not intent_id:
        return FALLBACK_FAILURE_REASON
    try:
        _configure()
        intent = stripe.PaymentIntent.retrieve(intent_id)
    except stripe.StripeError:
        logger.warning('billing: could not read payment intent %s', intent_id, exc_info=True)
        return FALLBACK_FAILURE_REASON

    error = getattr(intent, 'last_payment_error', None)
    return (getattr(error, 'code', None) or FALLBACK_FAILURE_REASON)[:255]
