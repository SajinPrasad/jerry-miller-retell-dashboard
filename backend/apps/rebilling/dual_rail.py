"""Phase 5: deciding which rail a bill is charged on, and when.

`ghl_billing` and `stripe_billing` each know how to charge a bill. This knows which one to ask
and how long to wait, and it is the only place that may start a charge.

The order is GHL first, Stripe second, and the gap between them is the whole point:

* **GHL retries a decline twice more, 24 hours apart, and that schedule is not configurable.**
  Falling back at hour one would race GHL's hour-24 retry and take the money twice for the same
  month. The window therefore has a floor of 72 hours, and is a setting rather than a constant
  because the floor is GHL's behaviour, not ours.
* **GHL publishes no charge-failed webhook.** A declined invoice, and an invoice GHL accepted
  and quietly did nothing with, both reach us as an invoice that never becomes `paid`. The
  second is the dangerous one — it reads as success in every log we keep — and it is the reason
  the fallback is a timer over `charging` bills rather than a reaction to a failure event.
* **The GHL invoice is voided before Stripe is touched, always.** If the void fails, the
  fallback does not happen. A bill nobody charged can be charged tomorrow; a bill charged on
  both rails has taken money twice from a client who owed it once.
"""

import logging
from datetime import timedelta

import stripe
from django.conf import settings
from django.utils import timezone

from .exceptions import GHLInvoiceError, StripeBillingError
from .ghl_billing import GHLInvoiceService
from .models import AUTO_CHARGE, CHARGING, FAILED, UNCHARGED, Bill
from .services import month_window
from .stripe_billing import charge_on_stripe

logger = logging.getLogger(__name__)


def charge_bills(month: str | None = None) -> dict:
    """Charge one month's bills: GHL first, then Stripe for anything GHL did not collect.

    Absent `month` means this month. Safe to run repeatedly — it is how the fallback happens at
    all, since a bill only becomes eligible for Stripe once enough time has passed. One bill's
    failure is logged and counted, never allowed to end the run for the others.
    """
    start, _ = month_window(month)
    period = f'{start.year:04d}-{start.month:02d}'

    result = {
        'period': period,
        'ghl_sent': 0,
        'skipped': 0,
        'ghl_failed': 0,
        'fell_back': 0,
        'fallback_blocked': 0,
        'fallback_failed': 0,
        'total_cents': 0,
    }

    service = GHLInvoiceService()
    _charge_on_ghl(period, service, result)
    _fall_back_to_stripe(service, result)
    return result


def _charge_on_ghl(period: str, service: GHLInvoiceService, result: dict) -> None:
    """Raise and send a GHL invoice for every bill of this period nobody has charged yet."""
    bills = Bill.objects.filter(billing_period=period, status=UNCHARGED).select_related('client')

    for bill in bills:
        if bill.client.billing_mode != AUTO_CHARGE:
            # `billing_mode` is how someone says "do not take this one's card", and it has to
            # mean it on both rails.
            result['skipped'] += 1
            continue
        try:
            # An id already here means a previous run raised the invoice and then failed to send
            # it. Sending that one again is the only safe move: creating a second would leave the
            # first as an orphan draft in Jerry's account, and GHL offers no idempotency key to
            # collapse the two.
            invoice_id = bill.ghl_invoice_id
            if not invoice_id:
                invoice_id = service.create_invoice(bill)
                # Saved before the invoice is sent, so a failure in between still leaves a bill
                # that can be traced to a real GHL invoice rather than an orphan nobody can find.
                bill.ghl_invoice_id = invoice_id
                bill.save(update_fields=['ghl_invoice_id'])

            service.send_invoice(invoice_id)
        except GHLInvoiceError:
            logger.warning(
                'billing: GHL could not charge %s for %s',
                bill.client.location_id,
                period,
                exc_info=True,
            )
            result['ghl_failed'] += 1
            continue

        bill.status = CHARGING
        # The clock the fallback window is measured from.
        bill.charged_at = timezone.now()
        bill.save(update_fields=['status', 'charged_at'])
        result['ghl_sent'] += 1
        result['total_cents'] += bill.total_cents
        logger.info(
            'billing: GHL charging %s for %s on %s',
            bill.client.location_id,
            period,
            invoice_id,
        )


def _fall_back_to_stripe(service: GHLInvoiceService, result: dict) -> None:
    """Move bills GHL has had long enough onto Stripe, voiding the GHL invoice first.

    This deliberately looks at every month, not just the one being charged: a bill sent to GHL
    at the end of last month comes due for its fallback in the middle of this one, and nothing
    else would ever come back for it.
    """
    cutoff = timezone.now() - timedelta(hours=settings.BILLING_FALLBACK_WINDOW_HOURS)
    stranded = (
        Bill.objects.filter(
            status=CHARGING,
            charged_at__lte=cutoff,
            # On the GHL rail and not yet on the Stripe one.
            stripe_invoice_id='',
        )
        .exclude(ghl_invoice_id='')
        .select_related('client')
    )

    for bill in stranded:
        try:
            service.void_invoice(bill.ghl_invoice_id)
        except GHLInvoiceError:
            # The first rail is still live. Charging Stripe now risks taking the money twice,
            # so this bill waits for a human instead.
            logger.error(
                'billing: could not void GHL invoice %s for %s; Stripe fallback withheld',
                bill.ghl_invoice_id,
                bill.client.location_id,
                exc_info=True,
            )
            result['fallback_blocked'] += 1
            continue

        bill.ghl_invoice_voided_at = timezone.now()
        bill.save(update_fields=['ghl_invoice_voided_at'])
        logger.info(
            'billing: voided GHL invoice %s for %s after %sh with no payment',
            bill.ghl_invoice_id,
            bill.client.location_id,
            settings.BILLING_FALLBACK_WINDOW_HOURS,
        )

        try:
            charge_on_stripe(bill)
        except (StripeBillingError, stripe.StripeError):
            logger.warning(
                'billing: Stripe fallback failed for %s on %s',
                bill.client.location_id,
                bill.billing_period,
                exc_info=True,
            )
            # Both rails are done with this bill. The GHL invoice is voided, so nothing is
            # going to collect it without someone stepping in.
            bill.status = FAILED
            bill.save(update_fields=['status'])
            result['fallback_failed'] += 1
            continue

        result['fell_back'] += 1
        logger.info(
            'billing: Stripe fallback charging %s for %s on %s',
            bill.client.location_id,
            bill.billing_period,
            bill.stripe_invoice_id,
        )
