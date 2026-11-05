"""Automatic retries of a declined charge, and the GoHighLevel status that follows them.

A first decline puts the bill into `retrying` on a schedule rather than leaving it failed. Each
due retry is made by `retry_due_bills`, and the bill is `failed` for a person once the schedule
is used up. Every outcome that matters to GoHighLevel is written twice: to a custom field on the
client's billing contact, which drives Jerry's dunning workflows, and to a custom value in the
client's own sub-account, which gates call routing directly. These tests record both instead of
sending them (see `ghl_contact_writes` and `ghl_custom_value_writes`).

Stripe is faked by `FakeStripe` and the guard in `test_stripe_billing` still stops anything
unfaked from going out, so these tests make no network calls.
"""

from datetime import UTC, date, datetime, timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
import stripe
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.ghl.exceptions import GHLError
from apps.rebilling import services
from apps.rebilling.models import FAILED, PAID, RETRYING, UNCHARGED
from apps.rebilling.stripe_billing import retry_due_bills
from apps.rebilling.tasks import retry_failed_charges

from .test_stripe_billing import charging_bill, event_body, make_bill, make_billable_client, post

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)
# Who the status is written against, and which field of theirs: the billing contact in the main
# sub-account, found by the client's billing email.
BILLING_EMAIL = 'ops@coolbreeze.test'
STATUS_FIELD = settings.GHL_RETELL_STATUS_CUSTOM_FIELD_ID
# The other write: the call-routing toggle in the client's own sub-account.
TOGGLE_VALUE_ID = settings.GHL_RETELL_STATUS_CUSTOM_VALUE_ID
INTERVAL = timedelta(hours=settings.BILLING_RETRY_INTERVAL_HOURS)
DECLINED = stripe.CardError('Your card was declined.', None, 'card_declined')


def retrying_bill(
    retry_count=0,
    next_retry_at=NOW - timedelta(minutes=1),
    status=RETRYING,
    client=None,
    period='2026-03',
    invoice_id='in_test1',
):
    """A bill part-way through its retries. Due one minute before NOW unless told otherwise."""
    client = client or make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client, period=period, status=status)
    bill.stripe_invoice_id = invoice_id
    bill.retry_count = retry_count
    bill.next_retry_at = next_retry_at
    bill.save()
    return bill


# --- what a decline and a payment do to the schedule (the webhook) ----------------------------


def test_a_first_decline_schedules_the_first_retry_one_interval_later(api):
    bill = charging_bill()
    before = timezone.now()

    post(api, event_body('invoice.payment_failed'))

    bill.refresh_from_db()
    assert bill.status == RETRYING
    assert bill.retry_count == 0
    assert before + INTERVAL <= bill.next_retry_at <= timezone.now() + INTERVAL


def test_a_further_decline_while_retrying_keeps_the_schedule_as_it_was(api):
    bill = retrying_bill(retry_count=1, next_retry_at=NOW + INTERVAL)

    response = post(api, event_body('invoice.payment_failed'))

    assert response.status_code == 204
    bill.refresh_from_db()
    assert bill.status == RETRYING
    assert bill.retry_count == 1
    assert bill.next_retry_at == NOW + INTERVAL


def test_a_decline_on_a_bill_already_failed_changes_nothing_but_the_reason(api):
    bill = retrying_bill(retry_count=3, next_retry_at=None, status=FAILED)

    post(api, event_body('invoice.payment_failed'))

    bill.refresh_from_db()
    assert bill.status == FAILED
    assert bill.next_retry_at is None


def test_being_paid_after_a_retry_clears_the_schedule_and_marks_the_client_active(
    api, ghl_contact_writes
):
    bill = retrying_bill(retry_count=2, next_retry_at=NOW + INTERVAL)

    post(api, event_body('invoice.paid'))

    bill.refresh_from_db()
    assert bill.status == PAID
    assert bill.next_retry_at is None
    assert ghl_contact_writes == [(BILLING_EMAIL, STATUS_FIELD, 'active')]


