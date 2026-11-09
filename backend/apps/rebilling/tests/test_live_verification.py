"""The reversible parts of the live-verification workflow: the test-key gate, seeding, teardown.

What the two commands do *between* those - charge a card, empty a live custom value - is live by
definition and is not exercised here; `never_reach_stripe` in this package's conftest would stop
it anyway. What is tested is everything that decides whether the run is safe to start and whether
the cleanup leaves the database as it found it.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.rebilling import live_verification as lv
from apps.rebilling.models import (
    BILLABLE,
    ENDED,
    PAID,
    UNBILLABLE,
    Bill,
    ClientMapping,
    RateHistory,
    RetellCall,
)

TEST_KEY = 'sk_test_abc123'


@pytest.fixture
def payer(db):
    return ClientMapping.objects.create(
        location_id=lv.PAID_PATH_LOCATION_ID,
        client_name=lv.PAID_PATH_NAME,
        retell_agent_ids=['agent_paid'],
        billing_email='payer@example.com',
    )


@pytest.fixture
def failer(db):
    return ClientMapping.objects.create(
        location_id=lv.FAILED_PATH_LOCATION_ID,
        client_name=lv.FAILED_PATH_NAME,
        retell_agent_ids=['agent_failed'],
        billing_email='failer@example.com',
    )


# --- the test-key gate ---------------------------------------------------------------------


@pytest.mark.parametrize('key', ['sk_live_abc', 'rk_test_abc', 'rk_live_abc', ''])
def test_anything_but_a_test_key_is_refused(settings, key):
    settings.STRIPE_SECRET_KEY = key
    with pytest.raises(lv.VerificationError, match='sk_test_'):
        lv.assert_test_mode()


def test_a_test_key_passes(settings):
    settings.STRIPE_SECRET_KEY = TEST_KEY
    lv.assert_test_mode()


@pytest.mark.django_db
def test_the_run_command_stops_on_a_live_key(settings, tmp_path):
    """The gate is the command's first act, before it reads a client or touches anything."""
    settings.STRIPE_SECRET_KEY = 'sk_live_abc123'
    with pytest.raises(CommandError, match='sk_test_'):
        call_command('verify_live_billing', state_file=str(tmp_path / 'state.json'))


@pytest.mark.django_db
def test_the_cleanup_command_stops_on_a_live_key(settings, tmp_path):
    settings.STRIPE_SECRET_KEY = 'sk_live_abc123'
    with pytest.raises(CommandError, match='sk_test_'):
        call_command('verify_live_billing_cleanup', state_file=str(tmp_path / 'state.json'))


# --- safety checks before anything is changed -----------------------------------------------


@pytest.mark.django_db
def test_an_unfinished_run_blocks_a_second_one(settings, tmp_path, payer, failer):
    """A second run would overwrite the only record of the pre-test live values."""
    settings.STRIPE_SECRET_KEY = TEST_KEY
    state = tmp_path / 'state.json'
    state.write_text('{}', encoding='utf-8')

    with pytest.raises(CommandError, match='verify_live_billing_cleanup first'):
        call_command('verify_live_billing', state_file=str(state))


@pytest.mark.django_db
def test_an_unmapped_target_stops_the_run(settings, tmp_path, payer):
    settings.STRIPE_SECRET_KEY = TEST_KEY
    with pytest.raises(CommandError, match=lv.FAILED_PATH_NAME):
        call_command('verify_live_billing', state_file=str(tmp_path / 'state.json'))


@pytest.mark.django_db
def test_a_client_not_on_auto_charge_stops_the_run(settings, tmp_path, payer, failer):
    """charge_bills would skip it, so the run would verify nothing and say it passed."""
    settings.STRIPE_SECRET_KEY = TEST_KEY
    failer.billing_mode = 'send_invoice'
    failer.save(update_fields=['billing_mode'])

    with pytest.raises(CommandError, match='automatic charging|auto charge'):
        call_command('verify_live_billing', state_file=str(tmp_path / 'state.json'))


