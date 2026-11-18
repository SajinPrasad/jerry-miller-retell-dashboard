"""The HCP → GHL field mapping, on fixtures alone.

No database and no HTTP: this is the layer most likely to be subtly wrong (two external field
vocabularies, money in cents, blank-vs-omitted) and the one that has to be cheap to check.
"""

from decimal import Decimal

from apps.hcp.constants import (
    HCP_CUSTOMER_ID_FIELD,
    HCP_CUSTOMER_NOTE_FIELD,
    JOB_PROPERTIES,
)
from apps.hcp.services import mapping
from apps.hcp.tests import fixtures

FIELD_IDS = {HCP_CUSTOMER_ID_FIELD: 'cf-id', HCP_CUSTOMER_NOTE_FIELD: 'cf-note'}


# --- the money / blank / text conventions ------------------------------------------------


def test_money_converts_cents_to_a_ghl_monetary_value():
    assert mapping.money(45000) == {'value': 450.0, 'currency': 'default'}


def test_money_keeps_zero():
    """A job with nothing left to pay is exactly when `due_amount` must be written, not skipped."""
    assert mapping.money(0) == {'value': 0.0, 'currency': 'default'}


def test_money_is_none_for_nothing_and_for_nonsense():
    assert mapping.money(None) is None
    assert mapping.money('lots') is None
    assert mapping.money(True) is None


def test_present_drops_blanks_but_keeps_zero():
    kept = mapping.present({'a': '', 'b': None, 'c': [], 'd': 0, 'e': 'x'})

    assert kept == {'d': 0, 'e': 'x'}


def test_text_trims():
    """HCP does not trim user input and GHL rejects a padded email with a 422."""
    assert mapping.text('  jane@example.test ') == 'jane@example.test'


def test_text_of_nothing_is_empty():
    assert mapping.text(None) == ''
    assert mapping.text(True) == ''


# --- contacts -----------------------------------------------------------------------------


def test_contact_payload_maps_the_customer():
    payload = mapping.contact_payload(fixtures.customer(), FIELD_IDS)

    assert payload['firstName'] == 'Jane'
    assert payload['lastName'] == 'Doe'
    assert payload['email'] == 'jane@example.test'
    assert payload['phone'] == '+15551230001'
    assert payload['companyName'] == 'Doe Residence'
    assert payload['source'] == 'Google'
    assert payload['tags'] == ['VIP']


def test_contact_payload_takes_the_first_address():
    payload = mapping.contact_payload(fixtures.customer(), FIELD_IDS)

    assert payload['address1'] == '12 Elm St'
    assert payload['city'] == 'Austin'
    assert payload['state'] == 'TX'
    assert payload['postalCode'] == '78701'


def test_contact_payload_carries_the_hcp_id_custom_field():
    payload = mapping.contact_payload(fixtures.customer(), FIELD_IDS)

    assert {'id': 'cf-id', 'field_value': 'hcp-customer-1'} in payload['customFields']
    assert {'id': 'cf-note', 'field_value': 'Gate code 1234'} in payload['customFields']


def test_a_custom_field_the_snapshot_lacks_is_skipped_not_sent_with_a_null_id():
    payload = mapping.contact_payload(fixtures.customer(), {HCP_CUSTOMER_ID_FIELD: 'cf-id'})

    assert payload['customFields'] == [{'id': 'cf-id', 'field_value': 'hcp-customer-1'}]


def test_the_home_number_is_used_when_there_is_no_mobile():
    payload = mapping.contact_payload(fixtures.customer(mobile_number=None), FIELD_IDS)

    assert payload['phone'] == '+15551230002'


def test_a_customer_with_nothing_to_call_it_is_not_identifiable():
    empty = fixtures.customer(
        first_name=None, last_name=None, email=None, mobile_number=None, home_number=None
    )

    assert mapping.contact_is_identifiable(mapping.contact_payload(empty, FIELD_IDS)) is False


def test_a_customer_with_only_a_name_is_identifiable():
    named = fixtures.customer(email=None, mobile_number=None, home_number=None)

    assert mapping.contact_is_identifiable(mapping.contact_payload(named, FIELD_IDS)) is True


# --- leads --------------------------------------------------------------------------------


def test_lead_properties_map_the_lead():
    properties = mapping.lead_properties(fixtures.lead(), 'ghl-contact-1')

    assert properties['contact_id'] == 'ghl-contact-1'
    assert properties['external_lead_id'] == 'hcp-lead-1'
    assert properties['lead_number'] == '41'
    assert properties['lead_status'] == 'new'
    assert properties['lead_source'] == 'Website'
    assert properties['lead_value'] == {'value': 450.0, 'currency': 'default'}


def test_lead_properties_flatten_tags_to_one_line():
    assert mapping.lead_properties(fixtures.lead(), 'c')['tags'] == 'hvac, urgent'


