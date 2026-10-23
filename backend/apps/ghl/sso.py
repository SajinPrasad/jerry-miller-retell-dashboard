"""The GHL session behind the embedded admin page.

The admin page is a GHL Custom Page: it runs in an iframe, asks the parent window for the
session with a `REQUEST_USER_DATA` postMessage, and gets back an encrypted blob. Only this
server can open it, because it is sealed with the app's Shared Secret (`GHL_APP_SSO_KEY`).
That makes the blob the page's credential, so it is sent on every admin request and opened
here rather than exchanged for a token of our own.

The blob is OpenSSL-compatible AES-256-CBC: the format CryptoJS produces when it is given a
passphrase instead of a key, which is what GHL's own docs and marketplace template use. So the
key and IV come from OpenSSL's legacy EVP_BytesToKey with MD5, not from the secret directly.

Docs: https://marketplace.gohighlevel.com/docs/other/user-context-marketplace-apps
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import APIException

from .models import GHLOAuthToken

# The header the admin page sends the blob in. Never logged: it is a credential.
SESSION_HEADER = 'X-GHL-Session'

# OpenSSL's envelope: b'Salted__' then an 8-byte salt, then the ciphertext.
SALT_HEADER = b'Salted__'
HEADER_LENGTH = 16
KEY_LENGTH = 32
IV_LENGTH = 16

# What the page is allowed to see, decided by `SSOContext.scope`.
AGENCY = 'agency'
LOCATION = 'location'


class SSOError(APIException):
    """The session is missing, or it could not be opened.

    An `APIException`, so DRF renders it without every view repeating a try/except. The detail
    is deliberately the same for every cause: telling a caller which half of a guess was right
    is free help for guessing the rest.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = 'Open this page from inside GoHighLevel.'
    default_code = 'sso_invalid'


class WrongAgencyError(SSOError):
    """The session opened, but it belongs to someone other than the agency that installed us."""

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = 'This session does not belong to the connected agency.'
    default_code = 'sso_wrong_agency'


@dataclass(frozen=True)
class SSOContext:
    """Who GHL says is looking at the page."""

    user_id: str
    user_name: str
    email: str
    role: str
    company_id: str
    active_location: str
    is_agency_owner: bool

    @property
    def scope(self) -> str:
        """`location` when the page was opened inside a sub-account, else `agency`.

        Decided on `activeLocation` alone. The payload's own `type` is not usable for this:
        GHL's documented location-context example still reports `type: "agency"`, and reading
        it would show one client every other client's numbers.
        """
        return LOCATION if self.active_location else AGENCY


def context_from_request(request) -> SSOContext:
    """The verified session behind an admin request. Raises if there isn't one."""
    return context_from_blob(request.headers.get(SESSION_HEADER) or '')


def context_from_blob(blob: str) -> SSOContext:
    """Open a session blob and check it belongs to the agency that installed the app."""
    payload = _decrypt(blob)
    context = SSOContext(
        user_id=payload.get('userId') or '',
        user_name=payload.get('userName') or '',
        email=payload.get('email') or '',
        role=payload.get('role') or '',
        company_id=payload.get('companyId') or '',
        # Read the fields we need and ignore the rest: GHL adds to this payload over time.
        active_location=payload.get('activeLocation') or '',
        is_agency_owner=bool(payload.get('isAgencyOwner')),
    )

    agency = GHLOAuthToken.objects.filter(is_agency=True, is_active=True).first()
    if agency is None or context.company_id != agency.company_id:
        # GHL sealed this `companyId`, so unlike the install check — which only rejects a
        # sub-account install — this really does pin the session to Jerry's agency.
        raise WrongAgencyError()
    return context


def _decrypt(blob: str) -> dict:
    if not blob:
        raise SSOError()
    if not settings.GHL_APP_SSO_KEY:
        # A deployment with no Shared Secret can't authenticate anyone. Fail shut.
        raise SSOError()

    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SSOError() from exc

    if len(raw) <= HEADER_LENGTH or not raw.startswith(SALT_HEADER):
        raise SSOError()

    salt = raw[len(SALT_HEADER) : HEADER_LENGTH]
    key, iv = _derive_key_and_iv(settings.GHL_APP_SSO_KEY.encode(), salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        padded = decryptor.update(raw[HEADER_LENGTH:]) + decryptor.finalize()
        payload = json.loads(unpadder.update(padded) + unpadder.finalize())
    except (ValueError, TypeError, InvalidKey) as exc:
        # A wrong secret lands here, as a padding error or as unreadable JSON.
        raise SSOError() from exc

    if not isinstance(payload, dict):
        raise SSOError()
    return payload


def _derive_key_and_iv(secret: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """OpenSSL's EVP_BytesToKey with MD5, which is what CryptoJS uses for a passphrase.

    Hash `previous_digest + secret + salt` until there are 48 bytes: the first 32 are the key
    and the next 16 the IV. MD5 is OpenSSL's choice of hash for this derivation, not ours, so
    `usedforsecurity=False` keeps it working where MD5 is otherwise restricted.
    """
    material = b''
    digest = b''
    while len(material) < KEY_LENGTH + IV_LENGTH:
        digest = hashlib.md5(digest + secret + salt, usedforsecurity=False).digest()
        material += digest
    return material[:KEY_LENGTH], material[KEY_LENGTH : KEY_LENGTH + IV_LENGTH]
