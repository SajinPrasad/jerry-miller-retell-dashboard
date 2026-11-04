"""The nightly audit, one client's Retell workspace at a time.

Catches calls whose webhook never arrived, fills in duration and cost the webhook left out,
and - because reading a workspace is the same request as proving its key still works - records
whether each key is still good.
"""

import json
from datetime import timedelta
from io import StringIO

import httpx
import pytest
import respx
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.rebilling.exceptions import RetellError
from apps.rebilling.models import (
    BILLABLE,
    UNBILLABLE,
    WORKSPACE_ACTIVE,
    WORKSPACE_KEY_INVALID,
    RetellCall,
    billing_state,
)
from apps.rebilling.services import RetellReconciliationService, reconcile_all

from .test_services import WORKSPACE_KEY, call_payload, make_client, make_workspace

pytestmark = pytest.mark.django_db

BASE = settings.RETELL_API_BASE_URL
LIST_URL = f'{BASE}/v3/list-calls'


@pytest.fixture
def workspace():
    """One client, with their own workspace and their own key. The whole scope of a run."""
    return make_workspace()


@pytest.fixture
def service(workspace):
    return RetellReconciliationService(workspace.api_key)


def mock_list(*pages):
    """Mock /v3/list-calls, one response per page given."""
    responses = [httpx.Response(200, json=page) for page in pages]
    return respx.post(LIST_URL).mock(side_effect=responses)


def page(*calls, pagination_key=None):
    return {
        'items': list(calls),
        'has_more': pagination_key is not None,
        **({'pagination_key': pagination_key} if pagination_key else {}),
    }


def mock_get_call(call_id, **overrides):
    return respx.get(f'{BASE}/v2/get-call/{call_id}').mock(
        return_value=httpx.Response(200, json=call_payload(call_id=call_id, **overrides))
    )


def recent_call(**overrides):
    """A call inside the window, which is all /v3/list-calls can return us."""
    now_ms = int(timezone.now().timestamp() * 1000)
    return call_payload(start_timestamp=now_ms - 60_000, end_timestamp=now_ms, **overrides)


def make_stored_call(client, call_id='call1', minutes_ago=60, **overrides):
    """A call already in the table, marked the way `store_workspace_call` would have marked it."""
    fields = {
        'call_id': call_id,
        'client': client,
        'call_status': 'ended',
        'started_at': timezone.now() - timedelta(minutes=minutes_ago),
        'duration_ms': None,
        'retell_cost_cents': None,
    }
    fields.update(overrides)
    status, reason = billing_state(
        fields['duration_ms'], fields['started_at'], fields['call_status']
    )
    return RetellCall.objects.create(billing_status=status, unbillable_reason=reason, **fields)


# --- listing ----------------------------------------------------------------------------------


@respx.mock
def test_stores_a_call_the_webhook_missed(service, workspace):
    mock_list(page(recent_call()))

    result = service.reconcile_workspace(workspace)

    assert result['seen'] == 1 and result['stored'] == 1
    stored = RetellCall.objects.get(call_id='call1')
    assert stored.client == workspace.client
    assert stored.duration_ms == 15791


@respx.mock
def test_sends_the_window_and_this_workspace_s_own_key(service, workspace):
    route = mock_list(page())
    before = timezone.now()

    service.reconcile_workspace(workspace)

    request = route.calls.last.request
    assert request.headers['Authorization'] == f'Bearer {WORKSPACE_KEY}'
    sent = json.loads(request.content)
    window = sent['filter_criteria']['start_timestamp']
    assert window['type'] == 'range' and window['op'] == 'bt'
    lower, upper = window['value']
    assert lower == pytest.approx((before - timedelta(hours=48)).timestamp() * 1000, abs=5000)
    assert upper == pytest.approx(before.timestamp() * 1000, abs=5000)
    assert sent['limit'] == 1000


