"""Phase 3: one bill per client per month, across both Retell and Twilio."""

from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.rebilling.models import (
    BILLABLE,
    RETELL_LINE,
    TWILIO_LINE,
    UNBILLABLE,
    Bill,
    RateHistory,
    RetellCall,
    TwilioUsageDay,
)
from apps.rebilling.services import calculate_bills

from .test_services import make_client

pytestmark = pytest.mark.django_db

MONTH = '2026-03'


def add_call(client, call_id, duration_ms=120_000, started_at=None, status=BILLABLE, cost=10):
    return RetellCall.objects.create(
        call_id=call_id,
        client=client,
        started_at=started_at or datetime(2026, 3, 10, 12, 0, tzinfo=UTC),
        duration_ms=duration_ms,
        retell_cost_cents=cost,
        call_status='ended',
        billing_status=status,
    )


def add_twilio_day(client, day, minutes='30.00', cents=120):
    return TwilioUsageDay.objects.create(
        client=client,
        subaccount_sid='ACchild',
        usage_date=day,
        call_count=4,
        usage_minutes=Decimal(minutes),
        twilio_price_cents=cents,
    )


# --- Retell line ---------------------------------------------------------------------------


def test_retell_line_bills_at_the_rate_in_effect():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1), voice_rate_cents=25)
    # 120,000ms = 2 minutes, per_second rounding is the default.
    add_call(client, 'call1', duration_ms=120_000)

    result = calculate_bills(MONTH)

    assert result == {
        'period': MONTH,
        'bills_created': 1,
        'already_billed': 0,
        'total_cents': 50,  # 2 minutes * 25c
    }
    bill = Bill.objects.get(client=client, billing_period=MONTH)
    line = bill.lines.get(line_type=RETELL_LINE)
    assert line.quantity_minutes == Decimal('2.00')
    assert line.cost_cents == 10
    assert line.bill_cents == 50


def test_unbillable_calls_are_excluded():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'good', duration_ms=60_000, status=BILLABLE)
    add_call(client, 'bad', duration_ms=60_000, status=UNBILLABLE)

    calculate_bills(MONTH)

    bill = Bill.objects.get(client=client, billing_period=MONTH)
    line = bill.lines.get(line_type=RETELL_LINE)
    # Only the billable call's minute counts, at the default 25c rate.
    assert line.quantity_minutes == Decimal('1.00')
    assert line.bill_cents == 25
    # The unbillable call is untouched - never linked to a bill.
    assert RetellCall.objects.get(call_id='bad').bill is None


def test_mid_month_rate_change_splits_the_bill():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1), voice_rate_cents=20)
    RateHistory.objects.create(client=client, start_date=date(2026, 3, 15), voice_rate_cents=30)
    # Before the change: 1 minute at 20c.
    add_call(
        client, 'before', duration_ms=60_000, started_at=datetime(2026, 3, 5, 0, 0, tzinfo=UTC)
    )
    # On/after the change: 1 minute at 30c.
    add_call(
        client, 'after', duration_ms=60_000, started_at=datetime(2026, 3, 20, 0, 0, tzinfo=UTC)
    )

    calculate_bills(MONTH)

    line = Bill.objects.get(client=client, billing_period=MONTH).lines.get(line_type=RETELL_LINE)
    assert line.quantity_minutes == Decimal('2.00')
    assert line.bill_cents == 20 + 30  # not 2 * 20 and not 2 * 30


def test_calls_outside_the_month_are_not_billed():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'march', started_at=datetime(2026, 3, 10, 0, 0, tzinfo=UTC))
    add_call(client, 'april', started_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))

    calculate_bills(MONTH)

    bill = Bill.objects.get(client=client, billing_period=MONTH)
    assert bill.lines.get(line_type=RETELL_LINE).quantity_minutes == Decimal('2.00')
    assert RetellCall.objects.get(call_id='april').bill is None


def test_call_on_a_day_with_no_rate_is_left_out_of_the_bill():
    client = make_client()
    # No RateHistory at all.
    add_call(client, 'call1')

    result = calculate_bills(MONTH)

    # Nothing billable: no bill is created.
    assert result['bills_created'] == 0
    assert not Bill.objects.filter(client=client).exists()
    assert RetellCall.objects.get(call_id='call1').bill is None


# --- Twilio line -----------------------------------------------------------------------------


def test_twilio_line_bills_at_the_rate_in_effect():
    client = make_client(twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1), call_rate_cents=5)
    add_twilio_day(client, date(2026, 3, 10), minutes='40.00', cents=160)

    calculate_bills(MONTH)

    bill = Bill.objects.get(client=client, billing_period=MONTH)
    line = bill.lines.get(line_type=TWILIO_LINE)
    assert line.quantity_minutes == Decimal('40.00')
    assert line.cost_cents == 160
    assert line.bill_cents == 200  # 40 minutes * 5c


def test_twilio_minutes_are_never_rounded_again():
    client = make_client(twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1), call_rate_cents=10)
    # A fractional-minute total, exactly as Twilio reports it (already rounded on their side).
    add_twilio_day(client, date(2026, 3, 10), minutes='10.50', cents=0)

    calculate_bills(MONTH)

    line = Bill.objects.get(client=client, billing_period=MONTH).lines.get(line_type=TWILIO_LINE)
    assert line.quantity_minutes == Decimal('10.50')
    assert line.bill_cents == 105  # 10.50 * 10c, not rounded up to 11 minutes first


