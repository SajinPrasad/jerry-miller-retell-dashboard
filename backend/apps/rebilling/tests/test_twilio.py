from datetime import date, datetime
from decimal import Decimal
from io import StringIO

import httpx
import pytest
import respx
from celery.schedules import crontab
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.rebilling.models import RetellCall, TwilioUsageDay, UnassignedCall
from apps.rebilling.services import TwilioUsageService
from apps.rebilling.tasks import pull_twilio_usage

from .test_services import call_payload, make_client

pytestmark = pytest.mark.django_db

BASE = settings.TWILIO_API_BASE_URL
SUBACCOUNT = 'ACclient1'
SCHEDULE_NAME = 'pull-twilio-usage'


@pytest.fixture
def service():
    return TwilioUsageService()


def usage_url(subaccount_sid=SUBACCOUNT):
    return f'{BASE}/2010-04-01/Accounts/{subaccount_sid}/Usage/Records/Daily.json'


def make_twilio_client(location_id='loc1', subaccount_sid=SUBACCOUNT, **overrides):
    return make_client(location_id, twilio_subaccount_sid=subaccount_sid, **overrides)


def record(start_date='2026-10-20', **overrides):
    """A `calls` usage record, with the string-typed numbers Twilio really sends."""
    row = {
        'account_sid': SUBACCOUNT,
        'category': 'calls',
        'description': 'Voice Minutes',
        'start_date': start_date,
        'end_date': start_date,
        'as_of': '2026-10-23T00:00:00+00:00',
        'count': '12',
        'count_unit': 'calls',
        'usage': '34.5',
        'usage_unit': 'minutes',
        'price': '1.2345',
        'price_unit': 'usd',
    }
    row.update(overrides)
    return row


def page(*records, next_page_uri=None):
    return {'usage_records': list(records), 'next_page_uri': next_page_uri}


def mock_usage(*pages, subaccount_sid=SUBACCOUNT):
    responses = [httpx.Response(200, json=p) for p in pages]
    return respx.get(usage_url(subaccount_sid)).mock(side_effect=responses)


# --- ingestion ---------------------------------------------------------------------------


@respx.mock
def test_stores_a_day_of_call_usage(service):
    client = make_twilio_client()
    mock_usage(page(record()))

    result = service.pull()

    assert result == {'clients': 1, 'days': 1, 'updated': 0, 'failed': 0}
    day = TwilioUsageDay.objects.get()
    assert day.client == client
    assert day.usage_date == date(2026, 10, 20)
    assert day.category == 'calls'
    assert day.call_count == 12
    assert day.subaccount_sid == SUBACCOUNT


@respx.mock
def test_parses_the_string_numbers_as_exact_decimals(service):
    """Twilio sends usage and price as strings; float() here would put drift into a bill."""
    make_twilio_client()
    mock_usage(page(record(usage='34.5', price='1.2345')))

    service.pull()

    day = TwilioUsageDay.objects.get()
    assert day.usage_minutes == Decimal('34.50')
    assert day.twilio_price_cents == 123  # 1.2345 dollars -> 123.45c -> 123


@respx.mock
def test_a_negative_price_is_stored_as_a_positive_cost(service):
    make_twilio_client()
    mock_usage(page(record(price='-2.00')))

    service.pull()

    assert TwilioUsageDay.objects.get().twilio_price_cents == 200


@respx.mock
def test_missing_usage_and_price_become_zero(service):
    make_twilio_client()
    mock_usage(page(record(usage=None, price=None, count=None)))

    service.pull()

    day = TwilioUsageDay.objects.get()
    assert day.usage_minutes == Decimal(0)
    assert day.twilio_price_cents == 0
    assert day.call_count == 0


@respx.mock
def test_keeps_twilios_as_of_stamp(service):
    make_twilio_client()
    mock_usage(page(record()))

    service.pull()

    assert TwilioUsageDay.objects.get().as_of == datetime(
        2026, 10, 23, tzinfo=timezone.get_fixed_timezone(0)
    )


@respx.mock
def test_an_unreadable_as_of_is_left_empty_rather_than_failing(service):
    """It is a freshness signal, not a billing input, so it must not cost us the row."""
    make_twilio_client()
    mock_usage(page(record(as_of='whenever')))

    result = service.pull()

    assert result['days'] == 1
    assert TwilioUsageDay.objects.get().as_of is None


@respx.mock
def test_a_record_without_a_start_date_is_skipped(service):
    make_twilio_client()
    mock_usage(page(record(start_date=None), record(start_date='2026-10-21')))

    result = service.pull()

    assert result['days'] == 1
    assert TwilioUsageDay.objects.get().usage_date == date(2026, 10, 21)


