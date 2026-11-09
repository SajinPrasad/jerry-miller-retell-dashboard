"""Shared ground for the two live-verification commands: what they touch, and how to undo it.

`verify_live_billing` drives the real production billing path against live GoHighLevel and Stripe
in **test mode** and stops with one client paid and one client failed and paused, so both can be
inspected by hand. `verify_live_billing_cleanup` puts everything back.

Everything reversible lives here, in one place, for one reason: the run command's job is to make
changes and the cleanup command's job is to undo exactly those changes, and the two only agree
about what "exactly those" means if they read the same constants and the same state file.

**Nothing here invents billing behaviour.** The commands call `calculate_bills`, `charge_bills`,
`retry_due_bills` and `retry_now` - the same functions the monthly run and the admin page call.
This module only sets the stage: synthetic usage in a dead period, a card that declines on
purpose, and a record of what everything looked like first.
"""

import json
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path

import stripe
from django.conf import settings

from apps.ghl.services import GHLOAuthService

from .models import (
    BILLABLE,
    DEFAULT_CALL_RATE_CENTS,
    DEFAULT_VOICE_RATE_CENTS,
    ENDED,
    Bill,
    ClientMapping,
    RateHistory,
    RetellCall,
)

# The period every synthetic row is written into. Chosen because it is dead: it is far enough
# back that no real usage is still arriving for it, and close enough that it stays inside the
# admin page's twelve-month picker, so the failed bill can be found and its Retry payment button
# pressed by hand. Nothing outside this period is read, written or deleted by either command.
TEST_PERIOD = '2025-12'
TEST_PERIOD_START = date_type(2025, 12, 1)
# Mid-period, so the call sits well inside the window whatever the server's timezone does.
TEST_CALL_AT = datetime(2025, 12, 15, 12, 0, tzinfo=UTC)

# The two sub-accounts the simulation runs against, by GoHighLevel location id. Hard-coded
# rather than taken as arguments: these are the two accounts this exercise was agreed against,
# and a mistyped id here would mean charging a real client and emptying their live agent id.
PAID_PATH_LOCATION_ID = 'c1m5g81A2wXZbPLbbHsr'
PAID_PATH_NAME = 'Branwynn Destinations LLC'
FAILED_PATH_LOCATION_ID = 'eOcXvOOF2Rg56lR47HUl'
FAILED_PATH_NAME = 'FrostLine Climate Solutions'

# A Stripe test payment method that attaches cleanly to a customer and then declines every
# invoice charge made against it. **Not** `pm_card_chargeDeclined`: that one declines on the
# `PaymentMethod.attach` call itself, in test mode, which would raise a `CardError` before this
# module ever got as far as finalising an invoice - the decline has to happen at charge time, not
# at attach time, or there is no invoice for the retry sweep to work on. Attaching it produces a
# *new* payment method on the customer, whose id is what gets detached again, so the shared token
# itself is never what we clean up.
DECLINING_TEST_PM = 'pm_card_chargeCustomerFail'

# How long the synthetic call is. At the 25c default voice rate this bills $2.50 - comfortably
# over Stripe's 50c minimum, which a bill under would be skipped by `charge_bills` entirely and
# never reach a card at all.
TEST_CALL_MINUTES = 10

# Every row either command creates or deletes carries this in its id, so cleanup can delete by
# prefix and can never reach a real Retell call.
TEST_CALL_PREFIX = 'live-verify-'

# Where the run command leaves what it found, for the cleanup command to put back.
STATE_FILENAME = 'live_verification_state.json'


class VerificationError(Exception):
    """A safety check said no, or the live systems answered something the run cannot continue on.

    The commands turn this into a `CommandError`, so the message reaches the operator as the
    reason the run stopped rather than as a traceback.
    """


# --- safety -----------------------------------------------------------------------------------


