"""Turning a Housecall Pro payload into the GoHighLevel shapes that mirror it.

Pure functions: dict in, dict out. No database, no HTTP, no settings. That is deliberate - the
field-by-field mapping is the part of this integration most likely to be wrong and most tedious
to debug through two APIs, so it is the part that has to be testable on a fixture alone.

Three conventions run through all of it:

- **Money arrives in cents.** HCP sends `total_amount: 45000`; GHL's Monetary Value fields want
  dollars, as `{'value': 450.0, 'currency': 'default'}`. Nothing here ever sends a bare number
  to a monetary field.
- **Blank is omitted, not sent.** A key left out of `properties` leaves GHL's value alone; a key
  sent empty clears it. HCP routinely omits fields it has nothing for, and clearing a field in
  GHL because HCP did not mention it this time is data loss.
- **Timestamps pass through as HCP's own ISO-8601 strings.** HCP sends UTC with a `Z`, which is
  what GHL's Date/Time fields accept. The reference project reformats them into the location's
  local timezone, which is a display choice made at write time and loses the offset; keeping
  UTC means the value in GHL means one unambiguous instant.
"""

from typing import Any

from apps.hcp.constants import (
    ITEM_TYPES,
    JOB_PROPERTIES,
    MONETARY_CURRENCY,
    TAXABLE_NO,
    TAXABLE_YES,
)

# How many cents are in the unit GHL's monetary fields are denominated in.
CENTS = 100


# --- small helpers ----------------------------------------------------------------------


def text(value: Any) -> str:
    """A trimmed string, or `''`. HCP does not trim user input, and GHL validates strictly -
    an email arriving as `"someone@example.com "` is rejected with a 422 that takes the whole
    contact with it, and every estimate and job that needed it.
    """
    if value is None or isinstance(value, bool):
        return ''
    return str(value).strip()


def money(cents: Any) -> dict | None:
    """Cents from HCP as a GHL Monetary Value, or `None` when there is no number to send.

    Zero is a number: a job with nothing left to pay has `due_amount: 0`, and that is exactly
    the case where the field must be written rather than skipped.
    """
    if cents is None or isinstance(cents, bool):
        return None
    try:
        value = int(cents) / CENTS
    except (TypeError, ValueError):
        return None
    return {'value': value, 'currency': MONETARY_CURRENCY}


