"""The admin endpoints behind the embedded GHL page: read-only, plus the one sync action."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
import stripe
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ghl.models import GHLOAuthToken
from apps.ghl.tests.test_services import BASE, mock_installed, mock_location_token
from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency, seal
from apps.rebilling.models import (
    BILLABLE,
    CHARGING,
    FAILED,
    PAID,
    UNBILLABLE,
    UNCHARGED,
    Bill,
    RateHistory,
    RetellCall,
    TwilioUsageDay,
)
from apps.rebilling.tests.test_services import make_client

pytestmark = pytest.mark.django_db

MONTH = '2026-03'
IN_WINDOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
AFTER_WINDOW = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)

RETELL_AGENTS_URL = f'{settings.RETELL_API_BASE_URL}/v2/list-agents'


@pytest.fixture(autouse=True)
def default_retell_agents():
    """`client_detail` now names each mapped agent off Retell, and `make_client()` maps every
    sample client to `agent1`, so every `get_detail()` call in this file would otherwise ask
    Retell for a name it mostly does not care about.

    A router of its own, independent of `respx.mock` - the global singleton the two GHL sync
    tests below activate with their own `@respx.mock`. Left unmocked here (`assert_all_mocked`
    is on by default), it raises for anything that is not this one URL, which is what lets
    `Mocker.handler()` fall through to that other router for GHL's. The handful of tests that
    care what a name resolves to add their own, more specific mock over this one.
    """
    router = respx.mock(assert_all_called=False)
    router.post(RETELL_AGENTS_URL).mock(
        return_value=httpx.Response(200, json={'items': [], 'has_more': False})
    )
    with router:
        yield router


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
    # Still read and still reported, just not billed while Twilio is paused.
    assert row['usage']['call_minutes'] == '100.00'
    # 2 AI minutes at 25c. The 100 call minutes are GoHighLevel's to bill, not ours.
    assert row['usage']['estimated_bill_cents'] == 50


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


@override_settings(EXCLUDED_GHL_LOCATION_IDS=['loc-internal'])
def test_an_excluded_sub_account_is_not_listed(client):
    """`EXCLUDED_GHL_LOCATION_IDS` keeps an internal or non-Retell GHL account off the list."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_location('loc-internal', 'SurPro Labs internal')

    rows = get_clients(client).json()['clients']

    assert [row['location_id'] for row in rows] == ['loc1']


@override_settings(EXCLUDED_GHL_LOCATION_IDS=['loc-internal'])
def test_an_excluded_sub_account_is_hidden_even_when_it_is_already_mapped(client):
    """Excluding an id that was mapped before takes effect on the next page load.

    The mapped half of the list is a separate pass - a mapping whose sub-account the sync has
    not seen is still listed, because it still bills - so the filter has to be applied there
    too, or excluding a mapped client would hide nothing.
    """
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc-internal', client_name='SurPro Labs internal')

    rows = get_clients(client).json()['clients']

    assert [row['location_id'] for row in rows] == ['loc1']


def test_the_agency_s_own_location_is_listed_like_any_client(client):
    """Unless excluded in `.env`, the agency's main location can be mapped and billed too."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_location(settings.AGENCY_LOCATION_ID, 'SurPro Labs AI')

    rows = get_clients(client).json()['clients']

    assert [row['location_id'] for row in rows] == ['loc1', settings.AGENCY_LOCATION_ID]


@override_settings(EXCLUDED_GHL_LOCATION_IDS=[settings.AGENCY_LOCATION_ID])
def test_the_agency_s_own_location_can_still_be_excluded(client):
    """Adding `AGENCY_LOCATION_ID` to `EXCLUDED_GHL_LOCATION_IDS` hides it like any other id."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_location(settings.AGENCY_LOCATION_ID, 'SurPro Labs AI')

    rows = get_clients(client).json()['clients']

    assert [row['location_id'] for row in rows] == ['loc1']


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
    # Newest first, including the default rate auto-seeded when the client was mapped.
    assert [rate['start_date'] for rate in body['rate_history']] == [
        timezone.now().date().isoformat(),
        '2026-03-01',
        '2026-01-01',
    ]
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
        'twilio_billing_enabled': False,
    }


