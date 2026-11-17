"""Pure conversions: a GHL payload into the HCP shapes Phase 4 pushes. No database, no HTTP."""

from datetime import datetime, timezone

from apps.hcp.services import reverse_mapping


class DummyLineItem:
    """Stands in for a `JobLineItem` row - only the fields `line_item_changes` reads."""

    def __init__(self, **overrides):
        from decimal import Decimal

        self.name = 'Filter'
        self.description = ''
        self.quantity = Decimal('1')
        self.unit_price = None
        self.unit_cost = None
        self.taxable = False
        self.item_type = ''
        for key, value in overrides.items():
            setattr(self, key, value)


# --- properties_dict / cents_of ----------------------------------------------------------


def test_properties_dict_reads_the_typed_value_fields():
    properties = [
        {'key': 'name', 'valueString': 'Filter'},
        {'key': 'quantity', 'valueNumber': 2},
        {'key': 'unit_price', 'valueCurrency': {'value': 12.5, 'currency': 'default'}},
    ]
    assert reverse_mapping.properties_dict(properties) == {
        'name': 'Filter',
        'quantity': 2,
        'unit_price': {'value': 12.5, 'currency': 'default'},
    }


def test_properties_dict_ignores_entries_with_no_key_or_no_typed_value():
    assert reverse_mapping.properties_dict([{'valueString': 'orphan'}, {'key': 'x'}]) == {}


def test_properties_dict_of_a_non_list_is_empty():
    assert reverse_mapping.properties_dict(None) == {}
    assert reverse_mapping.properties_dict('nope') == {}


def test_cents_of_a_monetary_value_dict():
    assert reverse_mapping.cents_of({'value': 12.5, 'currency': 'default'}) == 1250


def test_cents_of_a_bare_number():
    assert reverse_mapping.cents_of(12.5) == 1250


def test_cents_of_none_or_unparseable_is_none():
    assert reverse_mapping.cents_of(None) is None
    assert reverse_mapping.cents_of('not a number') is None


# --- contacts ------------------------------------------------------------------------------


def test_contact_from_ghl_reads_the_primary_phone_by_its_label():
    fields = reverse_mapping.contact_from_ghl(
        {'firstName': 'Jane', 'phone': '555-1', 'phoneLabel': 'Home'}
    )
    assert fields['home_number'] == '555-1'
    assert fields['mobile_number'] == ''


def test_contact_from_ghl_defaults_an_unlabeled_phone_to_mobile():
    fields = reverse_mapping.contact_from_ghl({'phone': '555-1'})
    assert fields['mobile_number'] == '555-1'


def test_contact_from_ghl_reads_additional_phones():
    fields = reverse_mapping.contact_from_ghl(
        {
            'phone': '555-1',
            'phoneLabel': 'Mobile',
            'additionalPhones': [{'phoneLabel': 'Work', 'phone': '555-2'}],
        }
    )
    assert fields['mobile_number'] == '555-1'
    assert fields['work_number'] == '555-2'


def test_contact_is_creatable_on_email_alone():
    assert reverse_mapping.contact_is_creatable(
        {
            'email': 'a@b.test',
            'first_name': '',
            'last_name': '',
            'company_name': '',
            'mobile_number': '',
            'home_number': '',
            'work_number': '',
        }
    )


def test_contact_is_creatable_requires_a_phone_alongside_a_company_name():
    fields = {
        'email': '',
        'first_name': '',
        'last_name': '',
        'company_name': 'Acme',
        'mobile_number': '',
        'home_number': '',
        'work_number': '',
    }
    assert not reverse_mapping.contact_is_creatable(fields)
    assert reverse_mapping.contact_is_creatable({**fields, 'mobile_number': '555-1'})


def test_contact_is_creatable_is_false_for_a_blank_contact():
    fields = {
        k: ''
        for k in (
            'email',
            'first_name',
            'last_name',
            'company_name',
            'mobile_number',
            'home_number',
            'work_number',
        )
    }
    assert not reverse_mapping.contact_is_creatable(fields)


def test_customer_create_payload_includes_the_address():
    contact = {'address1': '12 Elm St', 'city': 'Austin', 'state': 'TX', 'postalCode': '78701'}
    fields = reverse_mapping.contact_from_ghl({**contact, 'firstName': 'Jane'})
    payload = reverse_mapping.customer_create_payload(contact, fields, '')
    assert payload['addresses'] == [
        {'street': '12 Elm St', 'city': 'Austin', 'state': 'TX', 'zip': '78701'}
    ]


def test_customer_create_payload_omits_an_address_ghl_has_nothing_for():
    fields = reverse_mapping.contact_from_ghl({'firstName': 'Jane'})
    payload = reverse_mapping.customer_create_payload({}, fields, '')
    assert 'addresses' not in payload


