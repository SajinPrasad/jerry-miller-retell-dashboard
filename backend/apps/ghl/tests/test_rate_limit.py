"""How `GHLOAuthService` waits out a 429, for a caller that opted in to waiting."""

import httpx
import pytest
import respx
from django.conf import settings

from apps.ghl.exceptions import GHLError, GHLRateLimitError
from apps.ghl.services import GHLOAuthService

BASE = settings.GHL_API_BASE_URL


@pytest.fixture
def waits(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr('apps.common.backoff.pause', recorded.append)
    return recorded


def make_service(**kwargs):
    return GHLOAuthService(client=httpx.Client(base_url=BASE), **kwargs)


@respx.mock
def test_a_service_that_opted_in_waits_out_a_429_and_retries(waits):
    route = respx.get(f'{BASE}/thing').mock(
        side_effect=[
            httpx.Response(429, headers={'Retry-After': '3'}),
            httpx.Response(429, headers={'X-RateLimit-Interval-Milliseconds': '1500'}),
            httpx.Response(200, json={'ok': True}),
        ]
    )

    assert make_service(max_retries=3)._request('GET', '/thing', token='t') == {'ok': True}
    assert route.call_count == 3
    assert waits == [3, 1.5]


@respx.mock
def test_a_429_without_headers_backs_off_exponentially(waits):
    respx.get(f'{BASE}/thing').mock(
        side_effect=[httpx.Response(429), httpx.Response(429), httpx.Response(200, json={})]
    )

    make_service(max_retries=3)._request('GET', '/thing', token='t')

    assert waits == [1, 2]


@respx.mock
def test_a_429_is_raised_once_the_retries_run_out(waits):
    route = respx.get(f'{BASE}/thing').mock(return_value=httpx.Response(429))

    with pytest.raises(GHLRateLimitError):
        make_service(max_retries=2)._request('GET', '/thing', token='t')

    assert route.call_count == 3


@respx.mock
def test_a_service_that_did_not_opt_in_raises_a_429_at_once(waits):
    """Billing and the admin page are people waiting on a screen; they see it, not a sleep."""
    route = respx.get(f'{BASE}/thing').mock(return_value=httpx.Response(429))

    with pytest.raises(GHLError):
        make_service()._request('GET', '/thing', token='t')

    assert route.call_count == 1
    assert waits == []


@respx.mock
def test_other_failures_are_not_retried(waits):
    route = respx.get(f'{BASE}/thing').mock(return_value=httpx.Response(500))

    with pytest.raises(GHLError):
        make_service(max_retries=3)._request('GET', '/thing', token='t')

    assert route.call_count == 1