# --- what a retry run does -----------------------------------------------------------------------


def test_a_bill_not_yet_due_is_not_retried(fake):
    bill = retrying_bill(next_retry_at=NOW + timedelta(hours=1))

    result = retry_due_bills(NOW)

    assert result == {'retried': 0, 'paid': 0, 'retrying': 0, 'failed': 0}
    assert fake.of('Invoice.pay') == []
    bill.refresh_from_db()
    assert bill.retry_count == 0


def test_a_bill_that_is_not_retrying_is_never_picked_up(fake):
    retrying_bill(status=UNCHARGED, next_retry_at=NOW - INTERVAL)
    retrying_bill(status=FAILED, client=make_billable_client('loc2'), next_retry_at=None)

    assert retry_due_bills(NOW)['retried'] == 0
    assert fake.of('Invoice.pay') == []


def test_a_due_retry_is_counted_scheduled_and_paid_with_a_key_for_that_retry(fake):
    fake.pay_error = DECLINED
    bill = retrying_bill(retry_count=0)

    result = retry_due_bills(NOW)

    assert result == {'retried': 1, 'paid': 0, 'retrying': 1, 'failed': 0}
    bill.refresh_from_db()
    assert bill.retry_count == 1
    assert bill.last_retry_at == NOW
    assert bill.next_retry_at == NOW + INTERVAL
    assert bill.status == RETRYING
    assert bill.failure_reason == 'card_declined'
    assert fake.of('Invoice.pay') == [
        {'invoice_id': 'in_test1', 'idempotency_key': f'bill-{bill.pk}-retry-1'}
    ]


def test_a_retry_that_collects_marks_the_bill_paid_and_the_client_active(fake, ghl_contact_writes):
    bill = retrying_bill(retry_count=1)

    result = retry_due_bills(NOW)

    assert result == {'retried': 1, 'paid': 1, 'retrying': 0, 'failed': 0}
    bill.refresh_from_db()
    assert bill.status == PAID
    assert bill.paid_at == NOW
    assert bill.failure_reason == ''
    assert bill.next_retry_at is None
    assert ghl_contact_writes == [(BILLING_EMAIL, STATUS_FIELD, 'active')]


def test_an_invoice_stripe_already_shows_as_paid_is_not_charged_again(fake, ghl_contact_writes):
    fake.invoice_status = 'paid'
    bill = retrying_bill(retry_count=1)

    result = retry_due_bills(NOW)

    assert result['paid'] == 1
    assert fake.of('Invoice.pay') == []
    bill.refresh_from_db()
    assert bill.status == PAID


def test_the_last_retry_that_fails_uses_up_the_schedule_and_marks_the_client_failed(
    fake, ghl_contact_writes
):
    fake.pay_error = DECLINED
    bill = retrying_bill(retry_count=settings.BILLING_MAX_RETRIES - 1)

    result = retry_due_bills(NOW)

    assert result == {'retried': 1, 'paid': 0, 'retrying': 0, 'failed': 1}
    bill.refresh_from_db()
    assert bill.status == FAILED
    assert bill.retry_count == settings.BILLING_MAX_RETRIES
    assert bill.next_retry_at is None
    assert ghl_contact_writes == [(BILLING_EMAIL, STATUS_FIELD, 'failed')]


def test_a_retry_repeated_at_the_same_moment_is_not_made_twice(fake):
    fake.pay_error = DECLINED
    retrying_bill(retry_count=0)

    retry_due_bills(NOW)
    second = retry_due_bills(NOW)

    assert second['retried'] == 0
    assert len(fake.of('Invoice.pay')) == 1


def test_a_retry_that_cannot_reach_stripe_counts_as_a_declined_attempt(fake):
    fake.pay_error = stripe.APIConnectionError('Stripe is unreachable.')
    bill = retrying_bill(retry_count=0)

    result = retry_due_bills(NOW)

    assert result['retrying'] == 1
    bill.refresh_from_db()
    assert bill.status == RETRYING
    assert bill.retry_count == 1
    assert bill.failure_reason == 'APIConnectionError'


