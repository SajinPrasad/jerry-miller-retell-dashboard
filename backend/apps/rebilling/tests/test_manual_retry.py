"""The "Retry payment" button: collecting a failed bill now, because an admin asked.

The attempt is the same one the hourly sweep makes - `Invoice.retrieve`, then `Invoice.pay` on
an invoice Stripe does not already hold as paid - made under the bill's own row lock. What is
different is who is waiting for the answer, so a decline comes back as a 200 with the bill on it
rather than as an error, and only a bill in no state to be retried, or a Stripe that could not be
reached, is a failed request.

Stripe is faked by `FakeStripe` and GoHighLevel by the autouse fixtures in `conftest`, so nothing
here makes a network call.
"""

from datetime import UTC, datetime, timedelta

import pytest
import stripe
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency
from apps.rebilling.models import CHARGING, FAILED, PAID, RETRYING, SEND_INVOICE, UNCHARGED, Bill

from .test_stripe_billing import make_bill, make_billable_client

pytestmark = pytest.mark.django_db

MONTH = '2026-03'
DECLINED = stripe.CardError('Your card was declined.', None, 'card_declined')


@pytest.fixture(autouse=True)
def agency():
    """The agency install every one of these sessions is read against."""
    return make_agency()


def retry(api, location_id, blob=VECTOR_AGENCY, **body):
    return api.post(
        reverse('rebilling:admin-client-retry-charge', args=[location_id]),
        data=body or None,
        format='json',
        HTTP_X_GHL_SESSION=blob,
    )


def unpaid_bill(status=RETRYING, retry_count=0, invoice_id='in_test1', client=None, **fields):
    """A bill that has been sent to Stripe and not collected: what the button acts on."""
    client = client or make_billable_client(stripe_customer_id='cus_known')
    bill = make_bill(client, status=status, **fields)
    bill.stripe_invoice_id = invoice_id
    bill.retry_count = retry_count
    bill.failure_reason = 'card_declined'
    if status == RETRYING:
        bill.next_retry_at = timezone.now() + timedelta(hours=12)
    bill.save()
    return bill


# --- what the button collects -----------------------------------------------------------------


def test_a_retry_that_collects_pays_the_bill_and_says_so(fake, api, ghl_custom_value_writes):
    bill = unpaid_bill()

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 200
    body = response.json()
    assert body['bill']['status'] == PAID
    assert body['month'] == MONTH
    bill.refresh_from_db()
    assert bill.status == PAID
    assert bill.paid_at is not None
    assert bill.failure_reason == ''
    # Paying is what puts the client's Retell agent id back in GoHighLevel.
    assert ghl_custom_value_writes == [(bill.client.location_id, 'cv_agent1', 'agent1')]
    assert body['voice'] == {
        'paused': False,
        'paused_at': None,
        'routing_agent_id': 'agent1',
        'error': '',
    }


def test_an_invoice_stripe_already_holds_as_paid_is_not_charged_again(fake, api):
    """The repair for a webhook that never arrived: read first, take Stripe's word, charge
    nothing."""
    fake.invoice_status = 'paid'
    bill = unpaid_bill()

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 200
    assert response.json()['bill']['status'] == PAID
    assert fake.of('Invoice.pay') == []
    bill.refresh_from_db()
    assert bill.status == PAID


def test_a_declined_retry_answers_200_with_the_reason_on_the_bill(fake, api):
    """The card saying no is the outcome the button was pressed to find out, not an error."""
    fake.pay_error = DECLINED
    bill = unpaid_bill(retry_count=0)

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 200
    body = response.json()['bill']
    assert body['status'] == RETRYING
    assert body['failure_reason'] == 'card_declined'
    assert body['retry_count'] == 1
    bill.refresh_from_db()
    assert bill.status == RETRYING


def test_a_manual_retry_spends_one_of_the_automatic_retries(fake, api):
    """One counter, so "retry 2 of 3" on the page stays true and the next sweep is pushed out."""
    fake.pay_error = DECLINED
    bill = unpaid_bill(retry_count=1)
    before = bill.next_retry_at

    retry(api, bill.client.location_id, month=MONTH)

    bill.refresh_from_db()
    assert bill.retry_count == 2
    assert bill.last_retry_at is not None
    assert bill.next_retry_at > before


