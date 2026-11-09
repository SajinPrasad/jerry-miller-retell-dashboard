"""Phase 5: charging a calculated bill through Stripe, off-session - and whether we can.

Phase 7 added the second half: whether a client has a card Stripe would collect against, read
on the client's own page rather than discovered at month end, and the request that asks them for
one through GoHighLevel.


Phase 3 stops at the number. This starts from it and never recomputes it: `BillLine.bill_cents`
is an integer number of cents and so is Stripe's `amount`, so a bill reaches the card without a
single conversion on the way.

Nothing here charges twice. Every write carries an idempotency key derived from the `Bill`, so a
re-run of the monthly command re-reaches the same Stripe objects instead of making new ones, and
only bills still `uncharged` are picked up at all.

The charge is made here and confirmed somewhere else. Finalising the invoice hands collection to
Stripe, but Stripe's own first attempt is about an hour later, so the card is taken immediately
afterwards (`_collect_now`) and the webhook remains the authority on what happened. That is why
`charging` is still a real state rather than a moment: it is now what a bill Stripe did not
answer for during the run sits in.

Docs: https://docs.stripe.com/invoicing/integration
      https://docs.stripe.com/invoicing/automatic-charging
"""

import logging
from datetime import UTC, datetime, timedelta

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.ghl.exceptions import GHLError
from apps.ghl.services import GHLOAuthService

