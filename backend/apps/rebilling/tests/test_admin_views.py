"""The admin endpoints behind the embedded GHL page: read-only, plus the one sync action."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ghl.models import GHLOAuthToken
from apps.ghl.tests.test_services import BASE, mock_installed, mock_location_token
from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency, seal
from apps.rebilling.models import (
    BILLABLE,
    UNBILLABLE,
    RateHistory,
    RetellCall,
    TwilioUsageDay,
)
from apps.rebilling.tests.test_services import make_client

pytestmark = pytest.mark.django_db

MONTH = '2026-03'
IN_WINDOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
AFTER_WINDOW = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)


def make_location(location_id, name):
    """A sub-account as Task 1's sync leaves it."""
    return GHLOAuthToken.objects.create(
        company_id='comp_demo',
        location_id=location_id,
        name=name,
        access_token='token',
        expires_at=timezone.now() + timedelta(days=1),
    )


def add_call(client, call_id, duration_ms=120_000, started_at=IN_WINDOW, status=BILLABLE, cost=10):
    return RetellCall.objects.create(
        call_id=call_id,
        client=client,
        started_at=started_at,
        duration_ms=duration_ms,
        retell_cost_cents=cost,
        call_status='ended',
        billing_status=status,
    )


def add_twilio_day(client, day, minutes='30.00', cents=120, as_of=None):
    return TwilioUsageDay.objects.create(
        client=client,
        subaccount_sid='ACchild',
        usage_date=day,
        call_count=4,
        usage_minutes=Decimal(minutes),
        twilio_price_cents=cents,
        as_of=as_of or datetime(2026, 3, 31, 3, 30, tzinfo=UTC),
    )


def get_clients(client, blob=VECTOR_AGENCY, month=MONTH, **params):
    query = {'month': month, **params} if month else params
    return client.get(reverse('rebilling:admin-clients'), query, HTTP_X_GHL_SESSION=blob)


def get_detail(client, location_id, blob=VECTOR_AGENCY, month=MONTH):
    return client.get(
        reverse('rebilling:admin-client-detail', args=[location_id]),
        {'month': month},
        HTTP_X_GHL_SESSION=blob,
    )


def sync_clients(client, blob=VECTOR_AGENCY, month=MONTH):
    url = reverse('rebilling:admin-clients-sync')
    # `.post()`'s second argument is the request body, not the query string - the month has to
    # go on the URL itself, the same place the real frontend puts it.
    if month:
        url = f'{url}?month={month}'
    return client.post(url, HTTP_X_GHL_SESSION=blob)


# --- authentication ---------------------------------------------------------------------


def test_clients_401s_without_a_session(client):
    make_agency()

    assert client.get(reverse('rebilling:admin-clients')).status_code == 401


def test_clients_403s_for_another_agency(client):
    make_agency(company_id='comp_jerry')

    assert get_clients(client).status_code == 403


# --- the client list --------------------------------------------------------------------


def test_lists_a_mapped_client_with_rates_and_usage(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1))
    add_call(mapping, 'c1', duration_ms=120_000)
    add_twilio_day(mapping, date(2026, 3, 10), minutes='100.00', cents=420)

    row = get_clients(client).json()['clients'][0]

    assert row['mapped'] is True
    assert row['retell_connected'] is True
    assert row['twilio_connected'] is False
    assert row['rates'] == {
        'voice_rate_cents': 25,
        'call_rate_cents': 5,
        'start_date': '2026-01-01',
    }
    assert row['usage']['ai_minutes'] == '2.00'
    assert row['usage']['call_minutes'] == '100.00'
    # 2 AI minutes at 25c, plus 100 call minutes at 5c.
    assert row['usage']['estimated_bill_cents'] == 550


def test_an_onboarded_but_unmapped_client_is_listed_as_unmapped(client):
    """Nobody has set this client up. That must not read as a client who used nothing."""
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')

    row = get_clients(client).json()['clients'][0]

    assert row['client_name'] == 'Polar Peak Heating'
    assert row['mapped'] is False
    assert row['retell_connected'] is False
    assert row['usage'] is None
    assert row['rates'] is None


def test_a_mapping_the_sync_has_not_seen_is_still_listed(client):
    """It is being billed, so it has to be visible even without a GHL row."""
    make_agency()
    make_client('loc-orphan', client_name='Orphan Co')

    names = [row['client_name'] for row in get_clients(client).json()['clients']]

    assert names == ['Orphan Co']