def assert_test_mode() -> None:
    """Refuse to run against anything but a Stripe test key.

    This is the one check that cannot be skipped by an option, because it is the only thing
    standing between this exercise and real money leaving a real client's real card. A live key
    starts `sk_live_`; a restricted key starts `rk_`, which is also refused - a restricted key
    can be scoped to the live account just as easily as to the test one.
    """
    key = settings.STRIPE_SECRET_KEY or ''
    if not key.startswith('sk_test_'):
        shown = f'{key[:8]}...' if key else '(empty)'
        raise VerificationError(
            'Refusing to run: STRIPE_SECRET_KEY must be a Stripe test key starting with '
            f'"sk_test_", but it starts with {shown}. This command charges cards and empties '
            'live GoHighLevel values; it must never run against the live Stripe account.'
        )


def target_clients() -> tuple[ClientMapping, ClientMapping]:
    """The two mapped clients the simulation runs against, in (paid path, failed path) order."""
    return _target(PAID_PATH_LOCATION_ID, PAID_PATH_NAME), _target(
        FAILED_PATH_LOCATION_ID, FAILED_PATH_NAME
    )


def _target(location_id: str, expected_name: str) -> ClientMapping:
    client = ClientMapping.objects.filter(location_id=location_id).first()
    if client is None:
        raise VerificationError(
            f'No ClientMapping for {expected_name} ({location_id}). Map it in the admin page '
            'before running this.'
        )
    return client


# --- state ------------------------------------------------------------------------------------


def state_path(given: str | None = None) -> Path:
    return Path(given) if given else Path(settings.BASE_DIR) / STATE_FILENAME


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, default=str), encoding='utf-8')


def load_state(path: Path) -> dict:
    """What the run command backed up. Cleanup cannot restore anything without it."""
    if not path.exists():
        raise VerificationError(
            f'No backup file at {path}. The cleanup command restores what the run command saved '
            'there, so without it there is nothing to restore from - and guessing the values '
            'would be worse than stopping. Pass --state-file if it was written elsewhere.'
        )
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise VerificationError(f'The backup file at {path} is not readable JSON: {exc}') from exc


# --- GoHighLevel --------------------------------------------------------------------------------


def read_agent_value(location_id: str) -> dict:
    """The sub-account's Retell agent custom value, read exactly as the pause path resolves it.

    By name through `find_custom_value`, because that is what `stripe_billing._agent_custom_value`
    does: reading it any other way would verify a different value than the one the pause writes.
    """
    name = settings.GHL_RETELL_AGENT_CUSTOM_VALUE_NAME
    value = GHLOAuthService().find_custom_value(location_id, name)
    if value is None or not value.get('id'):
        raise VerificationError(
            f'No custom value named {name!r} in sub-account {location_id}. The pause and resume '
            'path needs it, so the simulation cannot verify anything without it.'
        )
    return {
        'id': value['id'],
        'name': value.get('name') or name,
        'value': str(value.get('value') or ''),
    }


def write_agent_value(location_id: str, backup: dict, value: str) -> None:
    """Put a value back into the sub-account's Retell agent custom value.

    The id is re-resolved by name rather than taken from the backup: a snapshot refresh between
    the run and the cleanup deletes and recreates the value under a new id, and writing to the
    old one would succeed at nothing. The backed-up id is the fallback for when the name itself
    has since been changed.
    """
    try:
        current = read_agent_value(location_id)
        custom_value_id, name = current['id'], current['name']
    except VerificationError:
        custom_value_id, name = backup['id'], backup['name']

    GHLOAuthService().set_custom_value(location_id, custom_value_id, value, name=name)


# --- synthetic usage ----------------------------------------------------------------------------


