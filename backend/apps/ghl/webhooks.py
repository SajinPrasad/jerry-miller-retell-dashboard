"""Checking that a webhook really came from GoHighLevel.

GHL signs the raw request body with its own private key. The matching public key is published
in its docs and is the same for every marketplace app, so `GHL_WEBHOOK_PUBLIC_KEY` is not a
secret — it only needs setting if GHL ever rotates it.

This lives on its own rather than inside a view because two apps now receive GHL webhooks: the
`LocationCreate` event that discovers a new sub-account, and the invoice events that say a bill
was paid. One of them having its own copy of the verification is how the two drift apart.
"""

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from django.conf import settings


def verify_signature(raw_body: bytes, signature: str) -> bool:
    """True if `signature` is GHL's signature over exactly these bytes."""
    if not signature:
        return False
    try:
        public_key = load_pem_public_key(settings.GHL_WEBHOOK_PUBLIC_KEY.encode())
        public_key.verify(base64.b64decode(signature), raw_body)
        return True
    except (InvalidSignature, ValueError, binascii.Error):
        return False
