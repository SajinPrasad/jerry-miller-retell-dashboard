"""The two lists behind the mapping editor's pickers: Retell agents, Twilio subaccounts.

Nothing is stored. Both endpoints read the live provider account so an agency admin picks a
real agent or subaccount instead of pasting an id into a text box. The Stripe customer is not
among them: it is matched automatically from the sub-account owner's email, never picked.
"""

import httpx
import pytest
import respx
from django.conf import settings
from django.urls import reverse

from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency

pytestmark = pytest.mark.django_db

RETELL_URL = f'{settings.RETELL_API_BASE_URL}/v2/list-agents'
TWILIO_URL = f'{settings.TWILIO_API_BASE_URL}/2010-04-01/Accounts.json'


def get(client, name, blob=VECTOR_AGENCY):
    return client.get(reverse(f'rebilling:{name}'), HTTP_X_GHL_SESSION=blob)


def agent(agent_id, agent_name='Front desk'):
    return {
        'agent_id': agent_id,
        'agent_name': agent_name,
        'channel': 'voice',
        'tags': {},
        'user_modified_timestamp': 1_767_000_000_000,
    }


def account(sid, friendly_name='Cool Breeze HVAC'):
    return {
        'sid': sid,
        'friendly_name': friendly_name,
        'status': 'active',
        'owner_account_sid': settings.TWILIO_ACCOUNT_SID,
    }


# --- Retell agents -------------------------------------------------------------------------


@respx.mock
def test_lists_retell_agents_by_name(client):
    make_agency()
    respx.post(RETELL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                'items': [agent('agent_2', 'Service calls'), agent('agent_1', 'After hours')],
                'has_more': False,
            },
        )
    )

    response = get(client, 'admin-retell-agents')

    assert response.status_code == 200
    # Only what a picker needs, alphabetically: Retell's own order is by timestamp.
    assert response.data['agents'] == [
        {'agent_id': 'agent_1', 'agent_name': 'After hours'},
        {'agent_id': 'agent_2', 'agent_name': 'Service calls'},
    ]


@respx.mock
def test_follows_retell_pagination(client):
    make_agency()
    respx.post(RETELL_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    'items': [agent('agent_1', 'After hours')],
                    'has_more': True,
                    'pagination_key': 'agent_1',
                },
            ),
            httpx.Response(
                200, json={'items': [agent('agent_2', 'Service calls')], 'has_more': False}
            ),
        ]
    )

    response = get(client, 'admin-retell-agents')

    assert [row['agent_id'] for row in response.data['agents']] == ['agent_1', 'agent_2']
    assert respx.calls[1].request.url.params['pagination_key'] == 'agent_1'


@respx.mock
def test_a_failing_retell_call_is_a_502(client):
    make_agency()
    respx.post(RETELL_URL).mock(return_value=httpx.Response(500))

    response = get(client, 'admin-retell-agents')

    assert response.status_code == 502


# --- Twilio subaccounts --------------------------------------------------------------------


@respx.mock
def test_lists_twilio_subaccounts_without_the_parent(client):
    make_agency()
    respx.get(TWILIO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                'accounts': [
                    account(settings.TWILIO_ACCOUNT_SID, 'SurPro Labs AI'),
                    account('AC' + '2' * 32, 'Polar Peak Heating'),
                    account('AC' + '1' * 32, 'Cool Breeze HVAC'),
                ],
                'next_page_uri': None,
            },
        )
    )

    response = get(client, 'admin-twilio-subaccounts')

    assert response.status_code == 200
    # The parent is gone: a client mapped to it would be billed for everyone's minutes.
    assert response.data['subaccounts'] == [
        {'sid': 'AC' + '1' * 32, 'friendly_name': 'Cool Breeze HVAC'},
        {'sid': 'AC' + '2' * 32, 'friendly_name': 'Polar Peak Heating'},
    ]


@respx.mock
def test_follows_twilio_pagination(client):
    make_agency()
    next_uri = '/2010-04-01/Accounts.json?Page=1&PageToken=PAteam'
    respx.get(TWILIO_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    'accounts': [account('AC' + '1' * 32, 'Cool Breeze HVAC')],
                    'next_page_uri': next_uri,
                },
            ),
            httpx.Response(
                200,
                json={
                    'accounts': [account('AC' + '2' * 32, 'Polar Peak Heating')],
                    'next_page_uri': None,
                },
            ),
        ]
    )

    response = get(client, 'admin-twilio-subaccounts')

    assert [row['friendly_name'] for row in response.data['subaccounts']] == [
        'Cool Breeze HVAC',
        'Polar Peak Heating',
    ]
    # Twilio's own cursor is followed verbatim rather than rebuilt from a page number.
    assert respx.calls[1].request.url.params['PageToken'] == 'PAteam'


@respx.mock
def test_a_failing_twilio_call_is_a_502(client):
    make_agency()
    respx.get(TWILIO_URL).mock(return_value=httpx.Response(401))

    response = get(client, 'admin-twilio-subaccounts')

    assert response.status_code == 502


# --- unauthorized callers are blocked ------------------------------------------------------


@pytest.mark.parametrize('name', ['admin-retell-agents', 'admin-twilio-subaccounts'])
def test_401s_without_a_session(client, name):
    make_agency()

    assert client.get(reverse(f'rebilling:{name}')).status_code == 401


@pytest.mark.parametrize('name', ['admin-retell-agents', 'admin-twilio-subaccounts'])
def test_a_sub_account_session_cannot_read_the_pickers(client, name):
    """Only an agency admin edits mapping, so only an agency admin sees what to map to."""
    make_agency()

    assert get(client, name, blob=VECTOR_LOCATION).status_code == 403
