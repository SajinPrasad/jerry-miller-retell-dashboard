"""Phase 5: charging a calculated bill through a GoHighLevel invoice.

This is the primary rail. Jerry's clients already hold a card in his GHL sub-account against
their $397 plan, so an invoice raised there reaches a card that is already on file and lands the
money in his own processor within days. `stripe_billing` is the fallback for when it does not.

Three things about GHL shape everything here:

* **Invoices are sub-account scoped.** They are raised in `GHL_MAIN_LOCATION_ID`, the sub-account
  that holds the client contacts and their saved cards — never in `AGENCY_LOCATION_ID`, which
  holds no contacts at all and would silently find nobody to bill.
* **GHL publishes no charge-failed webhook.** A declined invoice and an invoice GHL accepted and
  quietly did nothing with are indistinguishable from here: both are an invoice that never
  becomes `paid`. Only age tells them apart, which is why the fallback is a timer and not a
  reaction.
* **GHL retries a decline twice more, 24 hours apart, and that is not configurable.** Nothing
  here may fall back before those retries are done. `dual_rail` owns that rule.

Docs: https://highlevel.stoplight.io/docs/integrations/ (Invoices v3)
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from django.conf import settings

from apps.ghl.exceptions import GHLError
from apps.ghl.models import GHLOAuthToken
from apps.ghl.services import GHLOAuthService

from .exceptions import GHLInvoiceError
from .models import PAID, RETELL_LINE, TWILIO_LINE, Bill

logger = logging.getLogger(__name__)

# The currency Jerry bills in. Not a setting: it does not vary by environment, it varies by
# business, and nothing about this code would work for a second one anyway.
CURRENCY = 'USD'

# What each line is called on the client's invoice. The client reads these, so they say what was
# bought rather than which vendor supplied it.
LINE_NAMES = {
    RETELL_LINE: 'Voice AI minutes',
    TWILIO_LINE: 'Call minutes',
}

# The only GHL invoice event that decides a bill's fate. GHL sends six others; none of them
# means the charge failed, because no such event exists.
INVOICE_PAID = 'InvoicePaid'

# Shown on the invoice when GHL holds no phone number for the contact. GHL requires the field;
# it does not require it to be reachable.
NO_PHONE = 'N/A'


class GHLInvoiceService:
    """Raise, send and void the invoices that carry a month's voice usage."""

    def __init__(self, client: httpx.Client | None = None, oauth: GHLOAuthService | None = None):
        self.client = client or httpx.Client(
            base_url=settings.GHL_API_BASE_URL, timeout=settings.GHL_REQUEST_TIMEOUT
        )
        self.oauth = oauth or GHLOAuthService()

    # --- the three calls a charge is made of ---------------------------------------------

    def create_invoice(self, bill: Bill) -> str:
        """Raise a draft invoice for one bill and return its GHL id.

        The contact is read from GHL rather than from our own row, so the name, email and phone
        on the invoice are whatever GHL holds today. A contact we cannot read stops the invoice:
        billing the wrong person is worse than not billing today.
        """
        client = bill.client
        if not client.billing_contact_id:
            raise GHLInvoiceError(f'{client.location_id} has no billing contact to invoice.')

        contact = self._contact(client.billing_contact_id)
        self._remember_email(bill, contact)

        today = datetime.now(tz=UTC).date()
        body = self._request(
            'POST',
            '/invoices/',
            json={
                'altId': self._location,
                'altType': 'location',
                'name': f'Voice usage {bill.billing_period}',
                'currency': CURRENCY,
                'liveMode': settings.GHL_INVOICE_LIVE_MODE,
                'businessDetails': {'name': settings.GHL_BUSINESS_NAME},
                'contactDetails': {
                    'id': client.billing_contact_id,
                    'name': contact.get('contactName') or client.client_name,
                    'email': contact.get('email') or '',
                    'phoneNo': contact.get('phone') or NO_PHONE,
                },
                'issueDate': today.isoformat(),
                'dueDate': today.isoformat(),
                'items': [_item(line) for line in bill.lines.all()],
            },
        )

        invoice_id = body.get('_id') or body.get('id')
        if not invoice_id:
            raise GHLInvoiceError(f'GHL created an invoice for {client.location_id} with no id.')
        return invoice_id

    def send_invoice(self, invoice_id: str) -> dict:
        """Send the invoice and ask GHL to take the contact's card.

        `autoPayment.enable` on its own, with no `paymentMethodId`, is what charges the card
        already on file. The published schema requires nothing else, and no public endpoint
        reads a contact's saved cards, so there is no id we could send even if we wanted to.
        Whether GHL acts on it is the one thing the docs cannot answer and the live test must.
        """
        return self._request(
            'POST',
            f'/invoices/{invoice_id}/send',
            json={
                'altId': self._location,
                'altType': 'location',
                'action': 'send_manually',
                'liveMode': settings.GHL_INVOICE_LIVE_MODE,
                'autoPayment': {'enable': True},
            },
        )

    def void_invoice(self, invoice_id: str) -> dict:
        """Void an invoice so GHL stops trying to collect on it.

        This is what makes a Stripe fallback safe. Until it succeeds, GHL may still charge the
        card on one of its own retries, and a second charge on Stripe would take the money twice.
        """
        return self._request(
            'POST',
            f'/invoices/{invoice_id}/void',
            json={'altId': self._location, 'altType': 'location'},
        )

    # --- helpers -------------------------------------------------------------------------

    @property
    def _location(self) -> str:
        """The sub-account the invoices live in.

        Usually the real Main Sub-Account, distinct from `AGENCY_LOCATION_ID` - see
        `local_folder/plan.md`, "Live test-phase topology", for why the two are currently the
        same id in Jerry's account, and `GHLOAuthService._agency_location_token()` for how a
        valid Location-class token still gets minted for it either way.
        """
        if not settings.GHL_MAIN_LOCATION_ID:
            raise GHLInvoiceError('GHL_MAIN_LOCATION_ID is not set; no sub-account to invoice in.')
        return settings.GHL_MAIN_LOCATION_ID

    def _contact(self, contact_id: str) -> dict:
        body = self._request('GET', f'/contacts/{contact_id}')
        contact = body.get('contact')
        if not contact:
            raise GHLInvoiceError(f'GHL returned no contact for {contact_id}.')
        return contact

    @staticmethod
    def _remember_email(bill: Bill, contact: dict) -> None:
        """Keep the client's billing email in step with the GHL contact.

        The Stripe fallback finds its customer by this email, and it is the only thing that
        carries from one rail to the other, so it is taken from the same contact the invoice is
        addressed to rather than typed in somewhere else and left to drift.
        """
        email = contact.get('email') or ''
        if email and email != bill.client.billing_email:
            bill.client.billing_email = email
            bill.client.save(update_fields=['billing_email'])

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            token = self.oauth.get_location_token(self._location)
        except (GHLError, GHLOAuthToken.DoesNotExist) as exc:
            # No usable token for the billing sub-account. Raised as our own error so one
            # client's bad token is skipped like any other failure rather than ending the run.
            raise GHLInvoiceError(
                f'No GHL token for billing sub-account {self._location}: {exc}'
            ) from exc
        headers = {
            'Accept': 'application/json',
            'Version': settings.GHL_API_VERSION,
            'Authorization': f'Bearer {token}',
        }
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            # GHL puts the real reason in the body; the status line alone isn't diagnosable.
            raise GHLInvoiceError(
                f'GHL {method} {path} failed: {exc.response.status_code} {exc.response.text[:500]}'
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise GHLInvoiceError(f'GHL {method} {path} failed: {exc}') from exc


def _item(line) -> dict:
    """One bill line as a GHL invoice item.

    GHL types `amount` as a number rather than an integer, so it is a price in dollars where
    Stripe's is cents. The division happens here, at the edge, and nowhere else: `bill_cents`
    stays the integer Phase 3 calculated it as, and no money is ever held as a float.
    """
    return {
        'name': LINE_NAMES[line.line_type],
        'currency': CURRENCY,
        'amount': float(Decimal(line.bill_cents) / 100),
        'qty': 1,
        'description': f'{line.quantity_minutes} min',
    }


def apply_event(event: dict) -> bool:
    """Record what GHL says happened to a bill. True if a bill was updated.

    Only `InvoicePaid` is acted on. The other six invoice events either describe something we
    did ourselves or say nothing about whether the money moved, and GHL publishes no event at
    all for a failed charge — the fallback window in `dual_rail` is what covers that.

    An event about an invoice we did not raise is not an error: this sub-account invoices
    clients for their plans too, and those are none of our business.
    """
    if event.get('type') != INVOICE_PAID:
        return False

    invoice_id = event.get('_id') or event.get('id') or ''
    bill = Bill.objects.filter(ghl_invoice_id=invoice_id).first() if invoice_id else None
    if bill is None:
        logger.info('billing: InvoicePaid for unknown GHL invoice %s, ignored', invoice_id)
        return False

    bill.status = PAID
    bill.paid_at = _paid_at(event)
    bill.save(update_fields=['status', 'paid_at'])
    logger.info(
        'billing: GHL paid %s for %s on %s',
        bill.client.location_id,
        bill.billing_period,
        invoice_id,
    )
    return True


def _paid_at(event: dict) -> datetime:
    """GHL's own timestamp for the payment, falling back to now if it sent none.

    A webhook can be retried hours after the payment, so `now()` would date the money to
    whenever we happened to hear about it.
    """
    stamp = event.get('paidAt') or event.get('dateAdded') or ''
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return datetime.now(tz=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
