"""The Housecall Pro API client: the two onboarding reads, and what each failure becomes.

Nothing here reaches Housecall Pro - `respx` intercepts at the httpx transport, so the request
the client actually builds (path, headers, params) is what gets asserted on.
"""

import json

import httpx
import pytest
import respx
from django.conf import settings
from django.test import override_settings

from apps.hcp.exceptions import HCPAuthError, HCPError, HCPRateLimitError
from apps.hcp.services.hcp_client import HCPClient

BASE = settings.HCP_API_BASE_URL
COMPANY_URL = f'{BASE}/company'
EMPLOYEES_URL = f'{BASE}/employees'

COMPANY = {
    'id': 'hcp-company-1',
    'name': 'Cool Breeze HVAC',
    'phone_number': '+15551230000',
    'support_email': 'support@coolbreeze.test',
    'time_zone': 'America/Chicago',
}


def employee(employee_id, first='Ana', last='Ruiz'):
    return {
        'id': employee_id,
        'first_name': first,
        'last_name': last,
        'email': f'{employee_id}@coolbreeze.test',
        'mobile_number': '+15559990000',
    }


# --- GET /company -----------------------------------------------------------------------


@respx.mock
def test_get_company_reads_the_company():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))

    company = HCPClient('hcp-key').get_company()

    assert company.id == 'hcp-company-1'
    assert company.name == 'Cool Breeze HVAC'
    assert company.support_email == 'support@coolbreeze.test'
    assert company.time_zone == 'America/Chicago'


@respx.mock
def test_sends_the_token_scheme_not_bearer():
    """HCP's static per-company keys use `Authorization: Token`; `Bearer` is rejected."""
    route = respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=COMPANY))

    HCPClient('hcp-key').get_company()

    assert route.calls.last.request.headers['Authorization'] == 'Token hcp-key'


@respx.mock
def test_accepts_a_company_nested_under_its_own_key():
    """Some accounts answer `{"company": {...}}` rather than the body flat."""
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json={'company': COMPANY}))

    assert HCPClient('hcp-key').get_company().id == 'hcp-company-1'


@respx.mock
def test_a_company_with_no_id_is_an_error():
    """A 200 with nothing identifying the company cannot be stored - `hcp_company_id` is the
    key everything else about the account hangs off.
    """
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json={'name': 'Nameless'}))

    with pytest.raises(HCPError):
        HCPClient('hcp-key').get_company()


@respx.mock
def test_null_string_fields_become_empty_strings():
    """HCP sends `null` for an unset field; the model columns are non-nullable `CharField`s."""
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(
            200, json={'id': 'hcp-1', 'name': 'X', 'phone_number': None, 'support_email': None}
        )
    )

    company = HCPClient('hcp-key').get_company()

    assert company.phone_number == ''
    assert company.support_email == ''


@respx.mock
def test_a_non_object_body_is_an_error():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, json=['not', 'an', 'object']))

    with pytest.raises(HCPError):
        HCPClient('hcp-key').get_company()


# --- failures ---------------------------------------------------------------------------


@pytest.mark.parametrize('code', [401, 403])
@respx.mock
def test_a_rejected_key_raises_hcp_auth_error(code):
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(code, json={'error': 'nope'}))

    with pytest.raises(HCPAuthError):
        HCPClient('wrong-key').get_company()


@respx.mock
def test_the_auth_error_never_quotes_the_key_back():
    """The body can echo the credential, and an error message ends up in a log."""
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(401, json={'error': 'bad token: super-secret-key'})
    )

    with pytest.raises(HCPAuthError) as caught:
        HCPClient('super-secret-key').get_company()

    assert 'super-secret-key' not in str(caught.value)


@respx.mock
def test_a_rate_limit_carries_retry_after():
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(429, headers={'Retry-After': '30'}, json={})
    )

    with pytest.raises(HCPRateLimitError) as caught:
        HCPClient('hcp-key').get_company()

    assert caught.value.retry_after == 30


