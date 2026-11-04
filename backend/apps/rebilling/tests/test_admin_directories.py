"""The list behind the mapping editor's one remaining picker: Twilio subaccounts.

Nothing is stored. The endpoint reads the live Twilio account so an agency admin picks a real
subaccount instead of pasting a SID into a text box.

There is no Retell equivalent any more. A client's agents are not chosen from a catalogue -
they are whatever is in that client's own Retell workspace, discovered from their own key (see
`test_retell_key.py`). The Stripe customer was never picked either: it is matched
automatically from the sub-account owner's email.
"""

import httpx
import pytest
import respx
from django.conf import settings
from django.urls import reverse

from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency

pytestmark = pytest.mark.django_db

TWILIO_URL = f'{settings.TWILIO_API_BASE_URL}/2010-04-01/Accounts.json'


def get(client, name, blob=VECTOR_AGENCY):
    return client.get(reverse(f'rebilling:{name}'), HTTP_X_GHL_SESSION=blob)


def account(sid, friendly_name='Cool Breeze HVAC'):
    return {
        'sid': sid,
        'friendly_name': friendly_name,
        'status': 'active',
        'owner_account_sid': settings.TWILIO_ACCOUNT_SID,
    }


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


def test_401s_without_a_session(client):
    make_agency()

    assert client.get(reverse('rebilling:admin-twilio-subaccounts')).status_code == 401


def test_a_sub_account_session_cannot_read_the_picker(client):
    """Only an agency admin edits mapping, so only an agency admin sees what to map to."""
    make_agency()

    assert get(client, 'admin-twilio-subaccounts', blob=VECTOR_LOCATION).status_code == 403