def whole(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def joined(values: Any, separator: str = ', ') -> str:
    """A list of strings as one line. `[]` and non-lists both give `''`."""
    if not isinstance(values, list):
        return ''
    return separator.join(item for item in (text(value) for value in values) if item)


def present(properties: dict) -> dict:
    """Drop every key with nothing in it - see the module docstring on blank-vs-omitted.

    `0` and `{'value': 0.0, ...}` are kept: they are values HCP actually reported.
    """
    return {key: value for key, value in properties.items() if value not in (None, '', [], {})}


def address_of(payload: dict, key: str = 'address') -> dict:
    """HCP hangs an address off `address` on jobs and estimates, and off the first entry of
    `addresses` on a customer. Both land here as one dict.
    """
    address = payload.get(key)
    if isinstance(address, dict):
        return address
    addresses = payload.get('addresses')
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        return addresses[0]
    return {}


def _schedule(payload: dict) -> dict:
    schedule = payload.get('schedule')
    return schedule if isinstance(schedule, dict) else {}


def _work_timestamps(payload: dict) -> dict:
    stamps = payload.get('work_timestamps')
    return stamps if isinstance(stamps, dict) else {}


def _notes_text(payload: dict) -> str:
    """HCP's `notes` is a list of `{content: ...}`; GHL's field is one Multi Line Text."""
    notes = payload.get('notes')
    if isinstance(notes, str):
        return text(notes)
    if not isinstance(notes, list):
        return ''
    return '\n'.join(
        item
        for item in (text(note.get('content')) for note in notes if isinstance(note, dict))
        if item
    )


# --- contacts ---------------------------------------------------------------------------


def contact_payload(customer: dict, field_ids: dict[str, str]) -> dict:
    """An HCP customer as a GHL contact body.

    `field_ids` is `{field name: GHL id}` from `GHLObjects.contact_field_ids()`. A field the
    snapshot does not have is skipped rather than sent with a null id, which GHL rejects.
    """
    from apps.hcp.constants import HCP_CUSTOMER_ID_FIELD, HCP_CUSTOMER_NOTE_FIELD

    address = address_of(customer)
    payload = present(
        {
            'firstName': text(customer.get('first_name')),
            'lastName': text(customer.get('last_name')),
            'companyName': text(customer.get('company')),
            'email': text(customer.get('email')),
            'phone': text(customer.get('mobile_number')) or text(customer.get('home_number')),
            'source': text(customer.get('lead_source')),
            'address1': text(address.get('street')),
            'city': text(address.get('city')),
            'state': text(address.get('state')),
            'postalCode': text(address.get('zip')),
        }
    )

    tags = customer.get('tags')
    if isinstance(tags, list):
        cleaned = [item for item in (text(tag) for tag in tags) if item]
        if cleaned:
            payload['tags'] = cleaned

    custom_fields = []
    if field_ids.get(HCP_CUSTOMER_ID_FIELD):
        custom_fields.append(
            {'id': field_ids[HCP_CUSTOMER_ID_FIELD], 'field_value': text(customer.get('id'))}
        )
    note = _notes_text(customer)
    if note and field_ids.get(HCP_CUSTOMER_NOTE_FIELD):
        custom_fields.append({'id': field_ids[HCP_CUSTOMER_NOTE_FIELD], 'field_value': note})
    if custom_fields:
        payload['customFields'] = custom_fields

    return payload


def contact_is_identifiable(payload: dict) -> bool:
    """GHL refuses a contact with nothing to call it. Checked before the write so the refusal
    reads as "this HCP customer is too empty to mirror" rather than a 422 from GHL.
    """
    return any(payload.get(key) for key in ('email', 'phone', 'firstName', 'lastName'))


# --- leads ------------------------------------------------------------------------------


def lead_properties(lead: dict, contact_record_id: str) -> dict:
    address = address_of(lead)
    customer = lead.get('customer') if isinstance(lead.get('customer'), dict) else {}
    return present(
        {
            'contact_id': contact_record_id,
            'external_lead_id': text(lead.get('id')),
            'lead_number': text(lead.get('lead_number')),
            'lead_status': text(lead.get('status')) or text(lead.get('pipeline_status')),
            'lead_source': text(lead.get('lead_source')) or text(customer.get('lead_source')),
            'lead_value': money(lead.get('estimated_value') or lead.get('total_amount')),
            'street': text(address.get('street')),
            'unit': text(address.get('street_line_2')),
            'city': text(address.get('city')),
            'state': text(address.get('state')),
            'zip': text(address.get('zip')),
            'tags': joined(lead.get('tags')),
        }
    )


# --- estimates ---------------------------------------------------------------------------


def _option_lines(options: list, formatter) -> str:
    return '\n'.join(
        line
        for line in (formatter(option) for option in options if isinstance(option, dict))
        if line
    )


def _line_items_text(line_items: Any) -> str:
    """`Name - $12.34` per line, the summary shape the snapshot's Multi Line Text fields hold."""
    if not isinstance(line_items, list):
        return ''
    lines = []
    for item in line_items:
        if not isinstance(item, dict):
            continue
        name = text(item.get('name')) or 'Unknown'
        amount = money(
            item.get('amount') if item.get('amount') is not None else item.get('unit_price')
        )
        lines.append(f'{name} - ${amount["value"]:.2f}' if amount else name)
    return '\n'.join(lines)


def _approved_option(options: list) -> dict:
    """The one option the customer approved, if any. HCP allows exactly one."""
    for option in options:
        if isinstance(option, dict) and text(option.get('approval_status')).lower() == 'approved':
            return option
    return {}


def estimate_properties(estimate: dict, contact_record_id: str) -> dict:
    address = address_of(estimate)
    schedule = _schedule(estimate)
    stamps = _work_timestamps(estimate)
    options = estimate.get('options') if isinstance(estimate.get('options'), list) else []
    approved = _approved_option(options)

    properties = {
        'contact_id': contact_record_id,
        'external_estimate_id': text(estimate.get('id')),
        'estimate_number': text(estimate.get('estimate_number')),
        'estimate_status': text(estimate.get('work_status')) or text(estimate.get('status')),
        'lead_source': text(estimate.get('lead_source')),
        'scheduled_start_at': text(schedule.get('scheduled_start')),
        'scheduled_end_at': text(schedule.get('scheduled_end')),
        'arrival_window_minutes': whole(schedule.get('arrival_window')),
        'technician_en_route_at': text(stamps.get('on_my_way_at')),
        'work_started_at': text(stamps.get('started_at')),
        'work_completed_at': text(stamps.get('completed_at')),
        'estimate_sent_at': text(estimate.get('sent_at')),
        'street': text(address.get('street')),
        'unit': text(address.get('street_line_2')),
        'city': text(address.get('city')),
        'state': text(address.get('state')),
        'postal_code': text(address.get('zip')),
    }

    if approved:
        properties.update(
            {
                'approved_option_name': text(approved.get('name')),
                'approved_option_number': text(approved.get('option_number')),
                'approved_total_amount': money(approved.get('total_amount')),
                'approved_line_items': _line_items_text(approved.get('line_items')),
            }
        )

    if options:
        properties.update(
            {
                'all_options_numbers': _option_lines(
                    options, lambda o: text(o.get('option_number'))
                ),
                'all_options_names': _option_lines(options, lambda o: text(o.get('name'))),
                'all_options_total_amount': _option_lines(options, _option_total_line),
                'all_options_details': _option_lines(options, _option_detail_line),
                'all_line_items': '\n'.join(
                    line
                    for line in (
                        _line_items_text(o.get('line_items'))
                        for o in options
                        if isinstance(o, dict)
                    )
                    if line
                ),
            }
        )

    return present(properties)


def _option_label(option: dict) -> str:
    return text(option.get('name')) or f'Option {text(option.get("option_number"))}'.strip()


def _option_total_line(option: dict) -> str:
    """`Option 1: $450.00` - one line per option, because a Monetary field holds only one
    number and an estimate can carry good/better/best.
    """
    amount = money(option.get('total_amount'))
    label = _option_label(option)
    if not amount or not label:
        return ''
    return f'{label}: ${amount["value"]:.2f}'


def _option_detail_line(option: dict) -> str:
    parts = [_option_label(option)]
    status = text(option.get('approval_status'))
    if status:
        parts.append(f'({status})')
    amount = money(option.get('total_amount'))
    if amount:
        parts.append(f'${amount["value"]:.2f}')
    return ' '.join(part for part in parts if part)


# --- jobs ---------------------------------------------------------------------------------


def job_properties(job: dict, contact_record_id: str) -> dict:
    address = address_of(job)
    schedule = _schedule(job)
    stamps = _work_timestamps(job)
    invoices = job.get('invoices') if isinstance(job.get('invoices'), list) else []

    return present(
        {
            'contact_id': contact_record_id,
            'hcp_job_id': text(job.get('id')),
            'job_number': text(job.get('invoice_number')) or text(job.get('job_number')),
            'job_description': text(job.get('description')),
            'job_status': text(job.get('work_status')),
            'lead_source': text(job.get('lead_source')),
            'job_amount': money(job.get('total_amount')),
            'due_amount': money(job.get('outstanding_balance')),
            'job_type': job_type(job),
            'tags': joined(job.get('tags')),
            'street_line_1': text(address.get('street')),
            JOB_PROPERTIES['street_line_2']: text(address.get('street_line_2')),
            'city': text(address.get('city')),
            'state': text(address.get('state')),
            'postal_code': text(address.get('zip')),
            'notes': _notes_text(job),
            'scheduled_start_at': text(schedule.get('scheduled_start')),
            'scheduled_end_at': text(schedule.get('scheduled_end')),
            'arrival_window_minutes': whole(schedule.get('arrival_window')),
            'technician_en_route_at': text(stamps.get('on_my_way_at')),
            'work_started_at': text(stamps.get('started_at')),
            'work_completed_at': text(stamps.get('completed_at')),
            'line_items': _line_items_text(job.get('line_items')),
            'invoice_number': text(job.get('invoice_number')),
            'invoices': _invoices_text(invoices),
        }
    )


def job_type(job: dict) -> str:
    """HCP sends `job_fields.job_type` as an object with a name on it, and older payloads send
    a plain string. Both are the one Single Line Text field in GHL.
    """
    fields = job.get('job_fields') if isinstance(job.get('job_fields'), dict) else {}
    value = fields.get('job_type', job.get('job_type'))
    if isinstance(value, dict):
        return text(value.get('name'))
    return text(value)


def _invoices_text(invoices: list) -> str:
    lines = []
    for invoice in invoices:
        if not isinstance(invoice, dict):
            continue
        number = text(invoice.get('invoice_number')) or text(invoice.get('number'))
        amount = money(
            invoice.get('amount') if invoice.get('amount') is not None else invoice.get('total')
        )
        status = text(invoice.get('status'))
        parts = [
            part for part in (number, f'${amount["value"]:.2f}' if amount else '', status) if part
        ]
        if parts:
            lines.append(' - '.join(parts))
    return '\n'.join(lines)


# --- job line items --------------------------------------------------------------------------


def line_item_properties(item: dict, job_record_id: str) -> dict:
    taxable = item.get('taxable')
    return present(
        {
            'job_record_id': job_record_id,
            'hcp_line_item_id': text(item.get('id')),
            'name': text(item.get('name')),
            'item_description': text(item.get('description')),
            'quantity': whole(item.get('quantity')),
            'unit_price': money(item.get('unit_price')),
            'unit_cost': money(item.get('unit_cost')),
            'total_price': money(
                item.get('amount') if item.get('amount') is not None else item.get('total_price')
            ),
            # A dropdown, so the words the snapshot configured - not `True`/`False`.
            'taxable': (TAXABLE_YES if taxable else TAXABLE_NO) if taxable is not None else '',
            # Left off entirely when HCP sends a kind the snapshot has no option for: GHL
            # rejects an unconfigured dropdown value, which would cost the whole line item.
            'item_type': ITEM_TYPES.get(text(item.get('kind')).lower(), ''),
        }
    )


def line_item_fields(item: dict) -> dict:
    """The `JobLineItem` columns an HCP line item maps to, for the local mirror."""
    from decimal import Decimal

    def decimal_of(cents: Any) -> Decimal | None:
        amount = money(cents)
        return None if amount is None else Decimal(str(amount['value']))

    quantity = whole(item.get('quantity'))
    return {
        'name': text(item.get('name')),
        'description': text(item.get('description')),
        'unit_price': decimal_of(item.get('unit_price')),
        'unit_cost': decimal_of(item.get('unit_cost')),
        'quantity': Decimal(quantity) if quantity is not None else Decimal('1'),
        'taxable': bool(item.get('taxable')),
        'total_price': decimal_of(
            item.get('amount') if item.get('amount') is not None else item.get('total_price')
        ),
        'item_type': text(item.get('kind')),
    }


# --- appointments -------------------------------------------------------------------------


def appointment_payload(
    *,
    calendar_id: str,
    contact_id: str,
    assigned_user_id: str,
    title: str,
    starts_at: str,
    ends_at: str,
    address: str,
) -> dict:
    """A GHL calendar booking mirroring one HCP appointment.

    `appointmentStatus: confirmed` because HCP has already booked it - this is a mirror of a
    commitment that exists, not a request for one.
    """
    return present(
        {
            'calendarId': calendar_id,
            'contactId': contact_id,
            'assignedUserId': assigned_user_id,
            'title': title,
            'startTime': starts_at,
            'endTime': ends_at,
            'address': address,
            'meetingLocationType': 'address',
            'appointmentStatus': 'confirmed',
        }
    )


def address_line(address: dict) -> str:
    """An HCP address as the one line GHL shows on an appointment."""
    parts = [
        text(address.get('street')),
        text(address.get('street_line_2')),
        text(address.get('city')),
        text(address.get('state')),
        text(address.get('zip')),
    ]
    return ', '.join(part for part in parts if part)


def address_fields(address: dict) -> dict:
    """The five address columns shared by `Lead`, `Estimate` and `Job` in the local mirror."""
    return {
        'street_line_1': text(address.get('street')),
        'street_line_2': text(address.get('street_line_2')),
        'city': text(address.get('city')),
        'state': text(address.get('state')),
        'postal_code': text(address.get('zip')),
    }