@respx.mock
def test_a_rate_limit_without_a_usable_header_has_no_retry_after():
    respx.get(COMPANY_URL).mock(
        return_value=httpx.Response(429, headers={'Retry-After': 'Mon, 1 Jan 2035 00:00:00 GMT'})
    )

    with pytest.raises(HCPRateLimitError) as caught:
        HCPClient('hcp-key').get_company()

    assert caught.value.retry_after is None


@respx.mock
def test_a_timeout_raises_hcp_error_not_an_httpx_error():
    respx.get(COMPANY_URL).mock(side_effect=httpx.ConnectTimeout('too slow'))

    with pytest.raises(HCPError, match='timed out'):
        HCPClient('hcp-key').get_company()


@respx.mock
def test_a_server_error_is_an_ordinary_hcp_error():
    """Not an auth error and not a rate limit: the view turns this one into a 502."""
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(500, text='boom'))

    with pytest.raises(HCPError) as caught:
        HCPClient('hcp-key').get_company()

    assert not isinstance(caught.value, HCPAuthError | HCPRateLimitError)


@respx.mock
def test_a_body_that_is_not_json_is_an_hcp_error():
    respx.get(COMPANY_URL).mock(return_value=httpx.Response(200, text='<html>down</html>'))

    with pytest.raises(HCPError):
        HCPClient('hcp-key').get_company()


# --- GET /employees ---------------------------------------------------------------------


@respx.mock
def test_get_employees_reads_one_page():
    respx.get(EMPLOYEES_URL).mock(
        return_value=httpx.Response(
            200, json={'employees': [employee('emp-1'), employee('emp-2')], 'total_pages': 1}
        )
    )

    employees = HCPClient('hcp-key').get_employees()

    assert [item.id for item in employees] == ['emp-1', 'emp-2']
    assert employees[0].name == 'Ana Ruiz'


@override_settings(HCP_PAGE_SIZE=2)
@respx.mock
def test_get_employees_follows_pages():
    respx.get(EMPLOYEES_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={'employees': [employee('emp-1'), employee('emp-2')], 'total_pages': 2}
            ),
            httpx.Response(200, json={'employees': [employee('emp-3')], 'total_pages': 2}),
        ]
    )

    assert [item.id for item in HCPClient('hcp-key').get_employees()] == [
        'emp-1',
        'emp-2',
        'emp-3',
    ]


@override_settings(HCP_PAGE_SIZE=2)
@respx.mock
def test_a_short_page_ends_the_read_without_total_pages():
    route = respx.get(EMPLOYEES_URL).mock(
        return_value=httpx.Response(200, json={'employees': [employee('emp-1')]})
    )

    assert len(HCPClient('hcp-key').get_employees()) == 1
    assert route.call_count == 1


@override_settings(HCP_PAGE_SIZE=2)
@respx.mock
def test_an_employee_repeated_across_pages_is_returned_once():
    """Offset paging hands the same row back twice when the list shifts underneath it."""
    respx.get(EMPLOYEES_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={'employees': [employee('emp-1'), employee('emp-2')], 'total_pages': 2}
            ),
            httpx.Response(
                200, json={'employees': [employee('emp-2'), employee('emp-3')], 'total_pages': 2}
            ),
        ]
    )

    assert [item.id for item in HCPClient('hcp-key').get_employees()] == [
        'emp-1',
        'emp-2',
        'emp-3',
    ]


@respx.mock
def test_an_employee_with_no_id_is_skipped():
    """It cannot be mapped, so offering it would be offering a row that can never be saved."""
    respx.get(EMPLOYEES_URL).mock(
        return_value=httpx.Response(
            200, json={'employees': [{'first_name': 'Ghost'}, employee('emp-1')]}
        )
    )

    assert [item.id for item in HCPClient('hcp-key').get_employees()] == ['emp-1']


@respx.mock
def test_an_employee_with_no_name_falls_back_to_email_then_id():
    respx.get(EMPLOYEES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                'employees': [
                    {'id': 'emp-1', 'email': 'nameless@coolbreeze.test'},
                    {'id': 'emp-2'},
                ]
            },
        )
    )

    employees = HCPClient('hcp-key').get_employees()

    assert employees[0].name == 'nameless@coolbreeze.test'
    assert employees[1].name == 'emp-2'