def test_client_without_a_twilio_subaccount_gets_no_twilio_line():
    client = make_client()  # no twilio_subaccount_sid
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'call1')

    calculate_bills(MONTH)

    bill = Bill.objects.get(client=client, billing_period=MONTH)
    assert not bill.lines.filter(line_type=TWILIO_LINE).exists()


def test_client_billed_for_only_one_source_gets_only_that_line():
    client = make_client(twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_twilio_day(client, date(2026, 3, 10), minutes='10.00', cents=50)
    # No Retell calls at all for this client.

    calculate_bills(MONTH)

    bill = Bill.objects.get(client=client, billing_period=MONTH)
    assert bill.lines.count() == 1
    assert bill.lines.get().line_type == TWILIO_LINE


# --- Both sources, totals, skipping and idempotency -------------------------------------------


def test_bill_combines_both_sources_into_one_total():
    client = make_client(twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(
        client=client, start_date=date(2026, 1, 1), voice_rate_cents=25, call_rate_cents=5
    )
    add_call(client, 'call1', duration_ms=120_000)  # 2 min * 25c = 50c
    add_twilio_day(client, date(2026, 3, 10), minutes='20.00')  # 20 min * 5c = 100c

    calculate_bills(MONTH)

    bill = Bill.objects.get(client=client, billing_period=MONTH)
    assert bill.lines.count() == 2
    assert bill.total_cents == 150


def test_client_with_nothing_to_bill_gets_no_bill():
    make_client()  # no calls, no Twilio usage, not even a rate

    result = calculate_bills(MONTH)

    assert result['bills_created'] == 0
    assert result['total_cents'] == 0
    assert not Bill.objects.exists()


def test_inactive_client_is_never_billed():
    client = make_client(active=False)
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'call1')

    calculate_bills(MONTH)

    assert not Bill.objects.filter(client=client).exists()


def test_rerunning_an_already_billed_month_changes_nothing():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'call1', duration_ms=60_000)

    first = calculate_bills(MONTH)
    assert first['bills_created'] == 1

    # A late-arriving call for the same month must not alter the bill already sent.
    add_call(client, 'call2', duration_ms=60_000)
    second = calculate_bills(MONTH)

    assert second['bills_created'] == 0
    assert second['already_billed'] == 1
    assert Bill.objects.filter(client=client, billing_period=MONTH).count() == 1
    bill = Bill.objects.get(client=client, billing_period=MONTH)
    assert bill.lines.get(line_type=RETELL_LINE).quantity_minutes == Decimal('1.00')
    assert RetellCall.objects.get(call_id='call2').bill is None


def test_two_clients_bill_independently():
    cool_breeze = make_client('loc1', client_name='Cool Breeze HVAC')
    branwynn = make_client('loc2', client_name='Branwynn Destinations')
    RateHistory.objects.create(client=cool_breeze, start_date=date(2026, 1, 1), voice_rate_cents=25)
    RateHistory.objects.create(client=branwynn, start_date=date(2026, 1, 1), voice_rate_cents=40)
    add_call(cool_breeze, 'call1', duration_ms=60_000)
    add_call(branwynn, 'call2', duration_ms=60_000)

    result = calculate_bills(MONTH)

    assert result['bills_created'] == 2
    assert Bill.objects.get(client=cool_breeze, billing_period=MONTH).total_cents == 25
    assert Bill.objects.get(client=branwynn, billing_period=MONTH).total_cents == 40


def test_deleting_a_bill_never_deletes_the_underlying_records():
    client = make_client(twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'call1', duration_ms=60_000)
    add_twilio_day(client, date(2026, 3, 10), minutes='10.00', cents=50)

    calculate_bills(MONTH)
    bill = Bill.objects.get(client=client, billing_period=MONTH)
    assert RetellCall.objects.get(call_id='call1').bill == bill
    assert TwilioUsageDay.objects.get(client=client).bill == bill

    bill.delete()

    # The raw call and usage-day rows survive the bill's deletion, only unlinked from it.
    call = RetellCall.objects.get(call_id='call1')
    assert call.bill is None
    assert call.billing_status == BILLABLE
    day = TwilioUsageDay.objects.get(client=client)
    assert day.bill is None
    assert day.usage_minutes == Decimal('10.00')


def test_all_money_is_integer_cents():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1), voice_rate_cents=7)
    # 83 seconds under per_second rounding = 1.3833... minutes, deliberately not a round number.
    add_call(client, 'call1', duration_ms=83_000)

    calculate_bills(MONTH)

    line = Bill.objects.get(client=client, billing_period=MONTH).lines.get(line_type=RETELL_LINE)
    assert isinstance(line.bill_cents, int)
    # 1.3833... * 7 = 9.68...c, rounded half up to the nearest cent.
    assert line.bill_cents == 10


# --- the management command -------------------------------------------------------------------


def test_command_reports_what_it_did():
    client = make_client()
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    add_call(client, 'call1', duration_ms=60_000)

    out = StringIO()
    call_command('calculate_bills', '--month', MONTH, stdout=out)

    assert 'Billing 2026-03: 1 bill(s) created, 0 already billed, 25¢ total.' in out.getvalue()


def test_command_rejects_a_bad_month():
    with pytest.raises(CommandError):
        call_command('calculate_bills', '--month', 'not-a-month', stdout=StringIO())


def test_command_defaults_to_the_current_month():
    # No --month passed; calculate_bills() itself defaults via month_window(None).
    result = calculate_bills(None)
    assert result['bills_created'] == 0
    assert result['period']  # some YYYY-MM was picked without raising
