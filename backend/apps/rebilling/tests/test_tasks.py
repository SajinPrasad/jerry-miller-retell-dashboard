from datetime import date

import httpx
import pytest
import respx
from celery.schedules import crontab
from django.conf import settings

from apps.rebilling.exceptions import RetellError
from apps.rebilling.models import PAID, Bill, RateHistory, RetellCall
from apps.rebilling.tasks import reconcile_retell_calls, run_monthly_billing

from .test_billing import add_call
from .test_reconciliation import LIST_URL, mock_list, page, recent_call
from .test_services import make_client
from .test_stripe_billing import make_billable_client

pytestmark = pytest.mark.django_db

SCHEDULE_NAME = 'reconcile-retell-calls'


@respx.mock
def test_the_daily_task_runs_the_audit():
    make_client()
    mock_list(page(recent_call()))

    result = reconcile_retell_calls()

    assert result == {'seen': 1, 'stored': 1, 'backfilled': 0}
    assert RetellCall.objects.filter(call_id='call1').exists()


@respx.mock
def test_the_daily_task_fails_when_retell_does():
    """Celery records the failure; the next night's window covers the same calls."""
    respx.post(LIST_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(RetellError):
        reconcile_retell_calls()


def test_beat_runs_the_audit_once_a_day():
    """The dotted path must be the task's own name, or beat only finds out at 03:00."""
    entry = settings.CELERY_BEAT_SCHEDULE[SCHEDULE_NAME]

    assert entry['task'] == reconcile_retell_calls.name
    assert entry['schedule'] == crontab(hour=3, minute=0)


def test_the_project_exposes_the_celery_app():
    """Without this import in `config/__init__.py`, `@shared_task` binds to no app."""
    import config

    assert config.celery_app.main == 'config'


# --- the monthly billing task -------------------------------------------------------------------


def test_the_monthly_task_bills_the_month_that_just_ended(fake, monkeypatch):
    monkeypatch.setattr('apps.rebilling.tasks.previous_billing_period', lambda: '2026-03')
    client = make_billable_client(stripe_customer_id='cus_known')
    RateHistory.objects.create(client=client, start_date=date(2026, 1, 1))
    # 2 minutes at the default 25c rate = 50c, right at Stripe's minimum chargeable amount.
    add_call(client, 'call1', duration_ms=120_000)

    result = run_monthly_billing()

    assert result['period'] == '2026-03'
    assert result['calculated']['bills_created'] == 1
    assert result['charged']['charged'] == 1
    bill = Bill.objects.get(client=client, billing_period='2026-03')
    assert bill.status == PAID


def test_the_monthly_task_never_lets_one_client_stop_the_others(fake, monkeypatch):
    monkeypatch.setattr('apps.rebilling.tasks.previous_billing_period', lambda: '2026-03')
    broken = make_client('loc1', client_name='Broken HVAC')
    fine = make_billable_client('loc2', client_name='Fine HVAC', stripe_customer_id='cus_known')
    RateHistory.objects.create(client=broken, start_date=date(2026, 1, 1))
    RateHistory.objects.create(client=fine, start_date=date(2026, 1, 1))
    add_call(broken, 'call1', duration_ms=120_000)
    add_call(fine, 'call2', duration_ms=120_000)

    import apps.rebilling.services as services_module

    real_bill_one_client = services_module._bill_one_client

    def flaky(client, period, start, end):
        if client == broken:
            raise RuntimeError('boom')
        return real_bill_one_client(client, period, start, end)

    monkeypatch.setattr(services_module, '_bill_one_client', flaky)

    result = run_monthly_billing()

    assert result['calculated']['bills_created'] == 1
    assert result['calculated']['failed'] == 1
    assert result['charged']['charged'] == 1
    assert not Bill.objects.filter(client=broken, billing_period='2026-03').exists()
    assert Bill.objects.get(client=fine, billing_period='2026-03').status == PAID


def test_beat_runs_the_monthly_close_on_the_first_at_4am():
    entry = settings.CELERY_BEAT_SCHEDULE['run-monthly-billing']

    assert entry['task'] == run_monthly_billing.name
    assert entry['schedule'].hour == {4}
    assert entry['schedule'].minute == {0}
    assert entry['schedule'].day_of_month == {1}