@respx.mock
def test_a_call_we_already_have_is_not_stored_twice(service, workspace):
    mock_list(page(call_payload()))
    service.reconcile_workspace(workspace)
    respx.reset()
    mock_list(page(call_payload()))

    result = service.reconcile_workspace(workspace)

    assert result['seen'] == 1 and result['stored'] == 0
    assert RetellCall.objects.count() == 1


@respx.mock
def test_pages_through_with_the_pagination_key(service, workspace):
    route = mock_list(
        page(call_payload(call_id='call1'), pagination_key='key-2'),
        page(call_payload(call_id='call2'), pagination_key='key-3'),
        page(call_payload(call_id='call3')),
    )

    result = service.reconcile_workspace(workspace)

    assert result['seen'] == 3 and result['stored'] == 3
    assert route.call_count == 3
    assert 'pagination_key' not in json.loads(route.calls[0].request.content)
    assert json.loads(route.calls[1].request.content)['pagination_key'] == 'key-2'
    assert json.loads(route.calls[2].request.content)['pagination_key'] == 'key-3'
    assert set(RetellCall.objects.values_list('call_id', flat=True)) == {
        'call1',
        'call2',
        'call3',
    }


@respx.mock
def test_stops_when_has_more_is_true_but_no_key_comes_back(service, workspace):
    """Guards the pager against looping forever on a malformed page."""
    route = mock_list({'items': [call_payload()], 'has_more': True})

    result = service.reconcile_workspace(workspace)

    assert route.call_count == 1 and result['seen'] == 1


# --- the high-water mark -------------------------------------------------------------------------


@respx.mock
def test_remembers_the_newest_call_it_listed(service, workspace):
    newest = int(timezone.now().timestamp() * 1000)
    mock_list(
        page(
            call_payload(call_id='older', start_timestamp=newest - 600_000),
            call_payload(call_id='newest', start_timestamp=newest),
        )
    )

    service.reconcile_workspace(workspace)

    workspace.refresh_from_db()
    assert workspace.last_call_timestamp_ms == newest


@respx.mock
def test_the_next_run_resumes_from_the_mark_rather_than_re_listing_the_window(service, workspace):
    """Two days per client per night is the same data fetched N times over."""
    mark = int((timezone.now() - timedelta(hours=5)).timestamp() * 1000)
    workspace.last_call_timestamp_ms = mark
    workspace.save(update_fields=['last_call_timestamp_ms'])
    route = mock_list(page())

    service.reconcile_workspace(workspace)

    lower, _ = json.loads(route.calls.last.request.content)['filter_criteria']['start_timestamp'][
        'value'
    ]
    # An hour before the mark, deliberately: a call Retell recorded late would otherwise be
    # stepped straight over, and re-reading one is free.
    assert lower == pytest.approx(mark - 3_600_000, abs=5000)


@respx.mock
def test_a_mark_older_than_the_window_is_not_clamped_back_to_it(service, workspace):
    """A quiet client must not have the week since their last call skipped."""
    mark = int((timezone.now() - timedelta(days=7)).timestamp() * 1000)
    workspace.last_call_timestamp_ms = mark
    workspace.save(update_fields=['last_call_timestamp_ms'])
    route = mock_list(page())

    service.reconcile_workspace(workspace)

    lower, _ = json.loads(route.calls.last.request.content)['filter_criteria']['start_timestamp'][
        'value'
    ]
    assert lower == pytest.approx(mark - 3_600_000, abs=5000)


@respx.mock
def test_a_quiet_night_leaves_the_mark_where_it_was(service, workspace):
    mark = int((timezone.now() - timedelta(hours=5)).timestamp() * 1000)
    workspace.last_call_timestamp_ms = mark
    workspace.save(update_fields=['last_call_timestamp_ms'])
    mock_list(page())

    service.reconcile_workspace(workspace)

    workspace.refresh_from_db()
    assert workspace.last_call_timestamp_ms == mark


# --- backfill --------------------------------------------------------------------------------