def test_a_client_with_no_rate_row_has_no_estimate(client):
    """An unknown rate must not render as a free client."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    add_call(mapping, 'c1')

    usage = get_clients(client).json()['clients'][0]['usage']

    assert usage['ai_minutes'] == '2.00'
    assert usage['estimated_bill_cents'] is None


def test_twilio_only_and_retell_only_clients_read_as_exactly_that(client):
    make_agency()
    make_location('loc1', 'Retell Only')
    make_location('loc2', 'Twilio Only')
    retell_only = make_client('loc1', client_name='Retell Only')
    twilio_only = make_client('loc2', client_name='Twilio Only', twilio_subaccount_sid='ACchild')
    add_call(retell_only, 'c1')
    add_twilio_day(twilio_only, date(2026, 3, 10))

    rows = {row['client_name']: row for row in get_clients(client).json()['clients']}

    assert rows['Retell Only']['usage']['has_twilio_usage'] is False
    assert rows['Twilio Only']['usage']['ai_minutes'] == '0.00'
    assert rows['Twilio Only']['usage']['has_twilio_usage'] is True


# --- what counts ------------------------------------------------------------------------


def test_unbillable_calls_are_counted_but_not_billed(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1))
    add_call(mapping, 'ok', duration_ms=60_000)
    add_call(mapping, 'bad', duration_ms=60_000, status=UNBILLABLE)

    usage = get_clients(client).json()['clients'][0]['usage']

    assert usage['ai_minutes'] == '1.00'
    assert usage['unbillable_calls'] == 1


def test_only_calls_inside_the_month_count(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    add_call(mapping, 'inside', duration_ms=60_000)
    add_call(mapping, 'outside', duration_ms=60_000, started_at=AFTER_WINDOW)

    assert get_clients(client).json()['clients'][0]['usage']['ai_minutes'] == '1.00'


def test_only_twilio_days_inside_the_month_count(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    add_twilio_day(mapping, date(2026, 3, 31), minutes='10.00')
    add_twilio_day(mapping, date(2026, 4, 1), minutes='99.00')

    assert get_clients(client).json()['clients'][0]['usage']['call_minutes'] == '10.00'


@override_settings(RETELL_ROUNDING='round_up_minute')
def test_retell_rounding_setting_is_honoured(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    add_call(mapping, 'c1', duration_ms=61_000)

    assert get_clients(client).json()['clients'][0]['usage']['ai_minutes'] == '2.00'


@override_settings(RETELL_ROUNDING='round_up_minute')
def test_twilio_minutes_are_never_rounded_again(client):
    """Twilio rounds each call before summing the day. Rounding on top would overbill."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    add_twilio_day(mapping, date(2026, 3, 10), minutes='10.50')

    assert get_clients(client).json()['clients'][0]['usage']['call_minutes'] == '10.50'


