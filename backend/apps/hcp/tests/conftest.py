"""Shared fixtures for the hcp app tests: one connected account and one linked contact."""

import pytest

from apps.hcp.models import HCPAccount, IntegratedContact

LOCATION_ID = 'loc-hcp-1'


@pytest.fixture
def account():
    return HCPAccount.objects.create(
        location_id=LOCATION_ID,
        hcp_company_id='hcp-company-1',
        api_key='hcp-key-plaintext',
        company_name='Cool Breeze HVAC',
    )


@pytest.fixture
def contact(account):
    return IntegratedContact.objects.create(
        location_id=account.location_id,
        ghl_contact_id='ghl-contact-1',
        hcp_customer_id='hcp-customer-1',
        first_name='Jane',
        last_name='Doe',
    )