def test_the_rate_in_effect_is_the_one_for_the_month_being_viewed(client):
    """A past month shows the rate that applied then, not whatever is current now."""
    make_agency()
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=20)
    RateHistory.objects.create(client=mapping, start_date=date(2026, 9, 1), voice_rate_cents=99)

    assert get_detail(client, 'loc1', month='2026-03').json()['rates']['voice_rate_cents'] == 20


# --- the bill for the month being viewed -------------------------------------------------
#
# The page shows its own estimate only until the monthly run has produced a bill. After that the
# bill is the answer: it was worked out from the rate in effect on each call's own day, and no
# later rate change can move it.


def test_detail_carries_no_bill_for_a_month_that_has_not_been_billed(client):
    """A month still in progress. The page falls back to its estimate, which is still sent."""
    make_agency()
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1))
    add_call(mapping, 'c1', duration_ms=60_000)

    body = get_detail(client, 'loc1').json()

    assert body['bill'] is None
    assert body['usage']['estimated_bill_cents'] == 25


def test_detail_reports_a_paid_bill_rather_than_leaving_the_month_an_estimate(client):
    make_agency()
    mapping = make_client('loc1')
    Bill.objects.create(
        client=mapping, billing_period=MONTH, total_cents=47, status=PAID, paid_at=AFTER_WINDOW
    )

    assert get_detail(client, 'loc1').json()['bill'] == {
        'status': 'paid',
        'total_cents': 47,
        'failure_reason': '',
        'retry_count': 0,
        'max_retries': 3,
        'next_retry_at': None,
    }


def test_detail_reports_a_client_whose_voice_is_paused(client):
    """What the page shows beside a failed bill: paused, when, and onto what it would resume."""
    make_agency()
    mapping = make_client('loc1')
    mapping.voice_paused_at = IN_WINDOW
    mapping.retell_routing_agent_id = 'agent_captured'
    mapping.save(update_fields=['voice_paused_at', 'retell_routing_agent_id'])

    body = get_detail(client, 'loc1').json()

    assert body['voice'] == {
        'paused': True,
        'paused_at': IN_WINDOW.isoformat(),
        'routing_agent_id': 'agent_captured',
        'error': '',
    }


def test_detail_reports_a_client_who_could_not_be_paused(client):
    """A failed bill beside a client still routing calls is the state worth seeing."""
    make_agency()
    mapping = make_client('loc1')
    mapping.voice_sync_error = 'No custom value named "retell_agent_id" in sub-account loc1.'
    mapping.save(update_fields=['voice_sync_error'])

    body = get_detail(client, 'loc1').json()

    assert body['voice']['paused'] is False
    assert 'retell_agent_id' in body['voice']['error']
    # Nothing was captured, so a resume would write the agent they are mapped to.
    assert body['voice']['routing_agent_id'] == 'agent1'


def test_detail_reports_a_bill_still_waiting_on_stripe(client):
    """`charging` is a real state, not a moment: only the webhook moves a bill off it."""
    make_agency()
    mapping = make_client('loc1')
    Bill.objects.create(
        client=mapping,
        billing_period=MONTH,
        total_cents=650,
        status=CHARGING,
        stripe_invoice_id='in_1',
        charged_at=IN_WINDOW,
    )

    body = get_detail(client, 'loc1').json()

    assert body['bill']['status'] == 'charging'
    assert body['bill']['total_cents'] == 650


def test_detail_reports_why_a_charge_failed(client):
    """Stripe's own code is the difference between "send a new card" and "confirm this one"."""
    make_agency()
    mapping = make_client('loc1')
    Bill.objects.create(
        client=mapping,
        billing_period=MONTH,
        total_cents=650,
        status=FAILED,
        failure_reason='authentication_required',
    )

    assert get_detail(client, 'loc1').json()['bill'] == {
        'status': 'failed',
        'total_cents': 650,
        'failure_reason': 'authentication_required',
        'retry_count': 0,
        'max_retries': 3,
        'next_retry_at': None,
    }


