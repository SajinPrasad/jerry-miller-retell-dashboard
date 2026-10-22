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
from apps.rebilling.models import RetellCall, UnassignedCall
from apps.rebilling.services import RetellReconciliationService

from .test_services import call_payload, make_client

pytestmark = pytest.mark.django_db

BASE = settings.RETELL_API_BASE_URL
LIST_URL = f'{BASE}/v3/list-calls'


@pytest.fixture
def service():
    return RetellReconciliationService()


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
    fields = {
        'call_id': call_id,
        'client': client,
        'started_at': timezone.now() - timedelta(minutes=minutes_ago),
        'duration_ms': None,
        'retell_cost_cents': None,
    }
    fields.update(overrides)
    return RetellCall.objects.create(**fields)


@respx.mock
def test_stores_a_call_the_webhook_missed(service):
    client = make_client()
    mock_list(page(recent_call()))

    result = service.reconcile()

    assert result['seen'] == 1 and result['stored'] == 1
    stored = RetellCall.objects.get(call_id='call1')
    assert stored.client == client
    assert stored.duration_ms == 15791


@respx.mock
def test_sends_the_window_and_auth(service):
    make_client()
    route = mock_list(page())
    before = timezone.now()

    service.reconcile()

    request = route.calls.last.request
    assert request.headers['Authorization'] == f'Bearer {settings.RETELL_API_KEY}'
    sent = json.loads(request.content)
    window = sent['filter_criteria']['start_timestamp']
    assert window['type'] == 'range' and window['op'] == 'bt'
    lower, upper = window['value']
    assert lower == pytest.approx((before - timedelta(hours=48)).timestamp() * 1000, abs=5000)
    assert upper == pytest.approx(before.timestamp() * 1000, abs=5000)
    assert sent['limit'] == 1000


@respx.mock
def test_a_call_we_already_have_is_not_stored_twice(service):
    make_client()
    mock_list(page(call_payload()))
    service.reconcile()
    respx.reset()
    mock_list(page(call_payload()))

    result = service.reconcile()

    assert result['seen'] == 1 and result['stored'] == 0
    assert RetellCall.objects.count() == 1


@respx.mock
def test_unmatched_call_is_stored_as_unassigned(service):
    mock_list(page(call_payload()))

    service.reconcile()

    assert UnassignedCall.objects.filter(call_id='call1').exists()


@respx.mock
def test_pages_through_with_the_pagination_key(service):
    make_client()
    route = mock_list(
        page(call_payload(call_id='call1'), pagination_key='key-2'),
        page(call_payload(call_id='call2'), pagination_key='key-3'),
        page(call_payload(call_id='call3')),
    )

    result = service.reconcile()

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
def test_stops_when_has_more_is_true_but_no_key_comes_back(service):
    """Guards the pager against looping forever on a malformed page."""
    make_client()
    route = mock_list({'items': [call_payload()], 'has_more': True})

    result = service.reconcile()

    assert route.call_count == 1 and result['seen'] == 1


@respx.mock
def test_backfills_missing_duration_and_cost(service):
    client = make_client()
    make_stored_call(client)
    mock_list(page())
    route = mock_get_call('call1', duration_ms=90_000, call_cost={'combined_cost': 77.6})

    result = service.reconcile()

    assert result['backfilled'] == 1
    assert route.calls.last.request.headers['Authorization'] == f'Bearer {settings.RETELL_API_KEY}'
    stored = RetellCall.objects.get(call_id='call1')
    assert stored.duration_ms == 90_000
    assert stored.retell_cost_cents == 78


@respx.mock
def test_backfills_an_unassigned_call_too(service):
    UnassignedCall.objects.create(call_id='orphan', started_at=timezone.now() - timedelta(hours=1))
    mock_list(page())
    mock_get_call('orphan', duration_ms=30_000)

    result = service.reconcile()

    assert result['backfilled'] == 1
    assert UnassignedCall.objects.get(call_id='orphan').duration_ms == 30_000


@respx.mock
def test_backfills_a_call_discovered_in_the_same_run(service):
    make_client()
    incomplete = recent_call()
    del incomplete['duration_ms']
    del incomplete['call_cost']
    mock_list(page(incomplete))
    mock_get_call('call1', duration_ms=12_345)

    result = service.reconcile()

    assert result['stored'] == 1 and result['backfilled'] == 1
    assert RetellCall.objects.get(call_id='call1').duration_ms == 12_345


@respx.mock
def test_complete_calls_are_not_refetched(service):
    client = make_client()
    make_stored_call(client, duration_ms=1000, retell_cost_cents=5)
    mock_list(page())
    route = respx.get(f'{BASE}/v2/get-call/call1')

    result = service.reconcile()

    assert result['backfilled'] == 0
    assert not route.called


@respx.mock
def test_calls_older_than_the_window_are_left_alone(service):
    client = make_client()
    make_stored_call(client, minutes_ago=60 * 72)
    mock_list(page())
    route = respx.get(f'{BASE}/v2/get-call/call1')

    result = service.reconcile()

    assert result['backfilled'] == 0
    assert not route.called


@respx.mock
def test_one_failed_backfill_does_not_stop_the_others(service):
    client = make_client()
    make_stored_call(client, call_id='bad')
    make_stored_call(client, call_id='good')
    mock_list(page())
    respx.get(f'{BASE}/v2/get-call/bad').mock(return_value=httpx.Response(500))
    mock_get_call('good', duration_ms=5000)

    result = service.reconcile()

    assert result['backfilled'] == 1
    assert RetellCall.objects.get(call_id='bad').duration_ms is None
    assert RetellCall.objects.get(call_id='good').duration_ms == 5000


@respx.mock
@pytest.mark.parametrize('status_code', [429, 500, 401])
def test_a_failed_list_raises(service, status_code):
    respx.post(LIST_URL).mock(return_value=httpx.Response(status_code))

    with pytest.raises(RetellError):
        service.reconcile()


@respx.mock
def test_a_connection_failure_raises(service):
    respx.post(LIST_URL).mock(side_effect=httpx.ConnectError('no route'))

    with pytest.raises(RetellError):
        service.reconcile()


@respx.mock
def test_a_body_that_is_not_json_raises(service):
    respx.post(LIST_URL).mock(return_value=httpx.Response(200, content=b'<html>nope</html>'))

    with pytest.raises(RetellError):
        service.reconcile()


@respx.mock
def test_calls_from_earlier_pages_survive_a_later_failure(service):
    make_client()
    respx.post(LIST_URL).mock(
        side_effect=[
            httpx.Response(200, json=page(call_payload(), pagination_key='key-2')),
            httpx.Response(500),
        ]
    )

    with pytest.raises(RetellError):
        service.reconcile()

    assert RetellCall.objects.filter(call_id='call1').exists()


@respx.mock
def test_command_reports_what_it_did():
    make_client()
    mock_list(page(call_payload()))
    out = StringIO()

    call_command('reconcile_retell_calls', stdout=out)

    assert '1 calls checked, 1 stored, 0 filled in.' in out.getvalue()


@respx.mock
def test_command_fails_when_retell_does():
    respx.post(LIST_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(CommandError):
        call_command('reconcile_retell_calls', stdout=StringIO())
