"""Input validation for the three things onboarding writes: the connection, the employee
mappings, and the calendar settings.

Everything else behind `apps/hcp/views.py` is a read assembled in `services/onboarding.py`.
"""

from rest_framework import serializers


class ConnectAccountSerializer(serializers.Serializer):
    """What `POST /api/hcp/accounts/` accepts: the sub-account and its HCP API key.

    `api_key` is `trim_whitespace` by default, which matters more than it looks: the key is
    copied once out of Housecall Pro's UI and pasted, and a trailing newline off the clipboard
    would otherwise be stored and rejected by HCP on every later call.
    """

    location_id = serializers.CharField(max_length=255)
    api_key = serializers.CharField(max_length=1024)


class EmployeeMappingRowSerializer(serializers.Serializer):
    """One row of the mapping table: an HCP employee and the GHL user it assigns work to."""

    hcp_employee_id = serializers.CharField(max_length=255)
    hcp_employee_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=''
    )
    ghl_user_id = serializers.CharField(max_length=255)
    ghl_user_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=''
    )


class EmployeeMappingsSerializer(serializers.Serializer):
    """What `PUT /api/hcp/accounts/<location_id>/employee-mappings/` accepts: the whole table.

    The full set rather than one row, because unmapping someone is expressed by their row no
    longer being sent. An empty list is therefore legal and means "nobody is mapped".

    Both directions are checked for duplicates here rather than left to the database: the
    `unique_hcp_employee_per_location` / `unique_ghl_user_per_location` constraints would raise
    an `IntegrityError` mid-transaction, which tells the admin nothing about *which* row they
    duplicated. These two checks are the same rule, said in time to name it.
    """

    mappings = serializers.ListField(child=EmployeeMappingRowSerializer(), allow_empty=True)

    def validate_mappings(self, value: list[dict]) -> list[dict]:
        _assert_unique(value, 'hcp_employee_id', 'Housecall Pro employee')
        _assert_unique(value, 'ghl_user_id', 'GoHighLevel user')
        return value


def _assert_unique(rows: list[dict], field: str, label: str) -> None:
    seen = set()
    for row in rows:
        value = row[field]
        if value in seen:
            raise serializers.ValidationError(
                f'Each {label} can only be mapped once ({value} appears twice).'
            )
        seen.add(value)


class CalendarSettingsSerializer(serializers.Serializer):
    """What `PUT /api/hcp/accounts/<location_id>/calendar-settings/` accepts.

    The three `*_user` fields are `HCPEmployeeMapping` primary keys. The per-calendar ones
    override `default_user` for their own calendar and are blank on most accounts (the SOP's
    "leave either blank and that calendar uses the Default User"); they are `allow_null` so a
    saved override can be cleared by sending `null`.

    `default_user` is typed the same way rather than `required=True` on purpose: whether it is
    present is checked in `services/onboarding.py` alongside whether it names a real mapping, so
    the admin gets one answer about the default user rather than two different-shaped 400s
    depending on which half they got wrong.

    Whether those ids name real mappings, whether the calendars exist, and whether every mapped
    user is on both calendar teams are all decided in `services/onboarding.py`: they need the
    database and GoHighLevel, which is past what a serializer should be reaching for.
    """

    estimate_calendar_id = serializers.CharField(max_length=255)
    job_calendar_id = serializers.CharField(max_length=255)
    default_user = serializers.IntegerField(required=False, allow_null=True, default=None)
    estimate_default_user = serializers.IntegerField(required=False, allow_null=True, default=None)
    job_default_user = serializers.IntegerField(required=False, allow_null=True, default=None)