def test_the_newest_twilio_stamp_is_reported_and_flagged_when_stale(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    add_twilio_day(mapping, date(2026, 3, 10), as_of=datetime(2026, 3, 10, 3, 30, tzinfo=UTC))

    usage = get_clients(client).json()['clients'][0]['usage']

    assert usage['twilio_as_of'].startswith('2026-03-10')
    assert usage['twilio_stale'] is True


# --- the month parameter ----------------------------------------------------------------


def test_defaults_to_the_current_month(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1')

    body = get_clients(client, month=None).json()

    assert body['month'] == timezone.now().strftime('%Y-%m')


def test_a_mistyped_month_is_rejected_rather_than_quietly_changed(client):
    make_agency()

    assert get_clients(client, month='March').status_code == 400
    assert get_clients(client, month='2026-13').status_code == 400


# --- sub-account scope ------------------------------------------------------------------


def test_a_sub_account_session_sees_only_itself(client):
    make_agency()
    make_location('loc-cool-breeze', 'Cool Breeze HVAC')
    make_location('loc-other', 'Someone Else')
    make_client('loc-cool-breeze', client_name='Cool Breeze HVAC')
    make_client('loc-other', client_name='Someone Else')

    body = get_clients(client, blob=VECTOR_LOCATION).json()

    assert body['scope'] == 'location'
    assert [row['location_id'] for row in body['clients']] == ['loc-cool-breeze']


def test_a_sub_account_session_cannot_read_another_client(client):
    make_agency()
    make_client('loc-other', client_name='Someone Else')

    assert get_detail(client, 'loc-other', blob=VECTOR_LOCATION).status_code == 403


def test_agency_viewer_sees_every_client_even_when_active_location_is_the_agencys_own(client):
    """GHL can report the agency's own id as `activeLocation`. That must not narrow the list to
    one location - it must still mean every client, the same as no activeLocation at all."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_location('loc2', 'Someone Else')
    blob = seal({'companyId': 'comp_demo', 'activeLocation': 'agency-loc'})

    body = get_clients(client, blob=blob).json()

    assert body['scope'] == 'agency'
    assert {row['location_id'] for row in body['clients']} == {'loc1', 'loc2'}


# --- the detail view --------------------------------------------------------------------


def test_detail_returns_the_mapping_rates_and_usage(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1', twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=20)
    RateHistory.objects.create(client=mapping, start_date=date(2026, 3, 1), voice_rate_cents=30)
    add_call(mapping, 'c1', duration_ms=60_000)
    add_twilio_day(mapping, date(2026, 3, 10))

    body = get_detail(client, 'loc1').json()

    assert body['mapped'] is True
    assert body['retell_agent_ids'] == ['agent1']
    assert body['twilio_subaccount_sid'] == 'ACchild'
    assert body['rates']['voice_rate_cents'] == 30
    assert [rate['start_date'] for rate in body['rate_history']] == ['2026-03-01', '2026-01-01']
    assert len(body['twilio_days']) == 1
    assert [call['call_id'] for call in body['recent_calls']] == ['c1']


def test_detail_shows_why_a_call_cannot_be_billed(client):
    make_agency()
    mapping = make_client('loc1')
    RetellCall.objects.create(
        call_id='c1',
        client=mapping,
        started_at=IN_WINDOW,
        call_status='error',
        billing_status=UNBILLABLE,
        unbillable_reason='Call status is error, not ended.',
    )

    call = get_detail(client, 'loc1').json()['recent_calls'][0]

    assert call['billing_status'] == 'unbillable'
    assert call['unbillable_reason'] == 'Call status is error, not ended.'


def test_detail_404s_for_a_client_that_was_never_mapped(client):
    """An agency admin reading a client the list already marks unmapped. There is nothing to read
    yet, and the list is where that status is shown - this stays a 404."""
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')

    assert get_detail(client, 'loc-new').status_code == 404


def test_a_sub_account_with_no_mapping_gets_a_friendly_state_not_a_404(client):
    """The client's own page, opened before an agency admin has set it up. A 404 would look
    broken; this is a normal, expected state and must read as one."""
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')
    blob = seal({'companyId': 'comp_demo', 'activeLocation': 'loc-new'})

    response = get_detail(client, 'loc-new', blob=blob)

    assert response.status_code == 200
    assert response.json() == {
        'month': MONTH,
        'location_id': 'loc-new',
        'client_name': 'Polar Peak Heating',
        'mapped': False,
    }


def test_the_rate_in_effect_is_the_one_for_the_month_being_viewed(client):
    """A past month shows the rate that applied then, not whatever is current now."""
    make_agency()
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=20)
    RateHistory.objects.create(client=mapping, start_date=date(2026, 9, 1), voice_rate_cents=99)

    assert get_detail(client, 'loc1', month='2026-03').json()['rates']['voice_rate_cents'] == 20


# --- the "Refresh clients" sync action ---------------------------------------------------


def test_sync_401s_without_a_session(client):
    make_agency()

    response = client.post(reverse('rebilling:admin-clients-sync'))

    assert response.status_code == 401


def test_sync_is_refused_for_a_sub_account_session(client):
    make_agency()

    assert sync_clients(client, blob=VECTOR_LOCATION).status_code == 403


@respx.mock
def test_sync_discovers_a_new_sub_account_and_returns_the_updated_list(client):
    make_agency()
    mock_installed(('loc-new', 'Polar Peak Heating'))
    mock_location_token()

    response = sync_clients(client)

    assert response.status_code == 200
    body = response.json()
    assert body['month'] == MONTH
    assert [row['location_id'] for row in body['clients']] == ['loc-new']
    location = GHLOAuthToken.objects.get(location_id='loc-new')
    assert location.is_active and location.name == 'Polar Peak Heating'


@respx.mock
def test_sync_returns_502_on_ghl_failure(client):
    make_agency()
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))

    assert sync_clients(client).status_code == 502
