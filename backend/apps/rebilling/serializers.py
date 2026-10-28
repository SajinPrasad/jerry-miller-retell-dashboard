"""Input validation for the one thing an agency admin can write: a client's mapping.

Everything else behind `apps/rebilling/views.py` is a read computed in `services.py`, so this is
the only serializer in the app.
"""

import re

from rest_framework import serializers

from .services import normalise_number

# Twilio's own subaccount SID shape: "AC" followed by 32 hex characters.
TWILIO_SID_RE = re.compile(r'^AC[0-9a-fA-F]{32}$')


class ClientMappingSerializer(serializers.Serializer):
    """What `PUT /admin/clients/<location_id>/mapping/` accepts.

    Only the three fields the admin page edits: the Twilio subaccount, the Retell agent IDs, and
    the backup phone numbers. Rates have their own history and their own change rules, and
    `active`/`billing_mode` are not asked for here, so neither is touched by this serializer.
    """

    twilio_subaccount_sid = serializers.CharField(required=False, allow_blank=True, max_length=64)
    retell_agent_ids = serializers.ListField(
        child=serializers.CharField(max_length=255), required=False
    )
    phone_numbers = serializers.ListField(
        child=serializers.CharField(max_length=32), required=False
    )

    def validate_twilio_subaccount_sid(self, value: str) -> str:
        value = value.strip()
        # Blank clears Twilio billing for this client, same as an unset mapping does today.
        if value and not TWILIO_SID_RE.match(value):
            raise serializers.ValidationError(
                'A Twilio subaccount SID looks like "AC" followed by 32 hex characters.'
            )
        return value

    def validate_retell_agent_ids(self, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise serializers.ValidationError('Agent IDs cannot be blank.')
        return _deduplicate(cleaned)

    def validate_phone_numbers(self, value: list[str]) -> list[str]:
        cleaned = []
        for number in value:
            number = number.strip()
            digits = normalise_number(number)
            if not 7 <= len(digits) <= 15:
                raise serializers.ValidationError(f'"{number}" is not a usable phone number.')
            cleaned.append(number)
        return _deduplicate(cleaned)


def _deduplicate(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
