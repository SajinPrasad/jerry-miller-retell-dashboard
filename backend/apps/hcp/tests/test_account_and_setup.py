"""HCPAccount, HCPEmployeeMapping and HCPCalendarSettings: account-level setup, before any
customer/lead/estimate/job sync exists.
"""

import pytest
from django.db import IntegrityError, transaction

from apps.hcp.models import HCPAccount, HCPCalendarSettings, HCPEmployeeMapping

pytestmark = pytest.mark.django_db


def test_account_stores_the_api_key_encrypted_at_rest(account):
    """The plaintext round-trips through the ORM; only the stored column is ciphertext."""
    from django.db import connection

    assert account.api_key == 'hcp-key-plaintext'
    with connection.cursor() as cursor:
        cursor.execute('SELECT api_key FROM hcp_account WHERE id = %s', [account.id])
        (raw,) = cursor.fetchone()
    assert raw != 'hcp-key-plaintext'


def test_account_location_id_is_unique(account):
    with pytest.raises(IntegrityError), transaction.atomic():
        HCPAccount.objects.create(
            location_id=account.location_id,
            hcp_company_id='a-different-hcp-company',
            api_key='another-key',
        )


def test_account_hcp_company_id_is_unique(account):
    with pytest.raises(IntegrityError), transaction.atomic():
        HCPAccount.objects.create(
            location_id='a-different-location',
            hcp_company_id=account.hcp_company_id,
            api_key='another-key',
        )


def test_employee_mapping_rejects_a_second_mapping_for_the_same_hcp_employee(account):
    HCPEmployeeMapping.objects.create(
        location_id=account.location_id, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        HCPEmployeeMapping.objects.create(
            location_id=account.location_id,
            hcp_employee_id='hcp-emp-1',
            ghl_user_id='ghl-user-2',
        )


def test_employee_mapping_rejects_a_second_mapping_for_the_same_ghl_user(account):
    HCPEmployeeMapping.objects.create(
        location_id=account.location_id, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        HCPEmployeeMapping.objects.create(
            location_id=account.location_id,
            hcp_employee_id='hcp-emp-2',
            ghl_user_id='ghl-user-1',
        )


def test_employee_mapping_allows_the_same_hcp_employee_id_on_a_different_location(account):
    """The double-unique constraint is scoped per location, not global."""
    HCPEmployeeMapping.objects.create(
        location_id=account.location_id, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )

    other = HCPEmployeeMapping.objects.create(
        location_id='a-different-location', hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )

    assert other.pk is not None


def test_calendar_settings_falls_back_to_the_location_default_user(account):
    default = HCPEmployeeMapping.objects.create(
        location_id=account.location_id, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )
    settings_row = HCPCalendarSettings.objects.create(
        location_id=account.location_id, default_user=default
    )

    assert settings_row.estimate_appointment_default_user == default
    assert settings_row.job_appointment_default_user == default


def test_calendar_settings_per_calendar_override_takes_priority_over_the_default(account):
    default = HCPEmployeeMapping.objects.create(
        location_id=account.location_id, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )
    estimate_lead = HCPEmployeeMapping.objects.create(
        location_id=account.location_id, hcp_employee_id='hcp-emp-2', ghl_user_id='ghl-user-2'
    )
    settings_row = HCPCalendarSettings.objects.create(
        location_id=account.location_id,
        default_user=default,
        estimate_default_user=estimate_lead,
    )

    assert settings_row.estimate_appointment_default_user == estimate_lead
    assert settings_row.job_appointment_default_user == default