def test_the_last_retry_declining_fails_the_bill_and_pauses_the_client(
    fake, api, ghl_custom_value_writes
):
    fake.pay_error = DECLINED
    bill = unpaid_bill(retry_count=settings.BILLING_MAX_RETRIES - 1)

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 200
    assert response.json()['bill']['status'] == FAILED
    assert response.json()['voice']['paused'] is True
    # Paused by emptying the agent id, which is what their call routing reads.
    assert ghl_custom_value_writes == [(bill.client.location_id, 'cv_agent1', '')]


def test_a_failed_bill_can_be_retried_by_hand_again_and_again(fake, api):
    """The schedule is used up, but staff asking Stripe one more time is not the schedule."""
    fake.pay_error = DECLINED
    bill = unpaid_bill(status=FAILED, retry_count=settings.BILLING_MAX_RETRIES)

    first = retry(api, bill.client.location_id, month=MONTH)
    second = retry(api, bill.client.location_id, month=MONTH)

    assert (first.status_code, second.status_code) == (200, 200)
    bill.refresh_from_db()
    assert bill.status == FAILED
    # Still failed, and still off the schedule: a manual attempt does not put it back on one.
    assert bill.next_retry_at is None
    assert bill.retry_count == settings.BILLING_MAX_RETRIES + 2
    # Each attempt is its own Stripe request, so none of them can replay the cached decline of
    # the one before (Stripe keeps an idempotent answer, declines included, for 24 hours).
    keys = [call['idempotency_key'] for call in fake.of('Invoice.pay')]
    assert keys == [
        f'bill-{bill.pk}-retry-{settings.BILLING_MAX_RETRIES + 1}',
        f'bill-{bill.pk}-retry-{settings.BILLING_MAX_RETRIES + 2}',
    ]


def test_a_failed_bill_that_collects_resumes_the_client(fake, api, ghl_custom_value_writes):
    client = make_billable_client(stripe_customer_id='cus_known')
    client.voice_paused_at = timezone.now() - timedelta(days=1)
    client.save(update_fields=['voice_paused_at'])
    bill = unpaid_bill(status=FAILED, retry_count=settings.BILLING_MAX_RETRIES, client=client)

    response = retry(api, client.location_id, month=MONTH)

    assert response.json()['bill']['status'] == PAID
    assert response.json()['voice']['paused'] is False
    assert ghl_custom_value_writes == [(client.location_id, 'cv_agent1', 'agent1')]
    bill.refresh_from_db()
    assert bill.status == PAID


def test_another_unpaid_month_keeps_the_client_paused(fake, api, ghl_custom_value_writes):
    """Paying one month must not switch the agent back on while an earlier month is unpaid."""
    client = make_billable_client(stripe_customer_id='cus_known')
    make_bill(client, period='2026-02', status=FAILED)
    bill = unpaid_bill(client=client)

    response = retry(api, client.location_id, month=MONTH)

    assert response.json()['bill']['status'] == PAID
    assert ghl_custom_value_writes == []
    bill.refresh_from_db()
    assert bill.status == PAID


# --- what the button refuses ------------------------------------------------------------------


@pytest.mark.parametrize('status', [UNCHARGED, CHARGING])
def test_a_bill_in_no_state_to_be_retried_is_refused(fake, api, status):
    bill = unpaid_bill(status=status)

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 409
    assert 'retried' in response.json()['detail']
    assert fake.calls == []


def test_a_paid_bill_is_handed_back_rather_than_charged_again(fake, api):
    """Nothing to collect. Answered rather than refused, so a stale page corrects itself."""
    bill = unpaid_bill(status=PAID)

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 200
    assert response.json()['bill']['status'] == PAID
    assert fake.calls == []


def test_a_bill_that_never_reached_stripe_has_no_invoice_to_pay(fake, api):
    bill = unpaid_bill(status=FAILED, invoice_id='')

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 409
    assert 'no Stripe invoice' in response.json()['detail']
    assert fake.calls == []