def test_lead_properties_map_the_address():
    properties = mapping.lead_properties(fixtures.lead(), 'c')

    assert properties['street'] == '12 Elm St'
    assert properties['unit'] == 'Apt 4'
    assert properties['zip'] == '78701'


def test_lead_properties_omit_what_hcp_did_not_send():
    """A key left out leaves GHL's value alone; sending it empty would clear it."""
    properties = mapping.lead_properties(fixtures.lead(tags=[], estimated_value=None), 'c')

    assert 'tags' not in properties
    assert 'lead_value' not in properties


def test_a_lead_falls_back_to_the_customers_source():
    properties = mapping.lead_properties(fixtures.lead(lead_source=None), 'c')

    assert properties['lead_source'] == 'Google'


# --- estimates ------------------------------------------------------------------------------


def test_estimate_properties_map_the_estimate():
    properties = mapping.estimate_properties(fixtures.estimate(), 'ghl-contact-1')

    assert properties['external_estimate_id'] == 'hcp-estimate-1'
    assert properties['estimate_number'] == '1042'
    assert properties['estimate_status'] == 'estimate_sent'
    assert properties['estimate_sent_at'] == '2026-03-01T15:00:00Z'


def test_estimate_properties_map_the_schedule_and_work_timestamps():
    properties = mapping.estimate_properties(fixtures.estimate(), 'c')

    assert properties['scheduled_start_at'] == '2026-03-04T15:00:00Z'
    assert properties['scheduled_end_at'] == '2026-03-04T17:00:00Z'
    assert properties['arrival_window_minutes'] == 60
    assert properties['technician_en_route_at'] == '2026-03-04T14:30:00Z'
    assert properties['work_completed_at'] == '2026-03-04T16:40:00Z'


def test_an_unapproved_estimate_has_no_approved_fields():
    properties = mapping.estimate_properties(fixtures.estimate(), 'c')

    assert 'approved_total_amount' not in properties
    assert 'approved_option_name' not in properties


def test_the_approved_option_fills_the_approved_fields():
    approved = fixtures.estimate(
        options=[
            fixtures.option('o-1', approval_status='pro forma'),
            fixtures.option('o-2', approval_status='approved', name='Better', total_amount=72000),
        ]
    )

    properties = mapping.estimate_properties(approved, 'c')

    assert properties['approved_option_name'] == 'Better'
    assert properties['approved_total_amount'] == {'value': 720.0, 'currency': 'default'}


def test_all_options_totals_are_one_line_each():
    """A Monetary field holds one number, so good/better/best goes in Multi Line Text."""
    several = fixtures.estimate(
        options=[
            fixtures.option('o-1', name='Good', total_amount=45000),
            fixtures.option('o-2', name='Better', total_amount=72000),
        ]
    )

    assert mapping.estimate_properties(several, 'c')['all_options_total_amount'] == (
        'Good: $450.00\nBetter: $720.00'
    )


def test_all_options_names_and_numbers_are_listed():
    properties = mapping.estimate_properties(fixtures.estimate(), 'c')

    assert properties['all_options_names'] == 'Good'
    assert properties['all_options_numbers'] == '1'


def test_option_details_carry_the_approval_status():
    properties = mapping.estimate_properties(fixtures.estimate(), 'c')

    assert properties['all_options_details'] == 'Good (pro forma) $450.00'


def test_line_items_render_as_name_and_dollar_amount():
    assert mapping.estimate_properties(fixtures.estimate(), 'c')['all_line_items'] == (
        'Condenser - $400.00'
    )


def test_an_estimate_with_no_options_has_no_option_fields():
    properties = mapping.estimate_properties(fixtures.estimate(options=[]), 'c')

    assert 'all_options_names' not in properties
    assert 'approved_total_amount' not in properties


# --- jobs --------------------------------------------------------------------------------


def test_job_properties_map_the_job():
    properties = mapping.job_properties(fixtures.job(), 'ghl-contact-1')

    assert properties['hcp_job_id'] == 'hcp-job-1'
    assert properties['job_number'] == '5567'
    assert properties['invoice_number'] == '5567'
    assert properties['job_status'] == 'scheduled'
    assert properties['job_description'] == 'Install replacement condenser'


def test_job_money_fields_convert_from_cents():
    properties = mapping.job_properties(fixtures.job(), 'c')

    assert properties['job_amount'] == {'value': 4500.0, 'currency': 'default'}
    assert properties['due_amount'] == {'value': 1500.0, 'currency': 'default'}


def test_a_fully_paid_job_still_writes_a_zero_due_amount():
    properties = mapping.job_properties(fixtures.job(outstanding_balance=0), 'c')

    assert properties['due_amount'] == {'value': 0.0, 'currency': 'default'}


def test_the_job_type_is_read_off_the_job_fields_object():
    assert mapping.job_properties(fixtures.job(), 'c')['job_type'] == 'Installation'