def test_detail_carries_the_reason_a_bill_that_never_reached_stripe_recorded(client, fake):
    """A charge that failed before Stripe saw it leaves the bill `uncharged`, not `failed`.

    Reading `failure_reason` only on `failed` would hide exactly the state nobody can act on
    without being told - no matched customer, no card - so it is carried whatever the status.
    """
    make_agency()
    mapping = make_client('loc1', billing_email='owner@coolbreeze.test')
    Bill.objects.create(
        client=mapping,
        billing_period=MONTH,
        total_cents=650,
        status=UNCHARGED,
        failure_reason='No Stripe customer found for billing email: owner@coolbreeze.test',
    )

    bill = get_detail(client, 'loc1').json()['bill']

    assert bill['status'] == 'uncharged'
    assert 'No Stripe customer found' in bill['failure_reason']


def test_detail_shows_the_bill_for_the_month_being_viewed_and_no_other(client):
    make_agency()
    mapping = make_client('loc1')
    Bill.objects.create(client=mapping, billing_period='2026-02', total_cents=100, status=PAID)
    Bill.objects.create(client=mapping, billing_period=MONTH, total_cents=200, status=UNCHARGED)

    assert get_detail(client, 'loc1', month='2026-02').json()['bill']['total_cents'] == 100
    assert get_detail(client, 'loc1', month=MONTH).json()['bill']['total_cents'] == 200
    assert get_detail(client, 'loc1', month='2026-04').json()['bill'] is None


def test_each_call_carries_the_status_of_the_bill_it_is_on(client):
    """A call made after the month was billed is on no bill, whatever the month's bill says.

    The month's bill cannot answer this for every call in it: the run bills what exists when it
    runs, and a call that arrives afterwards is still waiting for the next one. Calling it paid
    would claim money that was never collected for it.
    """
    make_agency()
    mapping = make_client('loc1')
    bill = Bill.objects.create(
        client=mapping, billing_period=MONTH, total_cents=47, status=PAID, paid_at=AFTER_WINDOW
    )
    billed = [
        add_call(mapping, 'c_28a', started_at=datetime(2026, 3, 28, 9, 0, tzinfo=UTC)),
        add_call(mapping, 'c_28b', started_at=datetime(2026, 3, 28, 15, 30, tzinfo=UTC)),
    ]
    RetellCall.objects.filter(pk__in=[call.pk for call in billed]).update(bill=bill)
    # Made two days after the run charged the month, so no bill has taken it yet.
    add_call(mapping, 'c_30', started_at=datetime(2026, 3, 30, 11, 0, tzinfo=UTC))
    # Nothing can bill this one, so nothing ever will.
    add_call(mapping, 'c_dropped', duration_ms=None, status=UNBILLABLE)

    body = get_detail(client, 'loc1').json()

    assert {call['call_id']: call['bill_status'] for call in body['recent_calls']} == {
        'c_28a': 'paid',
        'c_28b': 'paid',
        'c_30': None,
        'c_dropped': None,
    }
    assert body['bill']['status'] == 'paid'


def test_a_call_follows_its_own_bill_while_the_charge_is_in_the_air(client):
    """The call reports the bill's state as it stands, not only once it is settled."""
    make_agency()
    mapping = make_client('loc1')
    bill = Bill.objects.create(
        client=mapping,
        billing_period=MONTH,
        total_cents=650,
        status=CHARGING,
        stripe_invoice_id='in_1',
        charged_at=AFTER_WINDOW,
    )
    call = add_call(mapping, 'c1')
    RetellCall.objects.filter(pk=call.pk).update(bill=bill)

    body = get_detail(client, 'loc1').json()

    assert body['recent_calls'][0]['bill_status'] == 'charging'


# --- each mapped agent's own name, read off Retell ------------------------------------------


def test_each_mapped_agent_carries_its_own_name(client, default_retell_agents):
    make_agency()
    make_client('loc1', retell_agent_ids=['agent1', 'agent2'])
    default_retell_agents.post(RETELL_AGENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                'items': [
                    {'agent_id': 'agent1', 'agent_name': 'Cool Breeze front desk'},
                    {'agent_id': 'agent2', 'agent_name': 'Cool Breeze after hours'},
                ],
                'has_more': False,
            },
        )
    )

    body = get_detail(client, 'loc1').json()

    # Same ids, same order as `retell_agent_ids` - editing still works in plain ids.
    assert body['retell_agent_ids'] == ['agent1', 'agent2']
    assert body['retell_agents'] == [
        {'agent_id': 'agent1', 'agent_name': 'Cool Breeze front desk'},
        {'agent_id': 'agent2', 'agent_name': 'Cool Breeze after hours'},
    ]