def test_customer_create_payload_carries_the_note():
    fields = reverse_mapping.contact_from_ghl({'firstName': 'Jane'})
    payload = reverse_mapping.customer_create_payload({}, fields, 'Gate code 1234')
    assert payload['notes'] == 'Gate code 1234'


def test_customer_update_payload_only_renames_what_is_given():
    payload = reverse_mapping.customer_update_payload({'first_name': 'Janet'})
    assert payload == {'first_name': 'Janet'}


def test_customer_update_payload_never_carries_an_address():
    payload = reverse_mapping.customer_update_payload(
        {'first_name': 'Janet', 'email': 'j@example.test'}
    )
    assert 'addresses' not in payload
    assert 'street' not in payload


# --- leads / estimates / jobs --------------------------------------------------------------


def test_lead_create_payload_needs_only_the_customer_id():
    assert reverse_mapping.lead_create_payload({}, 'hcp-cust-1') == {'customer_id': 'hcp-cust-1'}


def test_lead_create_payload_carries_a_lead_source_when_given():
    payload = reverse_mapping.lead_create_payload({'lead_source': 'Referral'}, 'hcp-cust-1')
    assert payload == {'customer_id': 'hcp-cust-1', 'lead_source': 'Referral'}


def test_estimate_create_payload_needs_only_the_customer_id():
    assert reverse_mapping.estimate_create_payload({}, 'hcp-cust-1') == {
        'customer_id': 'hcp-cust-1'
    }


def test_job_create_payload_needs_only_the_customer_id():
    assert reverse_mapping.job_create_payload({}, 'hcp-cust-1') == {'customer_id': 'hcp-cust-1'}


# --- job line items --------------------------------------------------------------------------


def test_line_item_payload_is_none_without_a_name():
    assert reverse_mapping.line_item_payload({}) is None


def test_line_item_payload_converts_money_to_cents():
    payload = reverse_mapping.line_item_payload(
        {
            'name': 'Filter',
            'unit_price': {'value': 12.5, 'currency': 'default'},
            'unit_cost': {'value': 4.0, 'currency': 'default'},
            'quantity': 2,
        }
    )
    assert payload['unit_price'] == 1250
    assert payload['unit_cost'] == 400
    assert payload['quantity'] == 2


def test_line_item_payload_converts_the_taxable_dropdown():
    assert (
        reverse_mapping.line_item_payload({'name': 'Filter', 'taxable': 'Yes'})['taxable'] is True
    )
    assert (
        reverse_mapping.line_item_payload({'name': 'Filter', 'taxable': 'No'})['taxable'] is False
    )


def test_line_item_payload_reverses_the_item_type_label():
    payload = reverse_mapping.line_item_payload({'name': 'Filter', 'item_type': 'Fixed Discount'})
    assert payload['kind'] == 'fixed_discount'


def test_line_item_payload_drops_an_unconfigured_item_type():
    payload = reverse_mapping.line_item_payload({'name': 'Filter', 'item_type': 'Something Else'})
    assert 'kind' not in payload


def test_line_item_changes_is_empty_when_nothing_differs():
    existing = DummyLineItem(name='Filter')
    candidate = {'name': 'Filter', 'description': ''}
    assert reverse_mapping.line_item_changes(candidate, existing) == {}


def test_line_item_changes_flags_a_changed_name():
    existing = DummyLineItem(name='Filter')
    candidate = {'name': 'New Filter', 'description': ''}
    assert reverse_mapping.line_item_changes(candidate, existing) == {'name': 'New Filter'}


def test_line_item_changes_compares_price_in_the_same_cents_unit():
    from decimal import Decimal

    existing = DummyLineItem(name='Filter', unit_price=Decimal('12.50'))
    candidate = {'name': 'Filter', 'description': '', 'unit_price': 1250}
    assert reverse_mapping.line_item_changes(candidate, existing) == {}

    candidate_changed = {'name': 'Filter', 'description': '', 'unit_price': 1500}
    assert reverse_mapping.line_item_changes(candidate_changed, existing) == {'unit_price': 1500}


# --- appointments ---------------------------------------------------------------------------


def test_appointment_times_parses_all_three_timestamps():
    starts_at, ends_at, updated_at = reverse_mapping.appointment_times(
        {
            'startTime': '2026-11-20T15:00:00+00:00',
            'endTime': '2026-11-20T16:00:00+00:00',
            'dateUpdated': '2026-11-17T12:00:00+00:00',
        }
    )
    assert starts_at == datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    assert ends_at == datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
    assert updated_at == datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def test_appointment_times_is_none_for_missing_fields():
    assert reverse_mapping.appointment_times({}) == (None, None, None)