def test_a_plain_string_job_type_still_maps():
    """Older HCP payloads send it flat rather than nested."""
    flat = fixtures.job(job_fields={}, job_type='Repair')

    assert mapping.job_properties(flat, 'c')['job_type'] == 'Repair'


def test_street_line_2_uses_ghls_double_underscore_key():
    """The field is named `Street Line 2 / Unit`, so GHL derived `street_line_2__unit`.

    Asserted on what `job_properties` actually returns: the constant alone passed while the
    mapper sent the bare `street_line_2`, which GHL rejected with a 400.
    """
    properties = mapping.job_properties(fixtures.job(), 'c')

    assert properties['street_line_2__unit'] == 'Apt 4'
    assert 'street_line_2' not in properties


def test_every_job_property_key_is_a_snapshot_key():
    """No key `job_properties` returns may be one GHL does not know."""
    properties = mapping.job_properties(fixtures.job(), 'c')

    assert set(properties) <= set(JOB_PROPERTIES.values())


def test_job_notes_are_joined_into_one_block():
    job = fixtures.job(notes=[{'content': 'Park in the alley'}, {'content': 'Dog on site'}])

    assert mapping.job_properties(job, 'c')['notes'] == 'Park in the alley\nDog on site'


def test_unset_work_timestamps_are_omitted():
    properties = mapping.job_properties(fixtures.job(), 'c')

    assert 'work_completed_at' not in properties
    assert 'technician_en_route_at' not in properties


def test_invoices_render_one_line_each():
    assert mapping.job_properties(fixtures.job(), 'c')['invoices'] == 'INV-1 - $4500.00 - sent'


# --- job line items --------------------------------------------------------------------------


def test_line_item_properties_map_the_item():
    properties = mapping.line_item_properties(fixtures.line_item(), 'ghl-job-1')

    assert properties['job_record_id'] == 'ghl-job-1'
    assert properties['hcp_line_item_id'] == 'hcp-line-1'
    assert properties['name'] == 'Condenser unit'
    assert properties['quantity'] == 1
    assert properties['unit_price'] == {'value': 400.0, 'currency': 'default'}
    assert properties['total_price'] == {'value': 400.0, 'currency': 'default'}


def test_taxable_becomes_the_dropdown_word():
    assert mapping.line_item_properties(fixtures.line_item(), 'j')['taxable'] == 'Yes'
    assert mapping.line_item_properties(fixtures.line_item(taxable=False), 'j')['taxable'] == 'No'


def test_item_type_maps_hcps_value_to_the_configured_option():
    assert mapping.line_item_properties(fixtures.line_item(), 'j')['item_type'] == 'Materials'
    assert (
        mapping.line_item_properties(fixtures.line_item(kind='percent_discount'), 'j')['item_type']
        == 'Percent Discount'
    )


def test_an_unknown_item_type_is_left_off_rather_than_guessed():
    """GHL rejects a dropdown value that is not configured, which would cost the whole item."""
    properties = mapping.line_item_properties(fixtures.line_item(kind='something_new'), 'j')

    assert 'item_type' not in properties


def test_line_item_fields_map_to_the_local_columns():
    fields = mapping.line_item_fields(fixtures.line_item())

    assert fields['name'] == 'Condenser unit'
    assert fields['unit_price'] == Decimal('400.0')
    assert fields['unit_cost'] == Decimal('280.0')
    assert fields['quantity'] == Decimal('1')
    assert fields['taxable'] is True


# --- appointments ----------------------------------------------------------------------------


def test_appointment_payload_is_booked_confirmed():
    """HCP has already booked it: this is a mirror of a commitment, not a request for a slot."""
    payload = mapping.appointment_payload(
        calendar_id='cal-job',
        contact_id='ghl-contact-1',
        assigned_user_id='user-1',
        title='Job #5567',
        starts_at='2026-03-10T15:00:00Z',
        ends_at='2026-03-10T19:00:00Z',
        address='12 Elm St, Austin, TX',
    )

    assert payload['appointmentStatus'] == 'confirmed'
    assert payload['calendarId'] == 'cal-job'
    assert payload['assignedUserId'] == 'user-1'
    assert payload['startTime'] == '2026-03-10T15:00:00Z'


def test_an_appointment_with_no_end_time_omits_it():
    payload = mapping.appointment_payload(
        calendar_id='cal-job',
        contact_id='c',
        assigned_user_id='u',
        title='Job',
        starts_at='2026-03-10T15:00:00Z',
        ends_at='',
        address='',
    )

    assert 'endTime' not in payload
    assert 'address' not in payload


def test_address_line_joins_the_parts_it_has():
    assert mapping.address_line(fixtures.job()['address']) == (
        '12 Elm St, Apt 4, Austin, TX, 78701'
    )
    assert mapping.address_line({'city': 'Austin', 'state': 'TX'}) == 'Austin, TX'