@pytest.mark.django_db
def test_an_existing_bill_in_the_test_period_stops_the_run(settings, tmp_path, payer, failer):
    """The run charges everything uncharged in the period, so the period must be empty first."""
    settings.STRIPE_SECRET_KEY = TEST_KEY
    Bill.objects.create(client=payer, billing_period=lv.TEST_PERIOD, total_cents=900)

    with pytest.raises(CommandError, match='already has bills'):
        call_command('verify_live_billing', state_file=str(tmp_path / 'state.json'))

    # Nothing was written on the way to that refusal.
    assert not (tmp_path / 'state.json').exists()


@pytest.mark.django_db
def test_the_cleanup_command_will_not_guess_without_a_backup(settings, tmp_path):
    settings.STRIPE_SECRET_KEY = TEST_KEY
    with pytest.raises(CommandError, match='No backup file'):
        call_command('verify_live_billing_cleanup', state_file=str(tmp_path / 'missing.json'))


# --- seeding -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_seeding_makes_one_billable_call_in_the_test_period(payer):
    seeded = lv.seed_usage(payer)

    call = RetellCall.objects.get(call_id=seeded['call_id'])
    assert call.client == payer
    assert call.call_status == ENDED
    assert call.billing_status == BILLABLE
    assert call.duration_ms == lv.TEST_CALL_MINUTES * 60_000
    assert call.started_at == lv.TEST_CALL_AT
    assert call.started_at.strftime('%Y-%m') == lv.TEST_PERIOD


@pytest.mark.django_db
def test_seeding_bills_over_the_stripe_minimum(payer):
    """A bill under 50c is skipped by charge_bills, so it would never reach a card at all."""
    from apps.rebilling.services import calculate_bills
    from apps.rebilling.stripe_billing import MINIMUM_CHARGE_CENTS

    lv.seed_usage(payer)
    calculate_bills(lv.TEST_PERIOD)

    bill = lv.test_bill(payer)
    assert bill is not None
    assert bill.total_cents >= MINIMUM_CHARGE_CENTS
    assert bill.total_cents == lv.TEST_CALL_MINUTES * 25


@pytest.mark.django_db
def test_seeding_adds_a_rate_the_test_period_can_use(payer):
    """A client only has rates from today, and a 2025 call under them bills at nothing."""
    seeded = lv.seed_usage(payer)

    assert seeded['rate_created'] is True
    rate = RateHistory.objects.get(pk=seeded['rate_id'])
    assert rate.start_date == lv.TEST_PERIOD_START
    assert rate.client == payer


@pytest.mark.django_db
def test_seeding_leaves_a_rate_row_that_was_already_there(payer):
    existing = RateHistory.objects.create(
        client=payer, start_date=lv.TEST_PERIOD_START, voice_rate_cents=99, call_rate_cents=7
    )

    seeded = lv.seed_usage(payer)

    assert seeded['rate_created'] is False
    assert seeded['rate_id'] == existing.pk
    existing.refresh_from_db()
    assert existing.voice_rate_cents == 99


@pytest.mark.django_db
def test_seeding_twice_changes_nothing(payer):
    first = lv.seed_usage(payer)
    second = lv.seed_usage(payer)

    assert second['call_created'] is False
    assert second['rate_created'] is False
    assert first['call_id'] == second['call_id']
    assert RetellCall.objects.filter(client=payer).count() == 1


# --- teardown ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_synthetic_rows_removes_the_bill_the_call_and_the_rate(payer):
    from apps.rebilling.services import calculate_bills

    seeded = lv.seed_usage(payer)
    calculate_bills(lv.TEST_PERIOD)
    bill = lv.test_bill(payer)
    assert bill.lines.count() == 1

    removed = lv.delete_synthetic(payer, seeded)

    assert removed == {'bills': 1, 'calls': 1, 'rates': 1}
    assert lv.test_bill(payer) is None
    assert not RetellCall.objects.filter(call_id=seeded['call_id']).exists()
    assert not RateHistory.objects.filter(pk=seeded['rate_id']).exists()