def test_the_schedule_runs_in_order_until_it_is_used_up(api, fake, ghl_contact_writes):
    """Three retries spaced an interval apart, then failed: what the admin page walks through."""
    bill = charging_bill()
    post(api, event_body('invoice.payment_failed'))
    bill.refresh_from_db()
    first_due = bill.next_retry_at
    fake.pay_error = DECLINED

    for attempt in range(1, settings.BILLING_MAX_RETRIES + 1):
        due = first_due + INTERVAL * (attempt - 1)
        assert retry_due_bills(due - timedelta(minutes=1))['retried'] == 0
        assert retry_due_bills(due)['retried'] == 1
        bill.refresh_from_db()
        assert bill.retry_count == attempt

    assert bill.status == FAILED
    assert bill.next_retry_at is None
    assert ghl_contact_writes == [(BILLING_EMAIL, STATUS_FIELD, 'failed')]


def test_a_declined_retry_for_one_client_does_not_stop_the_others(fake, monkeypatch):
    def pay(invoice_id, **kwargs):
        if invoice_id == 'in_declined':
            raise DECLINED
        return SimpleNamespace(id=invoice_id, status='paid')

    monkeypatch.setattr(stripe.Invoice, 'pay', pay)
    declined = retrying_bill(invoice_id='in_declined')
    other = retrying_bill(
        client=make_billable_client('loc2', stripe_customer_id='cus_two'), invoice_id='in_ok'
    )

    result = retry_due_bills(NOW)

    assert result == {'retried': 2, 'paid': 1, 'retrying': 1, 'failed': 0}
    declined.refresh_from_db()
    other.refresh_from_db()
    assert declined.status == RETRYING
    assert other.status == PAID


# --- the GoHighLevel status ----------------------------------------------------------------------


def test_a_paid_bill_does_not_reactivate_a_client_whose_earlier_bill_is_still_failed(
    fake, ghl_contact_writes
):
    client = make_billable_client(stripe_customer_id='cus_known')
    retrying_bill(client=client, retry_count=3, next_retry_at=None, status=FAILED, period='2026-02')
    current = retrying_bill(client=client, retry_count=1)

    retry_due_bills(NOW)

    current.refresh_from_db()
    assert current.status == PAID
    assert ghl_contact_writes == []


def test_a_bill_still_retrying_leaves_the_client_status_alone(fake, ghl_contact_writes):
    fake.pay_error = DECLINED
    retrying_bill(retry_count=0)

    retry_due_bills(NOW)

    assert ghl_contact_writes == []


def test_a_ghl_error_is_logged_and_does_not_undo_the_outcome(fake, ghl_contact_writes):
    ghl_contact_writes.fails_with = GHLError('GHL is down')
    bill = retrying_bill(retry_count=1)

    result = retry_due_bills(NOW)

    assert result['paid'] == 1
    bill.refresh_from_db()
    assert bill.status == PAID


def test_a_client_with_no_billing_email_has_no_contact_to_write_the_status_to(
    fake, ghl_contact_writes
):
    """The status is found by email, so without one there is nothing to write it against.

    Nothing is raised and nothing is written: the bill is paid either way, and inventing a
    contact to carry the status would tell a workflow about the wrong client.
    """
    client = make_billable_client(billing_email='', stripe_customer_id='cus_known')
    bill = retrying_bill(client=client, retry_count=1)

    retry_due_bills(NOW)

    bill.refresh_from_db()
    assert bill.status == PAID
    assert ghl_contact_writes == []


# --- the Retell call-routing toggle, alongside the contact field -------------------------------


def test_a_retry_that_collects_also_flips_the_toggle_on(fake, ghl_custom_value_writes):
    bill = retrying_bill(retry_count=1)

    retry_due_bills(NOW)

    assert ghl_custom_value_writes == [(bill.client.location_id, TOGGLE_VALUE_ID, 'on')]


