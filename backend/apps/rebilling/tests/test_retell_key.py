"""Setting a client's Retell workspace key: `/admin/clients/<location_id>/retell-key/`.

`POST` tests a key, `PUT` saves it. Both run the same check against Retell and both answer
with the agent names the key found, because reading those names is the only way to catch the
one failure no code can: a perfectly valid key belonging to the *wrong* client's workspace.
"""

import httpx
import pytest
import respx
from django.conf import settings
from django.db import connection
from django.urls import reverse

from apps.ghl.models import GHLOAuthToken
from apps.ghl.tests.test_sso import VECTOR_AGENCY, VECTOR_LOCATION, make_agency
from apps.rebilling.models import ClientMapping, RetellWorkspace

from .test_services import WORKSPACE_KEY, make_client, make_workspace

pytestmark = pytest.mark.django_db

AGENTS_URL = f'{settings.RETELL_API_BASE_URL}/v2/list-agents'


def url(location_id='loc1'):
    return reverse('rebilling:admin-client-retell-key', args=[location_id])


def make_location(location_id='loc1', name='Cool Breeze HVAC'):
    """A sub-account Task 1 has onboarded. Pasting a key is only allowed for one of these."""
    return GHLOAuthToken.objects.create(
        company_id='company1',
        location_id=location_id,
        name=name,
        is_agency=False,
        access_token='token',
        expires_at='2099-01-01T00:00:00Z',
    )


def agent(agent_id, agent_name='Front desk'):
    return {
        'agent_id': agent_id,
        'agent_name': agent_name,
        'channel': 'voice',
        'tags': {},
        'user_modified_timestamp': 1_767_000_000_000,
    }


def mock_agents(*agents, status_code=200):
    body = {'items': list(agents), 'has_more': False}
    return respx.post(AGENTS_URL).mock(
        return_value=httpx.Response(status_code, json=body if status_code == 200 else None)
    )


def send(client, method, location_id='loc1', blob=VECTOR_AGENCY, **body):
    return getattr(client, method)(
        url(location_id),
        data=body or {'api_key': 'key_pasted'},
        content_type='application/json',
        HTTP_X_GHL_SESSION=blob,
    )


# --- testing a key ------------------------------------------------------------------------


@respx.mock
def test_a_test_answers_with_the_agents_it_found_and_stores_nothing(client):
    make_agency()
    make_location()
    route = mock_agents(agent('agent_2', 'Service calls'), agent('agent_1', 'After hours'))

    response = send(client, 'post')

    assert response.status_code == 200
    # Sorted by name: this list is read by a person deciding whether it is the right client.
    assert response.data['agents'] == [
        {'agent_id': 'agent_1', 'agent_name': 'After hours'},
        {'agent_id': 'agent_2', 'agent_name': 'Service calls'},
    ]
    assert route.calls.last.request.headers['Authorization'] == 'Bearer key_pasted'
    assert not RetellWorkspace.objects.exists()
    assert not ClientMapping.objects.exists()


@respx.mock
def test_a_key_retell_will_not_answer_for_is_a_502_naming_the_badge(client):
    make_agency()
    make_location()
    mock_agents(status_code=401)

    response = send(client, 'post')

    assert response.status_code == 502
    assert 'webhook badge' in response.data['detail']


def test_a_blank_key_is_rejected_before_retell_is_asked(client):
    make_agency()
    make_location()

    response = send(client, 'post', api_key='')

    assert response.status_code == 400


# --- saving a key -------------------------------------------------------------------------


@respx.mock
def test_saving_stores_the_key_and_the_workspace_s_agents(client):
    make_agency()
    make_location()
    mock_agents(agent('agent_1', 'After hours'))

    response = send(client, 'put', api_key='key_pasted', workspace_name='Cool Breeze')

    assert response.status_code == 200
    workspace = RetellWorkspace.objects.get()
    assert workspace.api_key == 'key_pasted'
    assert workspace.workspace_name == 'Cool Breeze'
    assert workspace.status == 'active'
    assert workspace.last_checked_at is not None
    # The agents are discovered, never typed: this is what the agent check reads later.
    assert workspace.client.retell_agent_ids == ['agent_1']


@respx.mock
def test_saving_sets_a_client_up_who_was_not_mapped_yet(client):
    """Pasting a workspace key *is* setting a client up - a call needs a client to land on."""
    make_agency()
    make_location(name='Cool Breeze HVAC')
    mock_agents(agent('agent_1'))

    send(client, 'put')

    mapping = ClientMapping.objects.get(location_id='loc1')
    assert mapping.client_name == 'Cool Breeze HVAC'


@respx.mock
def test_the_key_is_never_sent_back(client):
    make_agency()
    make_location()
    mock_agents(agent('agent_1'))

    response = send(client, 'put', api_key='key_pasted_secret')

    assert 'key_pasted_secret' not in str(response.data)
    assert response.data['key_last4'] == 'cret'


@respx.mock
def test_the_key_is_encrypted_at_rest(client):
    make_agency()
    make_location()
    mock_agents(agent('agent_1'))

    send(client, 'put', api_key='key_pasted_secret')

    with connection.cursor() as cursor:
        cursor.execute('SELECT api_key FROM rebilling_retell_workspace')
        assert 'key_pasted_secret' not in cursor.fetchone()[0]


@respx.mock
def test_a_save_re_tests_rather_than_trusting_the_page(client):
    """ "Tested" has to be something the server knows, not something the browser asserts."""
    make_agency()
    make_location()
    route = mock_agents(status_code=500)

    response = send(client, 'put')

    assert response.status_code == 502
    assert route.called
    assert not RetellWorkspace.objects.exists()


@respx.mock
def test_a_rotated_key_replaces_the_old_one(client):
    make_agency()
    make_location()
    make_workspace(make_client())
    mock_agents(agent('agent_1'))

    send(client, 'put', api_key='key_rotated')

    workspace = RetellWorkspace.objects.get()
    assert workspace.api_key == 'key_rotated'
    assert workspace.api_key != WORKSPACE_KEY


@respx.mock
def test_re_saving_brings_the_agent_list_back_in_line_with_the_workspace(client):
    """A re-save is how a stale id, learned from a call and since deleted, gets cleaned up."""
    make_agency()
    make_location()
    make_workspace(make_client(retell_agent_ids=['agent_gone', 'agent_1']))
    mock_agents(agent('agent_1'))

    send(client, 'put')

    assert ClientMapping.objects.get().retell_agent_ids == ['agent_1']


@respx.mock
def test_a_sub_account_that_was_never_onboarded_is_a_404(client):
    make_agency()

    assert send(client, 'put').status_code == 404


# --- who may do it ---------------------------------------------------------------------------


@pytest.mark.parametrize('method', ['post', 'put'])
def test_401s_without_a_session(client, method):
    make_agency()
    make_location()

    response = getattr(client, method)(
        url(), data={'api_key': 'key_pasted'}, content_type='application/json'
    )

    assert response.status_code == 401


@respx.mock
@pytest.mark.parametrize('method', ['post', 'put'])
def test_a_sub_account_session_cannot_touch_the_key(client, method):
    """A credential that reads a whole workspace is not a client's to set, even their own."""
    make_agency()
    make_location()
    route = mock_agents(agent('agent_1'))

    assert send(client, method, blob=VECTOR_LOCATION).status_code == 403
    assert not route.called
    assert not RetellWorkspace.objects.exists()
