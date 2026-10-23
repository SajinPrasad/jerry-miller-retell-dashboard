"""The GHL session blob behind the embedded admin page.

The two `VECTOR_*` constants were produced by OpenSSL itself:

    printf '%s' '<json>' | openssl enc -aes-256-cbc -md md5 -pass pass:test-sso-secret -base64 -A

That is the same envelope CryptoJS writes when given a passphrase, which is what GHL uses. They
are hard-coded rather than generated here on purpose: a fixture built with our own derivation
would pass even if the derivation were wrong, and these do not.
"""

import base64
import json

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.urls import reverse
from django.utils import timezone

from apps.ghl import sso
from apps.ghl.models import GHLOAuthToken

pytestmark = pytest.mark.django_db

# {"userId":"user-1","companyId":"comp_demo","role":"admin","type":"agency",
#  "userName":"Jerry Miller","email":"jerry@example.test","isAgencyOwner":true}
VECTOR_AGENCY = (
    'U2FsdGVkX1/ISl2uIlRk69q6MYvu7k6ZhWS1aB5U+zZvzbMrxYe3BoxBm9eae6ktUMFMhm3sebe2v36XWbzcX6B+'
    'KCSpMaVWaN5XxVIjSxgxK6Pg+ar7LxPIW1EA5Rdg3Ac8AuG2ivJ+6XGxnrIc88WP5J6MXriy7NF+tjgoD34Md9Qv'
    'THX+dV9iLbcp2eNEMacDPO63GR8vlwN8CIB3//m+J/RsrT4yX/SDoAEF2Bk='
)

# As above but with "activeLocation":"loc-cool-breeze", and type still reported as "agency" -
# which is exactly why scope is decided on activeLocation instead.
VECTOR_LOCATION = (
    'U2FsdGVkX1/O2Pu+BTCPHxojlSgnbydapWm6xFUvyYZT5fdZlCrJr8vi5w5z9XyK1fWQvfSghuJtbyB61WmB7SR4'
    'bSlQ+abzGVrkR3sFGDyCKc4ZhKhIlhHV5U9h8qbzIVtQyeo5ePoh2RUMJxvlOpFBGU+xyCKk951l/wjb8tGsw02s'
    'FYLrD/l9QWQarWXv9y0LT8OkSgVFn/luhTwR1gW9QAIoBeLzVEMsGkrP52B0NRwnCOGOypn3FfjXGexMx7Jj8Wk2'
    'yIxVjWaqKWebbw=='
)


def make_agency(company_id='comp_demo'):
    return GHLOAuthToken.objects.create(
        company_id=company_id,
        location_id='agency-loc',
        name='SurPro Labs AI',
        is_agency=True,
        access_token='token',
        refresh_token='refresh',
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )


def seal(payload: dict, secret: str = 'test-sso-secret', salt: bytes = b'12345678') -> str:
    """Build a blob the way GHL does, for cases no fixed vector covers."""
    key, iv = sso._derive_key_and_iv(secret.encode(), salt)
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(
        b'Salted__' + salt + encryptor.update(padded) + encryptor.finalize()
    ).decode()


# --- decryption -------------------------------------------------------------------------


def test_opens_a_real_openssl_blob():
    make_agency()

    context = sso.context_from_blob(VECTOR_AGENCY)

    assert context.user_id == 'user-1'
    assert context.user_name == 'Jerry Miller'
    assert context.email == 'jerry@example.test'
    assert context.company_id == 'comp_demo'
    assert context.is_agency_owner is True


def test_agency_scope_when_there_is_no_active_location():
    make_agency()

    assert sso.context_from_blob(VECTOR_AGENCY).scope == sso.AGENCY


def test_location_scope_comes_from_active_location_not_type():
    """GHL reports `type: "agency"` even inside a sub-account, so only activeLocation decides."""
    make_agency()

    context = sso.context_from_blob(VECTOR_LOCATION)

    assert context.active_location == 'loc-cool-breeze'
    assert context.scope == sso.LOCATION


def test_reads_unknown_fields_without_complaining():
    make_agency()

    context = sso.context_from_blob(
        seal({'companyId': 'comp_demo', 'userName': 'Jerry', 'somethingNew': {'a': 1}})
    )

    assert context.user_name == 'Jerry'


@pytest.mark.parametrize(
    'blob',
    [
        '',
        'not-base64!!',
        base64.b64encode(b'NoSaltHere' + b'x' * 32).decode(),
        base64.b64encode(b'Salted__').decode(),
        'U2FsdGVkX1' + 'A' * 40,
    ],
)
def test_rejects_a_blob_it_cannot_open(blob):
    make_agency()

    with pytest.raises(sso.SSOError):
        sso.context_from_blob(blob)


def test_rejects_a_blob_sealed_with_another_secret():
    make_agency()

    with pytest.raises(sso.SSOError):
        sso.context_from_blob(seal({'companyId': 'comp_demo'}, secret='someone-elses-secret'))


def test_a_rejected_blob_never_says_why():
    """One detail for every failure: naming the cause helps whoever is guessing."""
    make_agency()

    with pytest.raises(sso.SSOError) as wrong_secret:
        sso.context_from_blob(seal({'companyId': 'comp_demo'}, secret='nope'))
    with pytest.raises(sso.SSOError) as not_base64:
        sso.context_from_blob('not-base64!!')

    assert str(wrong_secret.value.detail) == str(not_base64.value.detail)


# --- agency check -----------------------------------------------------------------------


def test_rejects_a_session_from_another_agency():
    make_agency(company_id='comp_jerry')

    with pytest.raises(sso.WrongAgencyError):
        sso.context_from_blob(VECTOR_AGENCY)


def test_rejects_every_session_when_no_agency_is_connected():
    with pytest.raises(sso.WrongAgencyError):
        sso.context_from_blob(VECTOR_AGENCY)


# --- the endpoint -----------------------------------------------------------------------


def test_session_endpoint_returns_the_viewer(client):
    make_agency()

    response = client.post(
        reverse('ghl:sso-session'),
        data={'key': VECTOR_AGENCY},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert response.json() == {
        'user_name': 'Jerry Miller',
        'email': 'jerry@example.test',
        'role': 'admin',
        'is_agency_owner': True,
        'company_id': 'comp_demo',
        'active_location': None,
        'scope': 'agency',
    }


def test_session_endpoint_reports_the_active_location(client):
    make_agency()

    response = client.post(
        reverse('ghl:sso-session'),
        data={'key': VECTOR_LOCATION},
        content_type='application/json',
    )

    assert response.json()['scope'] == 'location'
    assert response.json()['active_location'] == 'loc-cool-breeze'


def test_session_endpoint_401s_without_a_usable_blob(client):
    make_agency()

    response = client.post(reverse('ghl:sso-session'), data={}, content_type='application/json')

    assert response.status_code == 401


def test_session_endpoint_403s_for_another_agency(client):
    make_agency(company_id='comp_jerry')

    response = client.post(
        reverse('ghl:sso-session'),
        data={'key': VECTOR_AGENCY},
        content_type='application/json',
    )

    assert response.status_code == 403