# --- the request Twilio actually receives ------------------------------------------------


@respx.mock
def test_sends_the_window_category_and_auth(service):
    make_twilio_client()
    route = mock_usage(page())

    service.pull(start_date=date(2026, 10, 1), end_date=date(2026, 10, 3))

    request = route.calls.last.request
    assert request.url.params['Category'] == 'calls'
    assert request.url.params['StartDate'] == '2026-10-01'
    assert request.url.params['EndDate'] == '2026-10-03'
    assert request.url.params['PageSize'] == '1000'
    # Twilio's own default is true, which would bill a client for their subaccounts' usage.
    assert request.url.params['IncludeSubaccounts'] == 'false'
    assert request.headers['Authorization'].startswith('Basic ')


@respx.mock
def test_defaults_to_a_three_day_rolling_window(service):
    make_twilio_client()
    route = mock_usage(page())

    service.pull()

    params = route.calls.last.request.url.params
    today = timezone.now().date()
    assert params['EndDate'] == today.isoformat()
    assert params['StartDate'] == (today - timezone.timedelta(days=2)).isoformat()


@respx.mock
def test_pages_on_the_uri_twilio_hands_back(service):
    make_twilio_client()
    next_uri = f'/2010-04-01/Accounts/{SUBACCOUNT}/Usage/Records/Daily.json?Page=1&PageToken=xyz'
    route = mock_usage(
        page(record('2026-10-20'), next_page_uri=next_uri),
        page(record('2026-10-21')),
    )

    result = service.pull()

    assert result['days'] == 2
    assert route.call_count == 2
    assert route.calls[1].request.url.params['PageToken'] == 'xyz'
    assert set(TwilioUsageDay.objects.values_list('usage_date', flat=True)) == {
        date(2026, 10, 20),
        date(2026, 10, 21),
    }


# --- re-pulling updates, it does not duplicate -------------------------------------------


@respx.mock
def test_a_repull_updates_the_day_rather_than_duplicating_it(service):
    """The opposite of store_call(): Twilio keeps revising a day after it ends."""
    make_twilio_client()
    mock_usage(page(record(usage='10', price='0.50', count='3')))
    service.pull()
    respx.reset()
    mock_usage(page(record(usage='12.5', price='0.75', count='4')))

    result = service.pull()

    assert result == {'clients': 1, 'days': 1, 'updated': 1, 'failed': 0}
    day = TwilioUsageDay.objects.get()
    assert day.usage_minutes == Decimal('12.50')
    assert day.twilio_price_cents == 75
    assert day.call_count == 4


@respx.mock
def test_two_clients_keep_their_own_rows_for_the_same_day(service):
    make_twilio_client('loc1', 'ACclient1')
    make_twilio_client('loc2', 'ACclient2', client_name='Branwynn')
    mock_usage(page(record(account_sid='ACclient1')), subaccount_sid='ACclient1')
    mock_usage(page(record(account_sid='ACclient2')), subaccount_sid='ACclient2')

    result = service.pull()

    assert result['clients'] == 2 and result['days'] == 2
    assert TwilioUsageDay.objects.count() == 2


# --- which clients get pulled ------------------------------------------------------------


@respx.mock
def test_a_client_without_a_twilio_subaccount_is_not_pulled(service):
    make_client()  # Retell-only client: no twilio_subaccount_sid
    route = respx.get(url__regex=r'.*Usage/Records.*')

    result = service.pull()

    assert result['clients'] == 0
    assert not route.called


@respx.mock
def test_an_inactive_client_is_not_pulled(service):
    make_twilio_client(active=False)
    route = respx.get(url__regex=r'.*Usage/Records.*')

    result = service.pull()

    assert result['clients'] == 0
    assert not route.called


# --- errors are per client ---------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize('status_code', [429, 500, 401])
def test_one_clients_failure_does_not_cost_the_others_their_day(service, status_code):
    make_twilio_client('loc1', 'ACclient1')
    make_twilio_client('loc2', 'ACclient2', client_name='Branwynn')
    respx.get(usage_url('ACclient1')).mock(return_value=httpx.Response(status_code))
    mock_usage(page(record(account_sid='ACclient2')), subaccount_sid='ACclient2')

    result = service.pull()

    assert result == {'clients': 2, 'days': 1, 'updated': 0, 'failed': 1}
    assert TwilioUsageDay.objects.get().client.location_id == 'loc2'


