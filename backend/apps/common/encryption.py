"""Field-level encryption for secrets (OAuth tokens) at rest.

`EncryptedTextField` stores Fernet ciphertext and exposes plaintext in Python. The key is
`settings.FIELD_ENCRYPTION_KEY` (urlsafe base64, 32 bytes). Encryption is non-deterministic, so
never filter on an encrypted column.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured('FIELD_ENCRYPTION_KEY must be set to use EncryptedTextField.')
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedTextField(models.TextField):
    def get_prep_value(self, value):
        if value is None:
            return value
        return _fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            # Fail loudly: a wrong/rotated key must not silently yield garbage tokens.
            raise ImproperlyConfigured(
                'Could not decrypt a stored value; check FIELD_ENCRYPTION_KEY.'
            ) from exc