def test_an_agent_retell_no_longer_has_falls_back_to_no_name(client, default_retell_agents):
    """An agent removed from Retell since it was mapped is still a real mapping - it has simply
    lost the name that went with it, which the page falls back to the bare id for."""
    make_agency()
    make_client('loc1', retell_agent_ids=['agent_gone'])

    body = get_detail(client, 'loc1').json()

    assert body['retell_agents'] == [{'agent_id': 'agent_gone', 'agent_name': ''}]


def test_no_agents_mapped_means_no_name_lookup_at_all(client, default_retell_agents):
    """Nothing to name, so nothing asked - proven by the call count, not only by the result,
    since an empty catalog would answer `[]` either way."""
    make_agency()
    make_client('loc1', retell_agent_ids=[])

    assert get_detail(client, 'loc1').json()['retell_agents'] == []
    assert default_retell_agents.calls.call_count == 0


def test_a_failed_retell_lookup_leaves_the_mapping_real_but_unnamed(client, default_retell_agents):
    """Retell being unreachable must not take the whole page down with it."""
    make_agency()
    make_client('loc1')
    default_retell_agents.post(RETELL_AGENTS_URL).mock(return_value=httpx.Response(500))

    body = get_detail(client, 'loc1').json()

    assert body.get('retell_agent_ids') == ['agent1']
    assert body['retell_agents'] == [{'agent_id': 'agent1', 'agent_name': ''}]


# --- the Stripe customer's own name, for the billing contact card ---------------------------


def test_detail_reports_the_stripe_customer_by_name(client, monkeypatch, default_retell_agents):
    make_agency()
    make_client('loc1', stripe_customer_id='cus_known')
    monkeypatch.setattr(
        stripe.Customer, 'retrieve', lambda *a, **k: SimpleNamespace(name='Cool Breeze HVAC LLC')
    )
    monkeypatch.setattr(stripe.PaymentMethod, 'list', lambda **k: SimpleNamespace(data=[]))

    body = get_detail(client, 'loc1').json()

    assert body['stripe_customer_id'] == 'cus_known'
    assert body['stripe_customer_name'] == 'Cool Breeze HVAC LLC'


def test_a_stripe_customer_with_no_name_on_file_reports_none(
    client, monkeypatch, default_retell_agents
):
    make_agency()
    make_client('loc1', stripe_customer_id='cus_known')
    monkeypatch.setattr(stripe.Customer, 'retrieve', lambda *a, **k: SimpleNamespace(name=None))
    monkeypatch.setattr(stripe.PaymentMethod, 'list', lambda **k: SimpleNamespace(data=[]))

    assert get_detail(client, 'loc1').json()['stripe_customer_name'] == ''


def test_no_stripe_customer_means_no_name_lookup_at_all(client, monkeypatch, default_retell_agents):
    """Nothing matched yet, so nothing to ask Stripe for - proven by never calling it, not only
    by the empty result, since an unmocked call failing closed would answer the same way."""
    make_agency()
    make_client('loc1')

    def unexpected(*args, **kwargs):
        raise AssertionError('should never ask Stripe for a customer that was never matched')

    monkeypatch.setattr(stripe.Customer, 'retrieve', unexpected)

    body = get_detail(client, 'loc1').json()

    assert body['stripe_customer_id'] == ''
    assert body['stripe_customer_name'] == ''


def test_an_unreachable_stripe_leaves_the_page_up_with_no_name(
    client, monkeypatch, default_retell_agents
):
    """A name is not what the charge depends on, so Stripe being down must not take the rest of
    the client's own page down with it."""
    make_agency()
    make_client('loc1', stripe_customer_id='cus_known')

    def unreachable(*args, **kwargs):
        raise stripe.APIConnectionError('Stripe is unreachable.')

    monkeypatch.setattr(stripe.Customer, 'retrieve', unreachable)

    body = get_detail(client, 'loc1').json()

    assert body['stripe_customer_id'] == 'cus_known'
    assert body['stripe_customer_name'] == ''