@respx.mock
def test_a_connection_failure_is_counted_not_raised(service):
    make_twilio_client()
    respx.get(usage_url()).mock(side_effect=httpx.ConnectError('no route'))

    assert service.pull()['failed'] == 1


@respx.mock
def test_a_body_that_is_not_json_is_counted_not_raised(service):
    make_twilio_client()
    respx.get(usage_url()).mock(return_value=httpx.Response(200, content=b'<html>nope</html>'))

    assert service.pull()['failed'] == 1


@respx.mock
def test_days_from_an_earlier_page_survive_a_later_failure(service):
    make_twilio_client()
    next_uri = f'/2010-04-01/Accounts/{SUBACCOUNT}/Usage/Records/Daily.json?Page=1'
    respx.get(usage_url()).mock(
        side_effect=[
            httpx.Response(200, json=page(record('2026-10-20'), next_page_uri=next_uri)),
            httpx.Response(500),
        ]
    )

    result = service.pull()

    # The whole client is abandoned on a failed page: a half-read window would otherwise look
    # like a settled one to Phase 3.
    assert result['failed'] == 1 and result['days'] == 0
    assert not TwilioUsageDay.objects.exists()


# --- the Retell path is untouched ---------------------------------------------------------


@respx.mock
def test_the_twilio_pull_leaves_retell_rows_alone(service):
    """Both paths share ClientMapping and nothing else."""
    from apps.rebilling.services import store_call

    client = make_twilio_client()
    store_call(call_payload())
    mock_usage(page(record()))

    service.pull()

    assert RetellCall.objects.get().client == client
    assert RetellCall.objects.get().duration_ms == 15791
    assert not UnassignedCall.objects.exists()
    assert TwilioUsageDay.objects.count() == 1


# --- command and schedule ------------------------------------------------------------------


@respx.mock
def test_command_reports_what_it_did():
    make_twilio_client()
    mock_usage(page(record()))
    out = StringIO()

    call_command('pull_twilio_usage', stdout=out)

    assert '1 clients, 1 days stored, 0 updated.' in out.getvalue()


@respx.mock
def test_command_takes_an_explicit_range_for_the_month_close():
    make_twilio_client()
    route = mock_usage(page(record()))

    call_command(
        'pull_twilio_usage', '--start', '2026-09-01', '--end', '2026-09-30', stdout=StringIO()
    )

    params = route.calls.last.request.url.params
    assert params['StartDate'] == '2026-09-01' and params['EndDate'] == '2026-09-30'


def test_command_rejects_a_backwards_range():
    with pytest.raises(CommandError):
        call_command('pull_twilio_usage', '--start', '2026-09-30', '--end', '2026-09-01')


@respx.mock
def test_command_fails_when_every_client_fails():
    """Systemic: bad credentials or Twilio down. Exits non-zero so cron can notice."""
    make_twilio_client()
    respx.get(usage_url()).mock(return_value=httpx.Response(401))

    with pytest.raises(CommandError):
        call_command('pull_twilio_usage', stdout=StringIO())


@respx.mock
def test_command_only_warns_when_some_clients_fail():
    make_twilio_client('loc1', 'ACclient1')
    make_twilio_client('loc2', 'ACclient2', client_name='Branwynn')
    respx.get(usage_url('ACclient1')).mock(return_value=httpx.Response(500))
    mock_usage(page(record(account_sid='ACclient2')), subaccount_sid='ACclient2')
    out = StringIO()

    call_command('pull_twilio_usage', stdout=out)

    assert '1 of 2 clients failed' in out.getvalue()


@respx.mock
def test_the_daily_task_runs_the_pull():
    make_twilio_client()
    mock_usage(page(record()))

    result = pull_twilio_usage()

    assert result == {'clients': 1, 'days': 1, 'updated': 0, 'failed': 0}
    assert TwilioUsageDay.objects.count() == 1


@respx.mock
def test_the_daily_task_counts_a_failure_instead_of_raising():
    make_twilio_client()
    respx.get(usage_url()).mock(return_value=httpx.Response(500))

    assert pull_twilio_usage()['failed'] == 1


def test_beat_runs_the_pull_once_a_day_offset_from_the_retell_audit():
    entry = settings.CELERY_BEAT_SCHEDULE[SCHEDULE_NAME]
    retell = settings.CELERY_BEAT_SCHEDULE['reconcile-retell-calls']

    assert entry['task'] == pull_twilio_usage.name
    assert entry['schedule'] == crontab(hour=3, minute=30)
    assert entry['schedule'] != retell['schedule']