@respx.mock
def test_backfills_missing_duration_and_cost(service, workspace):
    make_stored_call(workspace.client)
    mock_list(page())
    route = mock_get_call('call1', duration_ms=90_000, call_cost={'combined_cost': 77.6})

    result = service.reconcile_workspace(workspace)

    assert result['backfilled'] == 1
    assert route.calls.last.request.headers['Authorization'] == f'Bearer {WORKSPACE_KEY}'
    stored = RetellCall.objects.get(call_id='call1')
    assert stored.duration_ms == 90_000
    assert stored.retell_cost_cents == 78


@respx.mock
def test_another_client_s_pending_calls_are_left_to_their_own_workspace(service, workspace):
    """This key cannot read them, so asking would be N 404s a night, every night."""
    other = make_client('loc2', client_name='Branwynn')
    make_stored_call(other, call_id='theirs')
    mock_list(page())
    route = respx.get(f'{BASE}/v2/get-call/theirs')

    result = service.reconcile_workspace(workspace)

    assert result['backfilled'] == 0
    assert not route.called


@respx.mock
def test_backfills_a_call_discovered_in_the_same_run(service, workspace):
    incomplete = recent_call()
    del incomplete['duration_ms']
    del incomplete['call_cost']
    mock_list(page(incomplete))
    mock_get_call('call1', duration_ms=12_345)

    result = service.reconcile_workspace(workspace)

    assert result['stored'] == 1 and result['backfilled'] == 1
    assert RetellCall.objects.get(call_id='call1').duration_ms == 12_345


@respx.mock
def test_complete_calls_are_not_refetched(service, workspace):
    make_stored_call(workspace.client, duration_ms=1000, retell_cost_cents=5)
    mock_list(page())
    route = respx.get(f'{BASE}/v2/get-call/call1')

    result = service.reconcile_workspace(workspace)

    assert result['backfilled'] == 0
    assert not route.called


@respx.mock
def test_calls_older_than_the_window_are_left_alone(service, workspace):
    """The backfill keeps the fixed window even when the listing resumes further back."""
    make_stored_call(workspace.client, minutes_ago=60 * 72)
    mock_list(page())
    route = respx.get(f'{BASE}/v2/get-call/call1')

    result = service.reconcile_workspace(workspace)

    assert result['backfilled'] == 0
    assert not route.called


@respx.mock
def test_one_failed_backfill_does_not_stop_the_others(service, workspace):
    make_stored_call(workspace.client, call_id='bad')
    make_stored_call(workspace.client, call_id='good')
    mock_list(page())
    respx.get(f'{BASE}/v2/get-call/bad').mock(return_value=httpx.Response(500))
    mock_get_call('good', duration_ms=5000)

    result = service.reconcile_workspace(workspace)

    assert result['backfilled'] == 1
    assert RetellCall.objects.get(call_id='bad').duration_ms is None
    assert RetellCall.objects.get(call_id='good').duration_ms == 5000


@respx.mock
def test_backfill_clears_the_unbillable_reason(service, workspace):
    make_stored_call(workspace.client)
    mock_list(page())
    mock_get_call('call1', duration_ms=90_000)

    service.reconcile_workspace(workspace)

    stored = RetellCall.objects.get(call_id='call1')
    assert stored.billing_status == BILLABLE
    assert stored.unbillable_reason == ''


@respx.mock
def test_a_call_that_has_since_ended_becomes_billable(service, workspace):
    """The cost is already there, so only the status makes it a candidate for a re-read."""
    make_stored_call(workspace.client, call_status='ongoing', duration_ms=1000, retell_cost_cents=5)
    mock_list(page())
    mock_get_call('call1', duration_ms=90_000, call_status='ended')

    result = service.reconcile_workspace(workspace)

    assert result['backfilled'] == 1
    stored = RetellCall.objects.get(call_id='call1')
    assert stored.billing_status == BILLABLE
    assert stored.duration_ms == 90_000
    assert stored.call_status == 'ended'