from .exceptions import ManualRetryUnavailable, StripeBillingError
from .models import (
    AUTO_CHARGE,
    CHARGING,
    FAILED,
    PAID,
    RETELL_LINE,
    RETRYING,
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

# What a retry is recorded as when Stripe answered without an error but the invoice is still not
# paid, which is how Stripe reports a payment that needs the client to act.
NOT_COLLECTED_REASON = 'not_collected'

# The values written to the rebilling status field on the client's billing contact in
# GoHighLevel. That field (`Retell Voice AI ON/OFF`) is a SINGLE_OPTIONS picklist whose
# `picklistOptions` are exactly `["ON", "OFF"]`, confirmed against the live field, so these are
# not free text: anything else is not one of the options the field accepts, and Jerry's GHL
# workflows key off these values too. The case matters.
GHL_STATUS_FAILED = 'OFF'
GHL_STATUS_ACTIVE = 'ON'

# The tag added to that same contact to ask a client for a card. Adding it is what triggers
# the GoHighLevel workflow that emails them a card collection form, so the word has to be
# exactly the one that workflow's trigger is set to. A tag rather than a custom field: there is
# no id to create in GoHighLevel and configure here, so nothing to keep in step.
GHL_CARD_REQUEST_TAG = 'collect card information'

# Whether Stripe holds a card we could charge this client with. A card GHL attached without
# making it the invoice default is promoted to the default the first time either is asked
# (`_promote_attached_card`), so `no_card` means Stripe truly has nothing to collect against.
CARD_ON_FILE = 'card_on_file'
NO_CARD = 'no_card'
NO_CUSTOMER = 'no_customer'
NO_BILLING_EMAIL = 'no_billing_email'
CARD_UNKNOWN = 'unknown'


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


def customer_facts(client: ClientMapping) -> dict:
    """Who Stripe says pays for this client, and whether it holds a card we could charge.

    One Stripe call answers both halves of the billing contact card: the customer's own name -
    the id means nothing to a reader of the admin page - and the card behind it.

    **The card is read here rather than remembered on the client.** A saved answer about whether
    money can be taken is wrong from the moment the client enters a card, which is minutes after
    being asked for one. Stripe being unreachable is `unknown` and never `no_card`, because a
    page that invites someone to chase a client who has a perfectly good card is worse than one
    that admits it could not check.
    """
    if not client.stripe_customer_id and not client.billing_email:
        # Nothing to look a customer up by. The page already says the sub-account has no owner
        # email in GoHighLevel; this is the card row agreeing rather than guessing.
        return _facts('', '', NO_BILLING_EMAIL)

    try:
        customer = customer_id(client)
    except StripeBillingError:
        return _facts('', '', NO_CUSTOMER)
    except stripe.StripeError:
        logger.exception('stripe: could not match a customer for %s', client.location_id)
        return _facts('', '', CARD_UNKNOWN)

    _configure()
    try:
        record = stripe.Customer.retrieve(
            # Expanded so the card's brand and last four arrive on the call that was being made
            # for the name anyway, rather than on a second one for the payment method.
            customer,
            expand=['invoice_settings.default_payment_method'],
        )
    except stripe.StripeError:
        logger.exception('stripe: could not read customer %s', customer)
        return _facts(customer, '', CARD_UNKNOWN)

    status, brand, last4 = _card(record, customer)
    return _facts(customer, getattr(record, 'name', None) or '', status, brand, last4)


def _facts(customer: str, name: str, status: str, brand: str = '', last4: str = '') -> dict:
    return {
        'stripe_customer_id': customer,
        'stripe_customer_name': name,
        'card': {'status': status, 'brand': brand, 'last4': last4},
    }


def _card(record, customer: str) -> tuple[str, str, str]:
    """The card Stripe would collect against, tested exactly as `_assert_chargeable` tests it."""
    invoice_settings = getattr(record, 'invoice_settings', None)
    method = getattr(invoice_settings, 'default_payment_method', None)
    if method is None:
        # The older way a card is held. Stripe still collects against it, so it counts - there
        # is just no payment method object behind it to read a brand and last four off.
        if getattr(record, 'default_source', None):
            return CARD_ON_FILE, '', ''
        method = _promote_attached_card(customer)
        if method is None:
            return NO_CARD, '', ''

    card = getattr(method, 'card', None)
    return CARD_ON_FILE, getattr(card, 'brand', '') or '', getattr(card, 'last4', '') or ''


def _promote_attached_card(customer: str):
    """A card GoHighLevel attached to the customer without making it the invoice default.

    `charge_automatically` only ever collects against the invoice default (or the legacy
    `default_source`), so a card sitting on the customer unattached to either is one Stripe
    would not actually charge. GoHighLevel attaches a card this way, so refusing to charge, or
    reporting no card, would be wrong about a client who has a perfectly good one - the first
    attached card becomes the invoice default here, the one time either caller needs to know
    about it, and every read after this one finds it as the default already.
    """
    try:
        found = stripe.PaymentMethod.list(customer=customer, type='card', limit=1)
        if not found.data:
            return None
        method = found.data[0]
        stripe.Customer.modify(customer, invoice_settings={'default_payment_method': method.id})
    except stripe.StripeError:
        # A transient network error or rate limit, not Stripe's answer about the card. Logged
        # and swallowed rather than raised, so it degrades to whatever the caller already does
        # when there is nothing to promote - the same way a down Stripe degrades elsewhere here.
        logger.warning('stripe: could not promote an attached card for %s', customer, exc_info=True)
        return None
    return method


def request_card(client: ClientMapping) -> None:
    """Ask a client for a card, by tagging their contact in GoHighLevel.

    Adding `GHL_CARD_REQUEST_TAG` is what starts Jerry's GoHighLevel workflow, which emails the
    client the form they enter a card on. Nothing about the card itself happens here - the form
    and the card entry both live in GoHighLevel and Stripe.

    **Not best effort**, unlike `sync_voice_routing`: an admin pressed a button, so a
    GoHighLevel failure is raised for them to see rather than logged, or they would walk away
    believing a client had been asked who never was.
    """
    if not client.billing_email:
        raise GHLError(
            f'{client.location_id} has no billing email in GoHighLevel to find a contact by.'
        )

    GHLOAuthService().add_contact_tag(client.billing_email, GHL_CARD_REQUEST_TAG)
    client.card_requested_at = timezone.now()
    client.save(update_fields=['card_requested_at'])


def clear_card_request(client: ClientMapping) -> bool:
    """Re-read Stripe after GoHighLevel says a card was saved. True once there is one.

    The webhook's word is never taken for it. Anything that can reach the endpoint can claim a
    card was saved, and a workflow can fire on a form the client abandoned half way; Stripe is
    the only thing that knows, so Stripe is asked, and only its answer clears the request.
    """
    if customer_facts(client)['card']['status'] != CARD_ON_FILE:
        return False

    if client.card_requested_at is not None:
        client.card_requested_at = None
        client.save(update_fields=['card_requested_at'])
    return True


def _assert_chargeable(customer: str) -> None:
    """Refuse to raise an invoice for a customer Stripe still has no card to charge.

    `charge_automatically` collects against the customer's default payment method, falling back
    to the legacy `default_source`. With neither set, the invoice still finalises and the
    payment then fails at once - and that failure reads exactly like a decline, when the real
    answer is that no card was ever made the default. GHL attaches a card without making it the
    invoice default, so `_promote_attached_card` is given the chance to fix exactly that before
    this gives up: only when no attached card exists either is there truly nothing to charge.
    """
    _configure()
    record = stripe.Customer.retrieve(customer)
    invoice_settings = getattr(record, 'invoice_settings', None)
    default_method = getattr(invoice_settings, 'default_payment_method', None)
    if default_method or getattr(record, 'default_source', None):
        return
    if _promote_attached_card(customer) is not None:
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

    Each card is attempted during the run rather than an hour afterwards (`_collect_now`), so
    most bills are `paid` or `retrying` by the time this returns. A bill still `charging` is one
    Stripe did not answer for in the run, and the webhook is what settles it.
    """
    start, _ = month_window(month)
    period = f'{start.year:04d}-{start.month:02d}'

    result = {
        'period': period,
        # `charged` counts cards attempted. The card answers during the run now, so what it
        # answered is counted too: `paid` and `declined` are both subsets of `charged`, and a
        # charged bill in neither is one Stripe has not finished answering for.
        'charged': 0,
        'paid': 0,
        'declined': 0,
        'skipped': 0,
        'too_small': 0,
        'twilio_paused': 0,
        'failed': 0,
        'total_cents': 0,
    }
    bills = Bill.objects.filter(billing_period=period, status=UNCHARGED).select_related('client')

    for bill in bills:
        if bill.client.billing_mode != AUTO_CHARGE:
            result['skipped'] += 1
            continue
        if (
            not settings.TWILIO_BILLING_ENABLED
            and bill.lines.filter(line_type=TWILIO_LINE).exists()
        ):
            # Calculated before Twilio was paused, so its total still includes minutes
            # GoHighLevel bills the client for as well. Charging it would take that money twice.
            # Left `uncharged` with the reason on it: the total cannot simply be reduced here,
            # because then the invoice and the saved bill would disagree about what was owed.
            reason = 'Bill includes Twilio minutes, which are no longer billed here.'
            logger.warning(
                'billing: %s for %s carries a Twilio line while Twilio billing is paused; '
                'not charged',
                bill.client.location_id,
                period,
            )
            bill.failure_reason = reason
            bill.save(update_fields=['failure_reason'])
            result['twilio_paused'] += 1
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
        if bill.status == PAID:
            result['paid'] += 1
        elif bill.status in (RETRYING, FAILED):
            result['declined'] += 1

    return result


def _charge_one(bill: Bill) -> None:
    """Put one bill on a Stripe invoice, finalise it, and take the card now.

    The invoice id is saved the moment it exists, before the lines are added, so a failure
    halfway through leaves a bill that can be traced to a real Stripe object rather than an
    orphan nobody can find.

    Finalising is what hands collection to Stripe; `_collect_now` is what stops the client's
    money waiting on Stripe's queue for it. See that function for why both happen.
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

    # Finalising a `charge_automatically` invoice is what puts it in Stripe's hands to collect,
    # and `auto_advance` is what hands the retry schedule over with it.
    stripe.Invoice.finalize_invoice(
        invoice.id, auto_advance=True, idempotency_key=f'{key}-finalize'
    )

    # `charging` before the card is attempted rather than after, so that a crash or a timeout
    # during the attempt below leaves the bill in the state the webhook can still answer for,
    # and never in `uncharged` with a finalised invoice already out at Stripe.
    bill.status = CHARGING
    # The clock a bill nothing ever answers for is measured against.
    bill.charged_at = timezone.now()
    bill.failure_reason = ''
    bill.save(update_fields=['status', 'charged_at', 'failure_reason'])
    logger.info(
        'billing: charging %s for %s on %s',
        bill.client.location_id,
        bill.billing_period,
        invoice.id,
    )

    _collect_now(bill, invoice.id, key)


def _collect_now(bill: Bill, invoice_id: str, key: str) -> None:
    """Take the card for a finalised invoice now instead of when Stripe's queue reaches it.

    Finalising a `charge_automatically` invoice does not charge it there and then: Stripe
    schedules the first attempt for about an hour later (seen live on 6 Nov 2026, 55 minutes),
    which left every bill reading `charging` for that hour with nothing to tell a stuck bill
    from one simply waiting. Paying here is the same attempt Stripe would have made by itself -
    off-session, against the same default card, on the same invoice - only now, so the month's
    outcome is on the bills by the time the command finishes.

    `auto_advance` is deliberately left on. This brings one attempt forward; it does not take
    the schedule back off Stripe, so an attempt that cannot be made here still gets made there.
    """
    try:
        invoice = stripe.Invoice.pay(invoice_id, idempotency_key=f'{key}-pay')
    except stripe.CardError as exc:
        # The card was reached and refused. That is an outcome, not a failure of the run: the
        # bill goes onto the retry schedule now rather than when `invoice.payment_failed`
        # arrives, and the webhook that follows finds it already there (`apply_event`).
        if _still_charging(bill):
            record_failure(bill, _stripe_reason(exc))
        return
    except stripe.StripeError:
        # Stripe was not reached, or would not answer. Whether the card was charged is not known
        # here, so nothing is written over `charging`: the webhook and Stripe's own schedule
        # settle this bill, exactly as they did before the card was taken immediately.
        logger.warning(
            'billing: %s for %s was finalised but could not be paid immediately; left to Stripe',
            bill.client.location_id,
            bill.billing_period,
            exc_info=True,
        )
        return

    if invoice.status == 'paid':
        if _still_charging(bill):
            _mark_paid(bill, paid_at=timezone.now())
        return

    # Stripe took the request without an error and the invoice is still not paid, which is how a
    # payment the client has to act on themselves comes back. Recorded the same way the retry
    # sweep records it (`_retry_one`), so both read alike on the bill.
    if _still_charging(bill):
        record_failure(bill, NOT_COLLECTED_REASON)


def _still_charging(bill: Bill) -> bool:
    """True if this bill is still waiting on the attempt just made, re-read from the database.

    Stripe can deliver `invoice.paid` before `Invoice.pay` has returned, so by now the webhook
    may already have settled this bill with Stripe's own timestamp and reason. An outcome that
    came from Stripe is never overwritten with one inferred here.
    """
    bill.refresh_from_db()
    if bill.status == CHARGING:
        return True
    logger.info(
        'billing: %s for %s was already %s by the time the charge returned',
        bill.client.location_id,
        bill.billing_period,
        bill.status,
    )
    return False


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
        # Stripe's own timestamp, not ours: a webhook can be retried hours after the payment.
        _mark_paid(bill, paid_at=datetime.fromtimestamp(event['created'], tz=UTC))
        return True

    if bill.status == PAID:
        # Webhooks are not ordered, and a failed attempt Stripe later collected can still be
        # redelivered afterwards. Money already taken is never un-taken on an older event.
        logger.info(
            'billing: %s arrived for already-paid bill %s, ignored', kind, bill.billing_period
        )
        return False

    if bill.status in (RETRYING, FAILED):
        # Already on the retry schedule, or already used up. The schedule decides what happens
        # next; this only records why this attempt was declined.
        bill.failure_reason = _failure_reason(event)
        bill.save(update_fields=['failure_reason'])
        return True

    record_failure(bill, _failure_reason(event))
    return True


def _mark_paid(bill: Bill, paid_at: datetime) -> None:
    """A bill is paid: clear every failure and retry field, then say so in GoHighLevel."""
    bill.status = PAID
    bill.paid_at = paid_at
    # Stripe's own retries can collect a bill that already failed once, so a paid bill must not
    # keep the reason an earlier attempt failed, or a retry date that no longer applies.
    bill.failure_reason = ''
    bill.next_retry_at = None
    bill.save(update_fields=['status', 'paid_at', 'failure_reason', 'next_retry_at'])
    sync_voice_routing(bill)


def record_failure(bill: Bill, reason: str, now: datetime | None = None) -> None:
    """Record a declined charge and decide what happens next: another retry, or failed.

    Called by the webhook for a bill's first decline and by the retry sweep for each retry that is
    declined, so the schedule is written in one place. Once `BILLING_MAX_RETRIES` retries have
    been made, the bill is `failed` for a person to act on, and the client's GoHighLevel status
    is set to `failed`.
    """
    now = now or timezone.now()
    bill.failure_reason = reason
    if bill.retry_count >= settings.BILLING_MAX_RETRIES:
        bill.status = FAILED
        bill.next_retry_at = None
        logger.warning(
            'billing: retries used up for %s, bill %s (%s)',
            bill.client.location_id,
            bill.billing_period,
            reason,
        )
    else:
        bill.status = RETRYING
        bill.next_retry_at = now + _retry_interval()
        logger.warning(
            'billing: charge declined for %s, bill %s (%s); retry %s of %s at %s',
            bill.client.location_id,
            bill.billing_period,
            reason,
            bill.retry_count + 1,
            settings.BILLING_MAX_RETRIES,
            bill.next_retry_at.isoformat(),
        )
    bill.save(update_fields=['status', 'failure_reason', 'next_retry_at'])
    if bill.status == FAILED:
        sync_voice_routing(bill)


def _retry_interval() -> timedelta:
    return timedelta(hours=settings.BILLING_RETRY_INTERVAL_HOURS)


def sync_voice_routing(bill: Bill) -> None:
    """Pause or resume the client's voice calls in GoHighLevel, from the bill's own state.

    Paused once a bill has used up its retries, resumed once it is paid - unless another bill
    for the same client is still failed, because paying one month must not switch the agent back
    on while an earlier month is unpaid. Any other status leaves GoHighLevel alone, so a bill
    still being retried pauses nobody.

    **What pauses a client is now the agent id itself** (`pause_voice`): the custom value their
    call-routing workflow reads to know which Retell agent answers is emptied, so there is no
    agent for a call to reach whatever the workflow does. The status this used to write was
    indirect - it needed a correctly-built workflow at the other end to turn the word into "do
    not route this call", and one client whose workflow was built before that convention kept
    taking calls unpaid.

    **The contact field is still written when one is configured**
    (`GHL_RETELL_STATUS_CUSTOM_FIELD_ID`, `OFF` or `ON` - see `GHL_STATUS_FAILED`), because it is
    the only thing a GoHighLevel workflow can *trigger* on, and a client in arrears is still
    meant to hear about it. Blank means no dunning field has been created yet and the write is
    skipped in silence - the pause does not depend on it.

    Every write here is best effort. The bill's outcome is already saved, and GoHighLevel being
    down must not undo it or stop the webhook, the sweep or the retry button.
    """
    if bill.status == FAILED:
        paused, status_word = True, GHL_STATUS_FAILED
    elif bill.status == PAID:
        if bill.client.bills.exclude(pk=bill.pk).filter(status=FAILED).exists():
            return
        paused, status_word = False, GHL_STATUS_ACTIVE
    else:
        return

    if paused:
        pause_voice(bill.client)
    else:
        resume_voice(bill.client)

    _set_dunning_status(bill.client, status_word)


def pause_voice(client: ClientMapping) -> None:
    """Empty the client's Retell agent custom value, so no call can reach a paid agent.

    The live value is captured into `retell_routing_agent_id` before it is cleared: clearing
    destroys it, and what was actually routing their calls is a better answer than what we
    believe should be. An empty live value leaves whatever was already captured alone.

    `voice_paused_at` is stamped only once GoHighLevel has taken the write, so a bill that reads
    `failed` beside a null `voice_paused_at` is a client who was *not* paused - which is the
    thing to alert on, and the thing a log line alone would hide.
    """
    try:
        custom_value = _agent_custom_value(client.location_id)
        live = str(custom_value.get('value') or '').strip()
        if live and live != client.retell_routing_agent_id:
            client.retell_routing_agent_id = live
            client.save(update_fields=['retell_routing_agent_id'])

        GHLOAuthService().set_custom_value(
            client.location_id, custom_value['id'], '', name=custom_value.get('name') or ''
        )
    except GHLError as exc:
        logger.exception('billing: could not pause voice for %s', client.location_id)
        _record_voice_sync(client, paused_at=client.voice_paused_at, error=str(exc))
        return

    logger.info('billing: voice paused for %s', client.location_id)
    _record_voice_sync(client, paused_at=timezone.now(), error='')


def resume_voice(client: ClientMapping) -> None:
    """Put the client's Retell agent id back, so their calls route again.

    The id comes from what was captured when they were paused, falling back to their mapping.
    A client with neither is left alone and logged: writing a guessed agent id would route a
    client's calls to an agent nobody chose, which is worse than leaving the pause in place for
    a human to fix.
    """
    agent_id = client.retell_routing_agent_id or _mapped_agent_id(client)
    if not agent_id:
        logger.warning(
            'billing: no Retell agent id to restore for %s, so its voice stays paused',
            client.location_id,
        )
        _record_voice_sync(
            client,
            paused_at=client.voice_paused_at,
            error='No Retell agent id to restore. Map an agent to this client.',
        )
        return

    try:
        custom_value = _agent_custom_value(client.location_id)
        GHLOAuthService().set_custom_value(
            client.location_id,
            custom_value['id'],
            agent_id,
            name=custom_value.get('name') or '',
        )
    except GHLError as exc:
        logger.exception('billing: could not resume voice for %s', client.location_id)
        _record_voice_sync(client, paused_at=client.voice_paused_at, error=str(exc))
        return

    logger.info('billing: voice resumed for %s on agent %s', client.location_id, agent_id)
    _record_voice_sync(client, paused_at=None, error='')


def _agent_custom_value(location_id: str) -> dict:
    """The sub-account's own Retell agent custom value, looked up by its configured name.

    Resolved on every write rather than cached on the client: pausing and resuming happen at
    most twice a client-month, so the extra read is free, and a value deleted and recreated by a
    snapshot refresh gets a new id that a cached one would go on writing past.
    """
    name = settings.GHL_RETELL_AGENT_CUSTOM_VALUE_NAME
    custom_value = GHLOAuthService().find_custom_value(location_id, name)
    if custom_value is None or not custom_value.get('id'):
        raise GHLError(f'No custom value named {name!r} in sub-account {location_id}.')
    return custom_value


def _mapped_agent_id(client: ClientMapping) -> str:
    """The client's mapped Retell agent. One agent per client, so the first is the only one."""
    agents = client.retell_agent_ids or []
    return str(agents[0]) if agents else ''


def _record_voice_sync(client: ClientMapping, paused_at: datetime | None, error: str) -> None:
    """Save what the last pause or resume left behind, for the client's page to read."""
    client.voice_paused_at = paused_at
    client.voice_sync_error = error[:255]
    client.save(update_fields=['voice_paused_at', 'voice_sync_error'])


def _set_dunning_status(client: ClientMapping, value: str) -> None:
    """Tell GoHighLevel's dunning workflow where this client stands, if one is configured.

    A contact field is the only thing a GoHighLevel workflow can trigger on, so this is what
    sends the client an email about a failed payment. Blank `GHL_RETELL_STATUS_CUSTOM_FIELD_ID`
    means no such field exists yet: skipped in silence, the way a blank card status field is.

    `value` has to be one of the field's own picklist options - `GHL_STATUS_FAILED` or
    `GHL_STATUS_ACTIVE`, nothing else.
    """
    if not settings.GHL_RETELL_STATUS_CUSTOM_FIELD_ID:
        return
    if not client.billing_email:
        # Nothing to find a contact by. Logged because the bill's outcome stands either way, and
        # this is the only trace that the workflow was never told.
        logger.warning(
            'billing: %s has no billing email, so its status could not be set to %s',
            client.location_id,
            value,
        )
        return

    try:
        GHLOAuthService().set_contact_field(
            client.billing_email, settings.GHL_RETELL_STATUS_CUSTOM_FIELD_ID, value
        )
    except GHLError:
        logger.exception(
            'billing: could not set the rebilling status to %s for %s', value, client.location_id
        )


def retry_due_bills(now: datetime | None = None) -> dict:
    """Make every automatic retry that is due, and report what came of each.

    A bill is claimed under a row lock before Stripe is called: its retry count goes up and its
    next retry moves on in the same transaction, so a second run at the same moment finds nothing
    due for that bill. The Stripe call also carries an idempotency key built from the retry
    number, so a retry repeated after a crash cannot charge the card twice.
    """
    now = now or timezone.now()
    result = {'retried': 0, 'paid': 0, 'retrying': 0, 'failed': 0}
    due = Bill.objects.filter(status=RETRYING, next_retry_at__lte=now).values_list('pk', flat=True)

    for pk in list(due):
        claimed = _claim_retry(pk, now)
        if claimed is None:
            continue
        result['retried'] += 1
        result[_retry_one(claimed, now)] += 1
    return result


def _claim_retry(pk: int, now: datetime) -> Bill | None:
    """Move a due bill onto its next retry, under a lock. None if it is no longer due."""
    with transaction.atomic():
        bill = Bill.objects.select_for_update().select_related('client').get(pk=pk)
        if bill.status != RETRYING or bill.next_retry_at is None or bill.next_retry_at > now:
            return None
        bill.retry_count += 1
        bill.last_retry_at = now
        bill.next_retry_at = now + _retry_interval()
        bill.save(update_fields=['retry_count', 'last_retry_at', 'next_retry_at'])
        return bill


def _retry_one(bill: Bill, now: datetime) -> str:
    """Try the open Stripe invoice again. Returns the bill's resulting status for the sweep's count.

    An invoice Stripe already shows as paid is taken as paid without asking for the money again.
    Otherwise the open invoice is paid, which makes Stripe attempt the card once more.
    """
    try:
        invoice = _pay_open_invoice(bill)
    except stripe.StripeError as exc:
        logger.warning(
            'billing: retry %s for %s, bill %s, could not be made',
            bill.retry_count,
            bill.client.location_id,
            bill.billing_period,
            exc_info=True,
        )
        record_failure(bill, _stripe_reason(exc), now)
        return bill.status

    if invoice.status == 'paid':
        _mark_paid(bill, paid_at=now)
        return PAID

    record_failure(bill, NOT_COLLECTED_REASON, now)
    return bill.status


def _pay_open_invoice(bill: Bill):
    """Ask Stripe for this bill's invoice and pay it unless Stripe already shows it paid.

    The read first is not caution for its own sake: an invoice Stripe collected without us
    hearing about it - a webhook that never arrived - is taken as paid rather than charged
    again. The idempotency key carries the retry number, so each attempt is its own request and
    none of them can replay the cached answer of an earlier one (Stripe keeps those 24 hours,
    declines included).

    Raises whatever Stripe raised; the caller decides what a decline means.
    """
    _configure()
    invoice = stripe.Invoice.retrieve(bill.stripe_invoice_id)
    if invoice.status == 'paid':
        return invoice
    return stripe.Invoice.pay(
        bill.stripe_invoice_id, idempotency_key=f'bill-{bill.pk}-retry-{bill.retry_count}'
    )


def retry_now(bill_pk: int, now: datetime | None = None) -> Bill:
    """Make one retry this moment, because an admin asked for it. Returns the settled bill.

    The whole attempt is made under the bill's own row lock, so a second click - or the hourly
    sweep landing mid-attempt - waits rather than charging the same card twice, and the first
    thing it finds on the other side is the outcome of the attempt it waited for. A bill another
    attempt has already paid is returned as paid without asking Stripe for the money again.

    **A decline is not an error here.** The card answering "no" is the outcome, recorded on the
    bill exactly as the sweep records it, and handed back for the page to show. Only an attempt
    that could not be *made* - Stripe unreachable, or answering about something other than this
    card - raises, and raising rolls the whole transaction back, so a bill nobody could charge
    keeps the retry count and schedule it had before the button was pressed.

    Which retry this is counts: a `retrying` bill spends one of its automatic retries and has its
    next one pushed out, so "retry 2 of 3" on the page stays true and three clicks cannot become
    an unbounded series of attempts against a card that keeps declining. A `failed` bill has no
    schedule left to spend, so staff may keep trying it - each attempt still takes the next
    number, which is what keeps the idempotency keys apart.
    """
    now = now or timezone.now()
    with transaction.atomic():
        bill = Bill.objects.select_for_update().select_related('client').get(pk=bill_pk)
        if bill.status == PAID:
            return bill
        if bill.status not in (RETRYING, FAILED):
            raise ManualRetryUnavailable(
                'Only a failed or retrying bill can be retried. '
                f'This one is {bill.get_status_display().lower()}.'
            )
        if not bill.stripe_invoice_id:
            raise ManualRetryUnavailable(
                'This bill has no Stripe invoice to pay. It was never sent to Stripe.'
            )
        if bill.client.billing_mode != AUTO_CHARGE:
            raise ManualRetryUnavailable(
                'This client is not on automatic charging, so their card is not taken here.'
            )

        bill.retry_count += 1
        bill.last_retry_at = now
        if bill.status == RETRYING:
            bill.next_retry_at = now + _retry_interval()
        bill.save(update_fields=['retry_count', 'last_retry_at', 'next_retry_at'])

        try:
            invoice = _pay_open_invoice(bill)
        except stripe.CardError as exc:
            # Reached the card and was refused: an outcome, recorded and handed back.
            record_failure(bill, _stripe_reason(exc), now)
            return bill
        except stripe.StripeError as exc:
            # Never reached the card, or Stripe would not say. Raising undoes the retry this
            # transaction claimed, so nothing was spent on an attempt that never happened.
            logger.warning(
                'billing: manual retry for %s, bill %s, could not be made',
                bill.client.location_id,
                bill.billing_period,
                exc_info=True,
            )
            raise StripeBillingError(_stripe_reason(exc)) from exc

        if invoice.status == 'paid':
            _mark_paid(bill, paid_at=now)
        else:
            record_failure(bill, NOT_COLLECTED_REASON, now)
        return bill


def _stripe_reason(exc: stripe.StripeError) -> str:
    """Stripe's code for a failed attempt, or the error's name when Stripe gave no code."""
    return (getattr(exc, 'code', None) or exc.__class__.__name__)[:255]


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