def test_detail_carries_the_defaults_the_reset_action_writes_back(client):
    """The page must not hold its own copy of two numbers the model already owns."""
    make_agency()
    make_client('loc1')

    assert get_detail(client, 'loc1').json()['default_rates'] == {
        'voice_rate_cents': 25,
        'call_rate_cents': 5,
    }


# --- the "Refresh clients" sync action ---------------------------------------------------


def test_sync_401s_without_a_session(client):
    make_agency()

    response = client.post(reverse('rebilling:admin-clients-sync'))

    assert response.status_code == 401


def test_sync_is_refused_for_a_sub_account_session(client):
    make_agency()

    assert sync_clients(client, blob=VECTOR_LOCATION).status_code == 403


@respx.mock
@override_settings(EXCLUDED_GHL_LOCATION_IDS=[settings.AGENCY_LOCATION_ID])
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
def test_a_refresh_puts_the_main_account_in_the_clients_list(client):
    """The agency's own account bills like any other client, so "Refresh clients" must surface
    it - even though `/oauth/installed-locations` does not report it."""
    make_agency()
    respx.get(f'{BASE}/locations/{settings.AGENCY_LOCATION_ID}').mock(
        return_value=httpx.Response(200, json={'location': {'name': 'SurPro Labs AI'}})
    )
    mock_installed(('loc-new', 'Polar Peak Heating'))
    mock_location_token()

    body = sync_clients(client).json()

    rows = {row['location_id']: row for row in body['clients']}
    assert set(rows) == {'loc-new', settings.AGENCY_LOCATION_ID}
    # Listed but not yet set up for billing, exactly like any other newly discovered client.
    assert rows[settings.AGENCY_LOCATION_ID]['client_name'] == 'SurPro Labs AI'
    assert rows[settings.AGENCY_LOCATION_ID]['mapped'] is False


@respx.mock
@override_settings(EXCLUDED_GHL_LOCATION_IDS=['loc-internal', settings.AGENCY_LOCATION_ID])
def test_a_refresh_does_not_bring_an_excluded_sub_account_back(client):
    """The point of the setting: pressing \"Refresh clients\" must not undo the exclusion."""
    make_agency()
    mock_installed(('loc-new', 'Polar Peak Heating'), ('loc-internal', 'SurPro Labs internal'))
    mock_location_token()

    body = sync_clients(client).json()

    assert [row['location_id'] for row in body['clients']] == ['loc-new']
    # Not stored at all, so it is not waiting to reappear if the setting is ever cleared by
    # accident - it comes back from GoHighLevel on the next sync, which is where it lives.
    assert not GHLOAuthToken.objects.filter(location_id='loc-internal').exists()


@respx.mock
def test_sync_returns_502_on_ghl_failure(client):
    make_agency()
    respx.get(f'{BASE}/oauth/installed-locations').mock(return_value=httpx.Response(500))

    assert sync_clients(client).status_code == 502


# --- Twilio paused -------------------------------------------------------------------------


def test_the_list_tells_the_page_twilio_billing_is_paused(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1')

    assert get_clients(client).json()['twilio_billing_enabled'] is False


def test_the_detail_tells_the_page_twilio_billing_is_paused(client):
    make_agency()
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1))

    assert get_detail(client, 'loc1').json()['twilio_billing_enabled'] is False


@override_settings(TWILIO_BILLING_ENABLED=True)
def test_turning_twilio_billing_back_on_restores_the_estimate_and_the_flag(client):
    """The pause is a switch, not a deletion: the figures come straight back."""
    make_agency()
    mapping = make_client('loc1', twilio_subaccount_sid='ACchild')
    RateHistory.objects.create(
        client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=25, call_rate_cents=5
    )
    add_call(mapping, 'call1', duration_ms=120_000)
    add_twilio_day(mapping, date(2026, 3, 10), minutes='100.00')

    body = get_detail(client, 'loc1').json()

    assert body['twilio_billing_enabled'] is True
    # 2 AI minutes at 25c, plus 100 call minutes at 5c.
    assert body['usage']['estimated_bill_cents'] == 550