def test_a_client_not_on_auto_charge_is_not_charged_by_the_button_either(fake, api):
    """`billing_mode` is how someone says "do not take this one's card". A button is not a way
    around that."""
    client = make_billable_client(stripe_customer_id='cus_known', billing_mode=SEND_INVOICE)
    bill = unpaid_bill(client=client)

    response = retry(api, client.location_id, month=MONTH)

    assert response.status_code == 409
    assert fake.calls == []
    bill.refresh_from_db()
    assert bill.status == RETRYING


def test_a_month_with_no_bill_is_a_404(fake, api):
    bill = unpaid_bill()

    response = retry(api, bill.client.location_id, month='2026-01')

    assert response.status_code == 404
    assert fake.calls == []


def test_a_client_that_was_never_mapped_is_a_404(fake, api):
    response = retry(api, 'loc-unknown', month=MONTH)

    assert response.status_code == 404


def test_a_month_that_cannot_be_read_is_a_400(fake, api):
    bill = unpaid_bill()

    response = retry(api, bill.client.location_id, month='March')

    assert response.status_code == 400


def test_a_sub_account_session_cannot_retry_its_own_card(fake, api):
    """A client re-running their own card is not something the agency asked for."""
    client = make_billable_client('loc1', stripe_customer_id='cus_known')
    unpaid_bill(client=client)

    response = retry(api, client.location_id, blob=VECTOR_LOCATION, month=MONTH)

    assert response.status_code == 403
    assert fake.calls == []


def test_a_stripe_that_cannot_be_reached_leaves_the_bill_exactly_as_it_was(fake, api):
    """Not a decline: the card was never reached, so nothing about the schedule is spent."""
    fake.fail_on = 'Invoice.retrieve'
    bill = unpaid_bill(retry_count=1)
    before = bill.next_retry_at

    response = retry(api, bill.client.location_id, month=MONTH)

    assert response.status_code == 502
    bill.refresh_from_db()
    assert bill.status == RETRYING
    assert bill.retry_count == 1
    assert bill.next_retry_at == before
    assert bill.failure_reason == 'card_declined'


def test_the_month_defaults_to_the_one_running_now(fake, api):
    """The page opens on this month, so a body without one means this month."""
    client = make_billable_client(stripe_customer_id='cus_known')
    period = timezone.now().strftime('%Y-%m')
    bill = unpaid_bill(client=client, period=period)

    response = retry(api, client.location_id)

    assert response.status_code == 200
    assert response.json()['month'] == period
    bill.refresh_from_db()
    assert bill.status == PAID


def test_a_retry_the_webhook_settles_first_is_not_charged_twice(fake, api):
    """The lock is held across the Stripe call, so the second request finds the first's outcome."""
    bill = unpaid_bill()
    retry(api, bill.client.location_id, month=MONTH)
    calls_after_first = len(fake.calls)

    second = retry(api, bill.client.location_id, month=MONTH)

    assert second.status_code == 200
    assert second.json()['bill']['status'] == PAID
    assert len(fake.calls) == calls_after_first


def test_the_response_carries_why_a_pause_could_not_be_written(fake, api, ghl_custom_value_writes):
    """A client the page must show as not actually paused, with GoHighLevel's reason."""
    from apps.ghl.exceptions import GHLError

    ghl_custom_value_writes.fails_with = GHLError('GHL is down')
    fake.pay_error = DECLINED
    bill = unpaid_bill(retry_count=settings.BILLING_MAX_RETRIES - 1)

    response = retry(api, bill.client.location_id, month=MONTH)

    body = response.json()
    assert body['bill']['status'] == FAILED
    assert body['voice']['paused'] is False
    assert 'GHL is down' in body['voice']['error']


def test_the_bill_the_sweep_would_have_retried_is_the_bill_the_button_retries(fake, api):
    """Same machinery, same keys: the manual attempt takes the next retry number."""
    fake.pay_error = DECLINED
    bill = unpaid_bill(retry_count=0)

    retry(api, bill.client.location_id, month=MONTH)

    assert fake.of('Invoice.pay')[0]['idempotency_key'] == f'bill-{bill.pk}-retry-1'


def test_the_charge_is_dated_now_not_when_the_bill_was_sent(fake, api):
    bill = unpaid_bill()
    before = datetime.now(UTC)

    retry(api, bill.client.location_id, month=MONTH)

    bill.refresh_from_db()
    assert bill.paid_at >= before
    assert Bill.objects.get(pk=bill.pk).status == PAID