@respx.mock
def test_a_call_still_missing_its_duration_keeps_its_reason(service, workspace):
    make_stored_call(workspace.client)
    mock_list(page())
    payload = call_payload(call_id='call1')
    del payload['duration_ms']
    del payload['call_cost']
    respx.get(f'{BASE}/v2/get-call/call1').mock(return_value=httpx.Response(200, json=payload))

    service.reconcile_workspace(workspace)

    stored = RetellCall.objects.get(call_id='call1')
    assert stored.duration_ms is None
    assert stored.billing_status == UNBILLABLE
    assert stored.unbillable_reason == 'Retell has not reported a duration for this call.'


@respx.mock
def test_backfill_takes_a_duration_given_in_seconds(service, workspace):
    make_stored_call(workspace.client)
    mock_list(page())
    payload = call_payload(call_id='call1')
    del payload['duration_ms']  # leaves call_cost.total_duration_seconds = 15.8
    respx.get(f'{BASE}/v2/get-call/call1').mock(return_value=httpx.Response(200, json=payload))

    service.reconcile_workspace(workspace)

    stored = RetellCall.objects.get(call_id='call1')
    assert stored.duration_ms == 15_800
    assert stored.billing_status == BILLABLE


# --- failure ------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize('status_code', [500, 401])
def test_a_failed_list_raises(service, workspace, status_code):
    respx.post(LIST_URL).mock(return_value=httpx.Response(status_code))

    with pytest.raises(RetellError):
        service.reconcile_workspace(workspace)


@respx.mock
def test_a_connection_failure_raises(service, workspace):
    respx.post(LIST_URL).mock(side_effect=httpx.ConnectError('no route'))

    with pytest.raises(RetellError):
        service.reconcile_workspace(workspace)


@respx.mock
def test_a_body_that_is_not_json_raises(service, workspace):
    respx.post(LIST_URL).mock(return_value=httpx.Response(200, content=b'<html>nope</html>'))

    with pytest.raises(RetellError):
        service.reconcile_workspace(workspace)


@respx.mock
def test_calls_from_earlier_pages_survive_a_later_failure(service, workspace):
    respx.post(LIST_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(call_payload(), pagination_key='key-2')),
            httpx.Response(500),
        ]
    )

    with pytest.raises(RetellError):
        service.reconcile_workspace(workspace)

    assert RetellCall.objects.filter(call_id='call1').exists()


# --- rate limiting ---------------------------------------------------------------------------


@respx.mock
def test_a_429_is_waited_out_once_rather_than_losing_the_night(service, workspace, monkeypatch):
    """Running workspace after workspace, a 429 is the client before this one being busy."""
    slept = []
    monkeypatch.setattr('apps.rebilling.services.clock.sleep', slept.append)
    route = respx.post(LIST_URL).mock(
        side_effect=[
            httpx.Response(429, headers={'RateLimit-Reset': '4'}),
            httpx.Response(200, json=page(call_payload())),
        ]
    )

    result = service.reconcile_workspace(workspace)

    assert slept == [4.0]  # Retell's own header, in seconds
    assert route.call_count == 2
    assert result['stored'] == 1


@respx.mock
def test_a_429_asking_for_longer_than_the_budget_takes_to_refill_is_capped(
    service, workspace, monkeypatch
):
    """This runs behind the admin page's Test button too; nobody waits a minute at a form."""
    slept = []
    monkeypatch.setattr('apps.rebilling.services.clock.sleep', slept.append)
    respx.post(LIST_URL).mock(
        side_effect=[
            httpx.Response(429, headers={'RateLimit-Reset': '600'}),
            httpx.Response(200, json=page()),
        ]
    )

    service.reconcile_workspace(workspace)

    assert slept == [10.0]