@pytest.mark.django_db
def test_teardown_never_touches_real_data_in_another_period(payer):
    """The one thing this whole exercise must not do: lose a real bill or a real call."""
    real_call = RetellCall.objects.create(
        call_id='call_real_november',
        client=payer,
        started_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
        duration_ms=120_000,
        call_status=ENDED,
        billing_status=BILLABLE,
    )
    real_bill = Bill.objects.create(
        client=payer, billing_period='2026-11', total_cents=5_000, status=PAID
    )
    real_rate = RateHistory.objects.create(
        client=payer, start_date=date(2026, 11, 1), voice_rate_cents=30, call_rate_cents=6
    )

    seeded = lv.seed_usage(payer)
    lv.delete_synthetic(payer, seeded)

    real_call.refresh_from_db()
    real_bill.refresh_from_db()
    real_rate.refresh_from_db()
    assert real_bill.total_cents == 5_000
    assert real_rate.voice_rate_cents == 30


@pytest.mark.django_db
def test_teardown_keeps_a_rate_row_the_run_did_not_create(payer):
    existing = RateHistory.objects.create(
        client=payer, start_date=lv.TEST_PERIOD_START, voice_rate_cents=99, call_rate_cents=7
    )
    seeded = lv.seed_usage(payer)

    removed = lv.delete_synthetic(payer, seeded)

    assert removed['rates'] == 0
    assert RateHistory.objects.filter(pk=existing.pk).exists()


@pytest.mark.django_db
def test_teardown_is_safe_to_repeat(payer):
    seeded = lv.seed_usage(payer)
    lv.delete_synthetic(payer, seeded)

    assert lv.delete_synthetic(payer, seeded) == {'bills': 0, 'calls': 0, 'rates': 0}


@pytest.mark.django_db
def test_teardown_only_ever_deletes_its_own_call_id(payer):
    """Deleting by client and period would take a real call with it; the id prefix cannot."""
    lookalike = RetellCall.objects.create(
        call_id='call_someone_elses',
        client=payer,
        started_at=lv.TEST_CALL_AT,
        duration_ms=60_000,
        call_status=ENDED,
        billing_status=UNBILLABLE,
    )

    seeded = lv.seed_usage(payer)
    lv.delete_synthetic(payer, seeded)

    assert RetellCall.objects.filter(pk=lookalike.pk).exists()
    assert lv.test_call_id(payer).startswith(lv.TEST_CALL_PREFIX)


# --- the backup file -------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_backup_file_round_trips(tmp_path):
    path = tmp_path / 'state.json'
    state = {
        'clients': {'loc1': {'agent_custom_value': {'id': 'cv1', 'name': 'X', 'value': 'ag_1'}}},
        'failed_path': {'original_default_payment_method': 'pm_real'},
    }

    lv.save_state(path, state)

    assert lv.load_state(path) == state
    assert json.loads(path.read_text(encoding='utf-8')) == state


def test_an_unreadable_backup_file_is_refused_rather_than_ignored(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{not json', encoding='utf-8')

    with pytest.raises(lv.VerificationError, match='not readable JSON'):
        lv.load_state(path)


def test_the_backup_path_defaults_beside_manage_py(settings):
    assert lv.state_path() == settings.BASE_DIR / lv.STATE_FILENAME
    assert lv.state_path('/tmp/elsewhere.json').name == 'elsewhere.json'


# --- the period itself -------------------------------------------------------------------------


def test_the_test_period_is_in_the_past_and_inside_the_admin_pickers_window():
    """2025-12 is chosen so the failed bill is still reachable in /admin to press Retry on."""
    from apps.rebilling.services import month_window

    start, end = month_window(lv.TEST_PERIOD)
    assert (start, end) == (date(2025, 12, 1), date(2025, 12, 31))
    assert start <= lv.TEST_CALL_AT.date() <= end
    assert lv.TEST_PERIOD_START == start


@pytest.mark.django_db
def test_the_synthetic_call_is_billable_minutes_not_a_rounding_edge(payer, settings):
    settings.RETELL_ROUNDING = 'per_second'
    lv.seed_usage(payer)

    call = RetellCall.objects.get(call_id=lv.test_call_id(payer))
    assert call.billable_minutes == Decimal(lv.TEST_CALL_MINUTES)
