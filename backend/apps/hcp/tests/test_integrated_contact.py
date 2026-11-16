"""IntegratedContact: the customer row every lead/estimate/job links back to."""

import pytest
from django.db import IntegrityError, transaction

from apps.hcp.models import IntegratedContact

pytestmark = pytest.mark.django_db


def test_contact_rejects_a_second_row_for_the_same_hcp_customer_on_one_location(contact):
    with pytest.raises(IntegrityError), transaction.atomic():
        IntegratedContact.objects.create(
            location_id=contact.location_id,
            ghl_contact_id='a-different-ghl-contact',
            hcp_customer_id=contact.hcp_customer_id,
        )


def test_contact_rejects_a_second_row_for_the_same_ghl_contact_on_one_location(contact):
    with pytest.raises(IntegrityError), transaction.atomic():
        IntegratedContact.objects.create(
            location_id=contact.location_id,
            ghl_contact_id=contact.ghl_contact_id,
            hcp_customer_id='a-different-hcp-customer',
        )


def test_contact_allows_the_same_hcp_customer_id_on_a_different_location(contact):
    """The unique constraint is scoped per location — two locations can each have their own
    HCP customer that happens to carry the same id (HCP ids are global, not location-scoped).
    """
    other = IntegratedContact.objects.create(
        location_id='a-different-location',
        ghl_contact_id='another-ghl-contact',
        hcp_customer_id=contact.hcp_customer_id,
    )

    assert other.pk is not None


def test_contact_str_prefers_the_name_then_falls_back_to_company_then_unknown():
    named = IntegratedContact(first_name='Jane', last_name='Doe')
    company_only = IntegratedContact(company_name='Acme Co')
    bare = IntegratedContact()

    assert 'Jane Doe' in str(named)
    assert 'Acme Co' in str(company_only)
    assert 'Unknown contact' in str(bare)
