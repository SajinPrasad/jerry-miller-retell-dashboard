"""The HCP client's list reads, empty responses, and how it waits out a 429.

Nothing here reaches Housecall Pro - `respx` intercepts at the httpx transport.
"""

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings

from apps.hcp.exceptions import HCPError, HCPRateLimitError
from apps.hcp.services.hcp_client import HCPClient

BASE = settings.HCP_API_BASE_URL
COMPANY_URL = f'{BASE}/company'
COMPANY = {'id': 'hcp-company-1', 'name': 'Cool Breeze HVAC'}


@pytest.fixture
def waits(monkeypatch):
    """Record the waits instead of sleeping through them."""
    recorded: list[float] = []
    monkeypatch.setattr('apps.common.backoff.pause', recorded.append)
    return recorded


# --- empty responses -------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize('status', [200, 204])
def test_a_delete_with_no_body_is_a_result_not_an_error(status):
    """HCP answers a successful DELETE with 204 or an empty 200; neither has JSON to parse."""
    respx.delete(f'{BASE}/jobs/hcp-job-1/appointments/hcp-appt-1').mock(
        return_value=httpx.Response(status)
    )
    respx.delete(f'{BASE}/jobs/hcp-job-1/line_items/hcp-li-1').mock(
        return_value=httpx.Response(status)
    )
    client = HCPClient('hcp-key')

    client.delete_job_appointment('hcp-job-1', 'hcp-appt-1')
    client.delete_job_line_item('hcp-job-1', 'hcp-li-1')


@respx.mock
def test_a_malformed_body_is_still_an_error():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, content=b'not json'))

    with pytest.raises(HCPError):
        HCPClient('hcp-key').get_company()


# --- list endpoints --------------------------------------------------------------------


@override_settings(HCP_PAGE_SIZE=2)
@respx.mock
@pytest.mark.parametrize(
    ('method', 'path', 'key'),
    [
        ('iter_customers', '/customers', 'customers'),
        ('iter_leads', '/leads', 'leads'),
        ('iter_estimates', '/estimates', 'estimates'),
        ('iter_jobs', '/jobs', 'jobs'),
    ],
)
def test_each_list_endpoint_is_read_page_by_page(method, path, key):
    route = respx.get(f'{BASE}{path}').mock(
        side_effect=[
            httpx.Response(200, json={key: [{'id': 'a'}, {'id': 'b'}]}),
            httpx.Response(200, json={key: [{'id': 'b'}, {'id': 'c'}]}),
            httpx.Response(200, json={key: [{'id': 'd'}]}),
        ]
    )

    rows = list(getattr(HCPClient('hcp-key'), method)())

    # `b` came back twice because the list shifted under the offset paging; it is read once.
    assert [row['id'] for row in rows] == ['a', 'b', 'c', 'd']
    assert route.call_count == 3
    assert route.calls[1].request.url.params['page'] == '2'


@respx.mock
def test_a_list_read_stops_at_an_empty_page():
    route = respx.get(f'{BASE}/customers').mock(
        return_value=httpx.Response(200, json={'customers': []})
    )

    assert list(HCPClient('hcp-key').iter_customers()) == []
    assert route.call_count == 1


# --- backing off a 429 -----------------------------------------------------------------


@respx.mock
def test_a_client_that_opted_in_waits_out_a_429_and_retries(waits):
    route = respx.get(COMPANY_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json=COMPANY),
        ]
    )

    company = HCPClient('hcp-key', max_retries=3).get_company()

    assert company.id == 'hcp-company-1'
    assert route.call_count == 3
    assert waits == [1, 2]


@respx.mock
def test_the_wait_follows_retry_after_when_hcp_sends_one(waits):
    respx.get(COMPANY_URL).mock(
        side_effect=[
            httpx.Response(429, headers={'Retry-After': '7'}),
            httpx.Response(200, json=COMPANY),
        ]
    )

    HCPClient('hcp-key', max_retries=1).get_company()

    assert waits == [7]


@respx.mock
def test_one_wait_is_capped(waits):
    respx.get(COMPANY_URL).mock(
        side_effect=[
            httpx.Response(429, headers={'Retry-After': '600'}),
            httpx.Response(200, json=COMPANY),
        ]
    )

    HCPClient('hcp-key', max_retries=1).get_company()

    assert waits == [30]


@respx.mock
def test_a_429_is_raised_once_the_retries_run_out(waits):
    route = respx.get(COMPANY_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(HCPRateLimitError):
        HCPClient('hcp-key', max_retries=2).get_company()

    assert route.call_count == 3
    assert len(waits) == 2


@respx.mock
def test_a_client_that_did_not_opt_in_raises_a_429_at_once(waits):
    """Onboarding is a person at a screen: they are told, rather than kept waiting."""
    route = respx.get(COMPANY_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(HCPRateLimitError):
        HCPClient('hcp-key').get_company()

    assert route.call_count == 1
    assert waits == []


@respx.mock
def test_other_failures_are_not_retried(waits):
    route = respx.get(COMPANY_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(HCPError):
        HCPClient('hcp-key', max_retries=3).get_company()

    assert route.call_count == 1
