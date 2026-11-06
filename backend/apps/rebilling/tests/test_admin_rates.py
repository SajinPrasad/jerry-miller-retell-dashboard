"""PUT /api/rebilling/admin/clients/<location_id>/rates/: an agency admin sets what a client pays.

A rate change adds a `RateHistory` row from today rather than editing one, so these tests are
mostly about what a save does *not* touch: a month already billed, and a period already past.
"""

from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency
from apps.rebilling.models import (
    DEFAULT_CALL_RATE_CENTS,
    DEFAULT_VOICE_RATE_CENTS,
    RateHistory,
)
from apps.rebilling.services import backfill_default_rates, current_rate
from apps.rebilling.tests.test_admin_views import make_location
from apps.rebilling.tests.test_services import make_client

pytestmark = pytest.mark.django_db


def put_rates(client, location_id, data, blob=VECTOR_AGENCY, month=None):
    return client.put(
        reverse('rebilling:admin-client-rates', args=[location_id]),
        data=data,
        content_type='application/json',
        HTTP_X_GHL_SESSION=blob,
        QUERY_STRING=f'month={month}' if month else '',
    )


# --- saving ------------------------------------------------------------------------------


def test_saving_rates_stores_them_from_today(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')

    response = put_rates(client, 'loc1', {'voice_rate_cents': 30, 'call_rate_cents': 7})

    assert response.status_code == 200
    rate = RateHistory.objects.get(client=mapping)
    assert (rate.voice_rate_cents, rate.call_rate_cents) == (30, 7)
    assert rate.start_date == timezone.now().date()


def test_the_response_is_the_refreshed_client_detail_at_the_new_rates(client):
    """The page replaces what it shows with the server's own figures, never with its own guess."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    make_client('loc1')

    body = put_rates(client, 'loc1', {'voice_rate_cents': 30, 'call_rate_cents': 7}).json()

    assert body['location_id'] == 'loc1'
    assert body['rates'] == {
        'voice_rate_cents': 30,
        'call_rate_cents': 7,
        'start_date': timezone.now().date().isoformat(),
    }
    # Everything the detail view answers, so the page needs no second request to catch up.
    assert body['usage']['estimated_bill_cents'] is not None
    assert body['bill'] is None


def test_saving_again_the_same_day_corrects_todays_rate_instead_of_failing(client):
    """`(client, start_date)` is unique. Correcting a rate minutes later is one decision, not
    two, and the monthly billing run cannot have billed anything in between."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')

    put_rates(client, 'loc1', {'voice_rate_cents': 30, 'call_rate_cents': 7})
    response = put_rates(client, 'loc1', {'voice_rate_cents': 35, 'call_rate_cents': 8})

    assert response.status_code == 200
    assert mapping.rates.count() == 1
    rate = mapping.rates.get()
    assert (rate.voice_rate_cents, rate.call_rate_cents) == (35, 8)


def test_an_earlier_rate_is_left_in_place_for_the_periods_it_covered(client):
    """The point of a rate *history*: usage already recorded still bills at the rate that was in
    effect on its own day."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    RateHistory.objects.create(
        client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=20, call_rate_cents=4
    )

    put_rates(client, 'loc1', {'voice_rate_cents': 30, 'call_rate_cents': 7})

    assert mapping.rates.count() == 2
    old = mapping.rates.get(start_date=date(2026, 1, 1))
    assert (old.voice_rate_cents, old.call_rate_cents) == (20, 4)
    # And it is still the rate that applies to a month that ended before the change.
    assert current_rate(mapping, date(2026, 3, 31)).voice_rate_cents == 20


def test_a_month_that_has_already_ended_still_reads_at_its_own_rate_after_a_save(client):
    """Saving while a past month is on screen must not restate what that month costs."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=20)

    body = put_rates(
        client, 'loc1', {'voice_rate_cents': 30, 'call_rate_cents': 7}, month='2026-03'
    ).json()

    assert body['month'] == '2026-03'
    assert body['rates']['voice_rate_cents'] == 20
    # The new rate is still visible as what the client is on going forward.
    assert body['rate_history'][0]['voice_rate_cents'] == 30


def test_the_rate_in_effect_today_is_the_one_just_saved(client):
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    yesterday = timezone.now().date() - timedelta(days=1)
    RateHistory.objects.create(client=mapping, start_date=yesterday, voice_rate_cents=20)

    put_rates(client, 'loc1', {'voice_rate_cents': 30, 'call_rate_cents': 7})

    assert current_rate(mapping, timezone.now().date()).voice_rate_cents == 30


# --- resetting to the defaults -----------------------------------------------------------


def test_resetting_writes_the_system_defaults_as_the_clients_active_rates(client):
    """Reset is the same save, carrying the defaults the detail view hands the page."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')
    RateHistory.objects.create(client=mapping, start_date=date(2026, 1, 1), voice_rate_cents=99)

    body = put_rates(
        client,
        'loc1',
        {
            'voice_rate_cents': DEFAULT_VOICE_RATE_CENTS,
            'call_rate_cents': DEFAULT_CALL_RATE_CENTS,
        },
    ).json()

    assert body['rates']['voice_rate_cents'] == DEFAULT_VOICE_RATE_CENTS
    assert body['rates']['call_rate_cents'] == DEFAULT_CALL_RATE_CENTS
    assert current_rate(mapping, timezone.now().date()).voice_rate_cents == 25


# --- falling back to defaults when nothing has ever been set ------------------------------


def test_current_rate_falls_back_to_defaults_with_no_rate_history_at_all():
    """Legacy data from before auto-seeding, or a row removed by hand - either way, a mapped
    client must never be read as having no rate."""
    mapping = make_client('loc1')
    mapping.rates.all().delete()

    rate = current_rate(mapping, timezone.now().date())

    assert (rate.voice_rate_cents, rate.call_rate_cents) == (
        DEFAULT_VOICE_RATE_CENTS,
        DEFAULT_CALL_RATE_CENTS,
    )


def test_current_rate_still_returns_none_for_a_day_before_any_rate_started():
    """A client who does have rate history, just not one covering this particular day, is a real
    gap and must not be masked by the same fallback."""
    mapping = make_client('loc1')
    mapping.rates.all().delete()
    RateHistory.objects.create(client=mapping, start_date=date(2026, 6, 1), voice_rate_cents=40)

    assert current_rate(mapping, date(2026, 1, 1)) is None


# --- backfilling clients mapped before auto-seeding existed --------------------------------


def test_backfill_seeds_only_active_clients_with_no_rate_history():
    legacy = make_client('loc1')
    legacy.rates.all().delete()
    already_rated = make_client('loc2', client_name='Branwynn Destinations')
    RateHistory.objects.create(
        client=already_rated, start_date=date(2026, 1, 1), voice_rate_cents=99
    )
    inactive = make_client('loc3', client_name='Closed Co', active=False)
    inactive.rates.all().delete()

    result = backfill_default_rates()

    assert result == {'seeded': 1}
    assert legacy.rates.get().voice_rate_cents == DEFAULT_VOICE_RATE_CENTS
    # Untouched: already had a rate, even though it is not the default.
    assert already_rated.rates.count() == 2
    assert already_rated.rates.get(start_date=date(2026, 1, 1)).voice_rate_cents == 99
    # Left alone: inactive clients are not billed, so nothing is seeded for them.
    assert not inactive.rates.exists()


def test_backfill_command_reports_what_it_did():
    make_client('loc1').rates.all().delete()

    out = StringIO()
    call_command('backfill_default_rates', stdout=out)

    assert 'Backfill: 1 client(s) given the default rates.' in out.getvalue()


# --- who may save ------------------------------------------------------------------------


def test_401_without_a_session(client):
    make_agency()
    make_client('loc1')

    response = client.put(
        reverse('rebilling:admin-client-rates', args=['loc1']),
        data={'voice_rate_cents': 30, 'call_rate_cents': 7},
        content_type='application/json',
    )

    assert response.status_code == 401


def test_a_sub_account_session_cannot_change_what_it_is_charged(client):
    """Read-only for a client on its own page, even though the page is the same one."""
    make_agency()
    make_location('loc1', 'Cool Breeze HVAC')
    mapping = make_client('loc1')

    response = put_rates(
        client, 'loc1', {'voice_rate_cents': 1, 'call_rate_cents': 1}, blob=VECTOR_LOCATION
    )

    assert response.status_code == 403
    # Still only the rate auto-seeded when the client was mapped - the rejected request added
    # nothing and changed nothing.
    assert mapping.rates.count() == 1
    assert mapping.rates.get().voice_rate_cents == DEFAULT_VOICE_RATE_CENTS


def test_404_for_a_client_nobody_has_mapped_yet(client):
    """A rate hangs off a client mapping; there is nothing to hang one on yet."""
    make_agency()
    make_location('loc-new', 'Polar Peak Heating')

    response = put_rates(client, 'loc-new', {'voice_rate_cents': 30, 'call_rate_cents': 7})

    assert response.status_code == 404


# --- what is accepted --------------------------------------------------------------------


def test_both_rates_are_required(client):
    """They share one row, so a half request would have to invent the other half."""
    make_agency()
    make_client('loc1')

    response = put_rates(client, 'loc1', {'voice_rate_cents': 30})

    assert response.status_code == 400
    assert 'call_rate_cents' in response.json()


def test_a_negative_rate_is_refused(client):
    make_agency()
    mapping = make_client('loc1')

    response = put_rates(client, 'loc1', {'voice_rate_cents': -1, 'call_rate_cents': 7})

    assert response.status_code == 400
    # Still only the rate auto-seeded when the client was mapped - the rejected request added
    # nothing and changed nothing.
    assert mapping.rates.count() == 1
    assert mapping.rates.get().voice_rate_cents == DEFAULT_VOICE_RATE_CENTS


def test_a_rate_of_zero_is_allowed(client):
    """Charging a client nothing per minute is a real arrangement, not a mistake."""
    make_agency()
    make_client('loc1')

    body = put_rates(client, 'loc1', {'voice_rate_cents': 0, 'call_rate_cents': 0}).json()

    assert body['rates']['voice_rate_cents'] == 0