def seed_usage(client: ClientMapping) -> dict:
    """Give one client a rate and a billable call in the test period. Idempotent.

    Returns what was created, so cleanup deletes the rate row only if this put it there - a
    client who already had a rate starting on or before the test period keeps it.
    """
    rate, rate_created = RateHistory.objects.get_or_create(
        client=client,
        start_date=TEST_PERIOD_START,
        defaults={
            'voice_rate_cents': DEFAULT_VOICE_RATE_CENTS,
            'call_rate_cents': DEFAULT_CALL_RATE_CENTS,
        },
    )

    agents = client.retell_agent_ids or []
    call, call_created = RetellCall.objects.get_or_create(
        call_id=test_call_id(client),
        defaults={
            'client': client,
            'agent_id': str(agents[0]) if agents else '',
            'direction': 'outbound',
            'started_at': TEST_CALL_AT,
            'duration_ms': TEST_CALL_MINUTES * 60_000,
            'retell_cost_cents': 0,
            'call_status': ENDED,
            'billing_status': BILLABLE,
        },
    )
    return {
        'rate_id': rate.pk,
        'rate_created': rate_created,
        'rate_cents': rate.voice_rate_cents,
        'call_id': call.call_id,
        'call_created': call_created,
    }


def test_call_id(client: ClientMapping) -> str:
    return f'{TEST_CALL_PREFIX}{TEST_PERIOD}-{client.location_id}'


def test_bill(client: ClientMapping) -> Bill | None:
    return Bill.objects.filter(client=client, billing_period=TEST_PERIOD).first()


def delete_synthetic(client: ClientMapping, seeded: dict) -> dict:
    """Remove this client's test period rows, and nothing else.

    The bill goes first: deleting it unlinks the call (`RetellCall.bill` is `SET_NULL`) and takes
    its lines with it, so the call can then be deleted on its own. Only the rate row this run
    created is removed, and only ever the one call id this module builds.
    """
    removed = {'bills': 0, 'calls': 0, 'rates': 0}

    bill = test_bill(client)
    if bill is not None:
        bill.delete()
        removed['bills'] = 1

    removed['calls'], _ = RetellCall.objects.filter(
        client=client, call_id=test_call_id(client)
    ).delete()

    if seeded.get('rate_created') and seeded.get('rate_id'):
        removed['rates'], _ = RateHistory.objects.filter(
            pk=seeded['rate_id'], client=client, start_date=TEST_PERIOD_START
        ).delete()

    return removed


# --- Stripe -------------------------------------------------------------------------------------


def configure_stripe() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY


def default_payment_method(customer_id: str) -> str:
    """The payment method Stripe would collect an invoice against. Empty when there is none."""
    configure_stripe()
    record = stripe.Customer.retrieve(customer_id)
    invoice_settings = getattr(record, 'invoice_settings', None)
    method = getattr(invoice_settings, 'default_payment_method', None)
    # Expanded or not, this is either the id or the object carrying it.
    return getattr(method, 'id', None) or (method or '')


def set_default_payment_method(customer_id: str, payment_method_id: str) -> None:
    """Make one payment method the invoice default. An empty id unsets it."""
    configure_stripe()
    stripe.Customer.modify(
        customer_id, invoice_settings={'default_payment_method': payment_method_id or ''}
    )


def attach_declining_card(customer_id: str) -> str:
    """Attach Stripe's always-declining test card and return the new payment method's own id.

    A card that declines is used rather than no card at all. `_assert_chargeable` refuses to
    raise an invoice for a customer with nothing to charge, so removing the card would stop the
    run before Stripe was ever asked - and the point of the failed path is the decline, the
    retry schedule and the pause that follows it, none of which a refusal reaches.
    """
    configure_stripe()
    attached = stripe.PaymentMethod.attach(DECLINING_TEST_PM, customer=customer_id)
    return attached.id


def detach_payment_method(payment_method_id: str) -> None:
    configure_stripe()
    stripe.PaymentMethod.detach(payment_method_id)


def settle_test_invoice(invoice_id: str) -> str:
    """Leave no collectable test invoice behind. Returns what was done, in a word.

    A draft is deleted, an open one is voided, and a paid or already-voided one is left exactly
    as Stripe has it - voiding is not a refund and pretending otherwise would misreport what the
    test did.
    """
    configure_stripe()
    invoice = stripe.Invoice.retrieve(invoice_id)
    if invoice.status == 'draft':
        stripe.Invoice.delete(invoice_id)
        return 'deleted'
    if invoice.status in ('open', 'uncollectible'):
        stripe.Invoice.void_invoice(invoice_id)
        return 'voided'
    return f'left as {invoice.status}'
