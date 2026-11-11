"""Keeping credentials out of logs and stored payloads.

Three layers, because no single one is enough:

1. **Known values.** Every secret this app is configured with is replaced wherever it appears
   verbatim - the only reliable catch for a key quoted inside an upstream error message.
2. **Shapes.** Stripe keys, bearer tokens, JWTs, the SSO blob's `Salted__` envelope, `key=value`
   pairs naming a credential, and the OAuth `code=` in a callback URL.
3. **Key names**, for structured data: any dict key that names a credential is blanked whatever
   its value looks like (`scrub`).
"""

import re

from django.conf import settings

REDACTED = '[REDACTED]'

# Settings whose value is a credential. Read at call time so `override_settings` and a changed
# `.env` are both honoured.
SECRET_SETTINGS = (
    'SECRET_KEY',
    'FIELD_ENCRYPTION_KEY',
    'GHL_CLIENT_SECRET',
    'GHL_APP_SSO_KEY',
    'GHL_CARD_WEBHOOK_TOKEN',
    'RETELL_API_KEY',
    'TWILIO_AUTH_TOKEN',
    'STRIPE_SECRET_KEY',
    'STRIPE_WEBHOOK_SECRET',
)

# Used inside free text, where the name is followed by `:` or `=`.
_SENSITIVE_NAME = (
    r'access[_-]?token|refresh[_-]?token|id[_-]?token|token|client[_-]?secret|secret|password|'
    r'authorization|api[_-]?key|auth[_-]?token|x-ghl-session|x-retell-signature|'
    r'stripe-signature|x-card-webhook-token|sso[_-]?key'
)
# Used on structured keys, and anchored: the whole key is a credential name, or ends in one after
# `_` / `-` (`access_token`, `client_secret`). `total_tokens`, `llm_token_usage`,
# `checkout_session` and `off_session` are payload fields, not credentials.
_SENSITIVE_KEY = re.compile(
    r'^(?:(?:[\w-]*[_-])?(?:token|secret|password|passwd|api[_-]?key|apikey|authorization)'
    r'|x-ghl-session|x-retell-signature|stripe-signature|x-card-webhook-token|sso[_-]?key)$',
    re.IGNORECASE,
)

_PATTERNS = [
    (re.compile(r'\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]+'), REDACTED),
    (re.compile(r'\bwhsec_[A-Za-z0-9]+'), REDACTED),
    (re.compile(r'\bkey_[0-9a-f]{20,}'), REDACTED),  # Retell API keys
    (re.compile(r'\bpit-[0-9a-f-]{20,}'), REDACTED),  # GHL private integration tokens
    (re.compile(r'\bSK[0-9a-f]{32}\b'), REDACTED),  # Twilio API keys
    (re.compile(r'Bearer\s+[A-Za-z0-9._~+/=-]+', re.IGNORECASE), f'Bearer {REDACTED}'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*'), REDACTED),  # JWT
    (re.compile(r'U2FsdGVkX1[A-Za-z0-9+/=]+'), REDACTED),  # the SSO blob: base64 of "Salted__"
    (re.compile(r'([?&]code=)[^&\s"\']+'), rf'\1{REDACTED}'),  # OAuth callback code
    (
        re.compile(
            rf'((?:{_SENSITIVE_NAME})["\']?\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^\s,&}}\]]+)',
            re.IGNORECASE,
        ),
        rf'\1"{REDACTED}"',
    ),
]


def redact(text: str) -> str:
    """`text` with every credential this app knows of or can recognise replaced."""
    if not text:
        return text
    for name in SECRET_SETTINGS:
        value = getattr(settings, name, '')
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub(value):
    """A copy of structured data with credential-named keys blanked and strings redacted."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_KEY.match(str(key)) and item else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


# Personal content Retell puts on a call that billing and replay never read: what was said, and
# where the audio lives.
_RETELL_PII = re.compile(
    r'transcript|recording|scrubbed|public_log|knowledge_base_retrieved|call_analysis',
    re.IGNORECASE,
)


def strip_retell_pii(value):
    """A copy of a Retell payload without transcripts, recordings or call analysis."""
    if isinstance(value, dict):
        return {k: strip_retell_pii(v) for k, v in value.items() if not _RETELL_PII.search(str(k))}
    if isinstance(value, list):
        return [strip_retell_pii(item) for item in value]
    return value