def test_the_schedule_used_up_also_flips_the_toggle_off(fake, ghl_custom_value_writes):
    fake.pay_error = DECLINED
    bill = retrying_bill(retry_count=settings.BILLING_MAX_RETRIES - 1)

    retry_due_bills(NOW)

    assert ghl_custom_value_writes == [(bill.client.location_id, TOGGLE_VALUE_ID, 'off')]


def test_a_bill_still_retrying_does_not_flip_the_toggle_either(fake, ghl_custom_value_writes):
    fake.pay_error = DECLINED
    retrying_bill(retry_count=0)

    retry_due_bills(NOW)

    assert ghl_custom_value_writes == []


def test_the_toggle_is_flipped_even_without_a_billing_email(fake, ghl_custom_value_writes):
    """Unlike the contact field, the toggle is per sub-account - there is nothing to find by
    email, so a missing billing email does not stop it."""
    client = make_billable_client(billing_email='', stripe_customer_id='cus_known')
    bill = retrying_bill(client=client, retry_count=1)

    retry_due_bills(NOW)

    assert ghl_custom_value_writes == [(client.location_id, TOGGLE_VALUE_ID, 'on')]
    bill.refresh_from_db()
    assert bill.status == PAID


@override_settings(GHL_RETELL_STATUS_CUSTOM_VALUE_ID='')
def test_a_blank_custom_value_id_skips_the_toggle_write(fake, ghl_custom_value_writes):
    """Blank means the custom value has not been created in GHL yet - nothing to write to."""
    retrying_bill(retry_count=1)

    retry_due_bills(NOW)

    assert ghl_custom_value_writes == []


def test_a_failed_toggle_write_is_logged_and_does_not_undo_the_outcome(
    fake, ghl_custom_value_writes
):
    ghl_custom_value_writes.fails_with = GHLError('GHL is down')
    bill = retrying_bill(retry_count=1)

    result = retry_due_bills(NOW)

    assert result['paid'] == 1
    bill.refresh_from_db()
    assert bill.status == PAID


# --- the task, the command and the schedule ----------------------------------------------------


def test_the_scheduled_task_runs_the_retry_run(fake):
    fake.pay_error = DECLINED
    bill = retrying_bill(retry_count=0, next_retry_at=timezone.now() - timedelta(minutes=1))

    result = retry_failed_charges()

    assert result['retried'] == 1
    bill.refresh_from_db()
    assert bill.retry_count == 1


def test_the_command_reports_what_it_retried(fake):
    fake.pay_error = DECLINED
    retrying_bill(retry_count=0)
    out = StringIO()

    call_command('retry_failed_charges', stdout=out)

    assert 'Retries: 1 made, 0 paid, 1 still retrying, 0 used up and failed.' in out.getvalue()


def test_the_retry_run_is_hourly_on_the_beat_schedule():
    entry = settings.CELERY_BEAT_SCHEDULE['retry-failed-charges']

    assert entry['task'] == 'apps.rebilling.tasks.retry_failed_charges'
    assert entry['schedule'].minute == {15}
    assert entry['schedule'].hour == set(range(24))


# --- what the admin page reads -------------------------------------------------------------------


def test_the_bill_row_carries_the_retry_count_and_next_retry_for_the_page():
    bill = retrying_bill(retry_count=2, next_retry_at=NOW + INTERVAL)

    row = services._bill_row(bill.client, date(2026, 3, 1))

    assert row == {
        'status': RETRYING,
        'total_cents': bill.total_cents,
        'failure_reason': '',
        'retry_count': 2,
        'max_retries': settings.BILLING_MAX_RETRIES,
        'next_retry_at': (NOW + INTERVAL).isoformat(),
    }


def test_a_bill_with_no_pending_retry_has_no_next_retry_for_the_page():
    bill = retrying_bill(retry_count=0, next_retry_at=None, status=PAID)

    row = services._bill_row(bill.client, date(2026, 3, 1))

    assert row['status'] == PAID
    assert row['next_retry_at'] is None