@respx.mock
def test_a_429_that_survives_the_wait_raises(service, workspace, monkeypatch):
    monkeypatch.setattr('apps.rebilling.services.clock.sleep', lambda _: None)
    respx.post(LIST_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(RetellError):
        service.reconcile_workspace(workspace)


# --- the whole run ------------------------------------------------------------------------------


@respx.mock
def test_every_workspace_is_audited_with_its_own_key():
    make_workspace(make_client('loc1'), api_key='key_one')
    make_workspace(make_client('loc2', client_name='Branwynn'), api_key='key_two')
    route = mock_list(page(call_payload(call_id='a')), page(call_payload(call_id='b')))

    result = reconcile_all()

    assert result['workspaces'] == 2 and result['failed'] == 0 and result['stored'] == 2
    keys = {call.request.headers['Authorization'] for call in route.calls}
    assert keys == {'Bearer key_one', 'Bearer key_two'}


@respx.mock
def test_a_client_with_no_key_is_simply_not_in_the_run():
    make_client('loc_no_key')
    make_workspace(make_client('loc1'))
    mock_list(page())

    assert reconcile_all()['workspaces'] == 1


@respx.mock
def test_a_run_with_no_workspaces_at_all_does_nothing():
    """Nothing to read anything with, and nothing to fall back to."""
    make_client()

    result = reconcile_all()

    assert result == {'workspaces': 0, 'failed': 0, 'seen': 0, 'stored': 0, 'backfilled': 0}
    assert not respx.calls


@respx.mock
def test_a_bad_key_marks_that_workspace_and_the_run_carries_on():
    broken = make_workspace(make_client('loc1'), api_key='key_broken')
    good = make_workspace(make_client('loc2', client_name='Branwynn'), api_key='key_good')

    def by_key(request):
        refused = request.headers['Authorization'] == 'Bearer key_broken'
        return httpx.Response(401) if refused else httpx.Response(200, json=page(call_payload()))

    respx.post(LIST_URL).mock(side_effect=by_key)

    result = reconcile_all()

    assert result['workspaces'] == 2 and result['failed'] == 1 and result['stored'] == 1
    broken.refresh_from_db()
    good.refresh_from_db()
    assert broken.status == WORKSPACE_KEY_INVALID
    assert broken.last_checked_at is not None
    assert good.status == WORKSPACE_ACTIVE


@respx.mock
def test_a_workspace_that_reads_again_is_marked_active_once_more():
    workspace = make_workspace(status=WORKSPACE_KEY_INVALID)
    mock_list(page())

    reconcile_all()

    workspace.refresh_from_db()
    assert workspace.status == WORKSPACE_ACTIVE
    assert workspace.last_checked_at is not None


@respx.mock
def test_a_failed_workspace_keeps_its_key():
    """Marking a key broken must not be a path that rewrites the key itself."""
    workspace = make_workspace()
    respx.post(LIST_URL).mock(return_value=httpx.Response(401))

    reconcile_all()

    workspace.refresh_from_db()
    assert workspace.api_key == WORKSPACE_KEY


# --- the command ---------------------------------------------------------------------------------


@respx.mock
def test_command_reports_what_it_did():
    make_workspace()
    mock_list(page(call_payload()))
    out = StringIO()

    call_command('reconcile_retell_calls', stdout=out)

    assert '1 workspaces checked, 1 calls checked, 1 stored, 0 filled in.' in out.getvalue()


@respx.mock
def test_command_warns_but_succeeds_when_one_client_of_several_fails():
    """One bad key is that client's problem, already recorded against them."""
    make_workspace(make_client('loc1'), api_key='key_broken')
    make_workspace(make_client('loc2', client_name='Branwynn'), api_key='key_good')
    respx.post(LIST_URL).mock(side_effect=[httpx.Response(401), httpx.Response(200, json=page())])
    out = StringIO()

    call_command('reconcile_retell_calls', stdout=out)

    assert '1 of 2 workspaces could not be read.' in out.getvalue()


@respx.mock
def test_command_fails_when_every_workspace_fails():
    """Systemic: the network, the settings, or Retell itself."""
    make_workspace()
    respx.post(LIST_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(CommandError):
        call_command('reconcile_retell_calls', stdout=StringIO())