@respx.mock
def test_no_employees_is_an_empty_list_not_an_error():
    respx.get(EMPLOYEES_URL).mock(return_value=httpx.Response(200, json={'employees': []}))

    assert HCPClient('hcp-key').get_employees() == []


@respx.mock
def test_get_employees_sends_the_page_parameters():
    route = respx.get(EMPLOYEES_URL).mock(return_value=httpx.Response(200, json={'employees': []}))

    HCPClient('hcp-key').get_employees()

    assert dict(route.calls.last.request.url.params) == {
        'page': '1',
        'page_size': str(settings.HCP_PAGE_SIZE),
    }


# --- Phase 4 writes (GHL -> HCP) ---------------------------------------------------------


@respx.mock
def test_create_customer_posts_to_customers():
    route = respx.post(f'{BASE}/customers').mock(
        return_value=httpx.Response(200, json={'customer': {'id': 'hcp-customer-1'}})
    )

    customer = HCPClient('hcp-key').create_customer({'first_name': 'Jane'})

    assert customer['id'] == 'hcp-customer-1'
    assert json.loads(route.calls.last.request.content) == {'first_name': 'Jane'}


@respx.mock
def test_update_customer_puts_to_the_customer_id():
    respx.put(f'{BASE}/customers/hcp-customer-1').mock(
        return_value=httpx.Response(200, json={'customer': {'id': 'hcp-customer-1'}})
    )

    HCPClient('hcp-key').update_customer('hcp-customer-1', {'email': 'jane@example.test'})


@respx.mock
def test_create_job_line_item_posts_under_the_job():
    respx.post(f'{BASE}/jobs/hcp-job-1/line_items').mock(
        return_value=httpx.Response(200, json={'line_item': {'id': 'hcp-li-1'}})
    )

    line_item = HCPClient('hcp-key').create_job_line_item('hcp-job-1', {'name': 'Filter'})

    assert line_item['id'] == 'hcp-li-1'


@respx.mock
def test_delete_job_line_item_deletes_under_the_job():
    route = respx.delete(f'{BASE}/jobs/hcp-job-1/line_items/hcp-li-1').mock(
        return_value=httpx.Response(200, json={})
    )

    HCPClient('hcp-key').delete_job_line_item('hcp-job-1', 'hcp-li-1')

    assert route.called


@respx.mock
def test_update_estimate_option_schedule_puts_the_nested_path():
    route = respx.put(f'{BASE}/estimates/hcp-estimate-1/options/hcp-option-1/schedule').mock(
        return_value=httpx.Response(200, json={})
    )

    HCPClient('hcp-key').update_estimate_option_schedule(
        'hcp-estimate-1', 'hcp-option-1', {'start_time': '2026-11-20T15:00:00+00:00'}
    )

    assert route.called


@respx.mock
def test_update_job_appointment_puts_under_the_job():
    respx.put(f'{BASE}/jobs/hcp-job-1/appointments/hcp-appt-1').mock(
        return_value=httpx.Response(200, json={'appointment': {'id': 'hcp-appt-1'}})
    )

    HCPClient('hcp-key').update_job_appointment(
        'hcp-job-1', 'hcp-appt-1', {'start_time': '2026-11-20T15:00:00+00:00'}
    )


@respx.mock
def test_delete_job_appointment_deletes_under_the_job():
    route = respx.delete(f'{BASE}/jobs/hcp-job-1/appointments/hcp-appt-1').mock(
        return_value=httpx.Response(200, json={})
    )

    HCPClient('hcp-key').delete_job_appointment('hcp-job-1', 'hcp-appt-1')

    assert route.called


@respx.mock
def test_get_job_appointments_reads_the_list():
    respx.get(f'{BASE}/jobs/hcp-job-1/appointments').mock(
        return_value=httpx.Response(200, json={'appointments': [{'id': 'hcp-appt-1'}]})
    )

    appointments = HCPClient('hcp-key').get_job_appointments('hcp-job-1')

    assert appointments == [{'id': 'hcp-appt-1'}]
