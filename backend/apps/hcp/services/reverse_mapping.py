"""Turning a GoHighLevel payload into the Housecall Pro shapes that mirror it back.

The reverse of `mapping.py`: pure functions, dict in, dict out, no database, no HTTP, no
settings. Phase 4 only pushes GHL -> HCP for what the SOP allows - contacts and job line items
both ways, leads/estimates/jobs on creation only, appointments on time and technician only - so
this module is deliberately smaller than the forward one. There is no "build an estimate's
options" shape here: HCP is what computes pricing and options, never GHL.

Two conventions carry over unchanged from the forward direction:

- **Money is cents on the HCP side, dollars on the GHL side.** A GHL Monetary Value property
  arrives as `{'value': dollars, 'currency': ...}` (the same shape `mapping.money()` builds
  going the other way) and is converted to whole cents for HCP.
- **Blank is omitted, not sent.** A key a caller has nothing for is left out of the HCP payload
  rather than sent empty, for the same reason `mapping.present()` exists: HCP's own update
  endpoints treat an omitted field as "leave it", not "clear it".
"""

from datetime import datetime
from typing import Any

from django.utils.dateparse import parse_datetime

from apps.hcp.constants import ITEM_TYPES, TAXABLE_YES
from apps.hcp.services.mapping import present, text, whole

# The inverse of `constants.ITEM_TYPES`: GHL's human-readable dropdown label back to HCP's own
# lower-case, underscored `kind` value.
_HCP_ITEM_KIND = {label: kind for kind, label in ITEM_TYPES.items()}


# --- reading a custom-object record's properties ----------------------------------------


def properties_dict(properties: Any) -> dict[str, Any]:
    """GHL's webhook shape for a record's properties - a list of `{key, value*}` entries, one
    typed value field per GHL field type - as the flat `{key: value}` dict the rest of this
    module works with.

    Only one of `valueString` / `valueNumber` / `valueCurrency` is ever present per entry; taking
    whichever is there is what makes a caller's `properties.get('unit_price')` work the same way
    regardless of which GHL field type backs it.
    """
    if not isinstance(properties, list):
        return {}
    flat: dict[str, Any] = {}
    for entry in properties:
        if not isinstance(entry, dict):
            continue
        key = entry.get('key')
        if not key:
            continue
        for value_key in ('valueString', 'valueNumber', 'valueCurrency'):
            if value_key in entry:
                flat[key] = entry[value_key]
                break
    return flat


def cents_of(value: Any) -> int | None:
    """A GHL Monetary Value property (`{'value': dollars, ...}`) as whole cents for HCP.

    `None` for anything that is not a number GHL could have sent this way - including a bare
    dict with no `value` - so a caller can tell "nothing to send" apart from a genuine zero.
    """
    if isinstance(value, dict):
        value = value.get('value')
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


# --- contacts -------------------------------------------------------------------------------


def contact_from_ghl(contact: dict) -> dict[str, str]:
    """The phone/name/company fields of a GHL contact, read once and reused by both the create
    and the update path.

    GHL carries one phone as `phone` with a `phoneLabel` saying which of mobile/home/work it is,
    plus any others in `additionalPhones` - never three separate fields the way HCP (and this
    app's own `IntegratedContact`) does, so the three are picked out of up to four entries here
    rather than three flat lookups.
    """
    fields = {
        'first_name': text(contact.get('firstName')),
        'last_name': text(contact.get('lastName')),
        'company_name': text(contact.get('companyName')),
        'email': text(contact.get('email')),
        'source': text(contact.get('source')),
        'mobile_number': '',
        'home_number': '',
        'work_number': '',
    }

    entries = [{'phoneLabel': contact.get('phoneLabel') or 'Mobile', 'phone': contact.get('phone')}]
    additional = contact.get('additionalPhones')
    if isinstance(additional, list):
        entries += [entry for entry in additional if isinstance(entry, dict)]

    for entry in entries:
        number = text(entry.get('phone'))
        if not number:
            continue
        label = text(entry.get('phoneLabel')) or 'Mobile'
        if label == 'Home':
            fields['home_number'] = number
        elif label == 'Work':
            fields['work_number'] = number
        else:
            fields['mobile_number'] = number

    return fields


def contact_is_creatable(fields: dict[str, str]) -> bool:
    """HCP needs something to call the customer. Matches the forward direction's own check
    (`mapping.contact_is_identifiable`), applied to GHL's field names instead of HCP's.
    """
    return bool(
        fields['email']
        or fields['first_name']
        or fields['last_name']
        or (
            fields['company_name']
            and any((fields['mobile_number'], fields['home_number'], fields['work_number']))
        )
    )


def customer_create_payload(contact: dict, fields: dict[str, str], note: str) -> dict:
    """An HCP customer, for the one call `HCPClient.create_customer()` makes.

    Address is included here and nowhere else: the SOP's one asymmetry on contacts is that an
    address is part of creating an HCP customer from a GHL contact, but never part of updating
    one - an address edited in GHL afterwards stays in GHL.
    """
    payload = present(
        {
            'first_name': fields['first_name'],
            'last_name': fields['last_name'],
            'company': fields['company_name'],
            'email': fields['email'],
            'mobile_number': fields['mobile_number'],
            'home_number': fields['home_number'],
            'work_number': fields['work_number'],
            'lead_source': fields['source'],
            'notes': note,
        }
    )
    address = present(
        {
            'street': text(contact.get('address1')),
            'city': text(contact.get('city')),
            'state': text(contact.get('state')),
            'zip': text(contact.get('postalCode')),
        }
    )
    if address:
        payload['addresses'] = [address]
    return payload


def customer_update_payload(changed: dict[str, str]) -> dict:
    """The subset of `fields` that differ from what is already stored, as HCP's own field
    names. Built by the caller (which has the stored `IntegratedContact` to diff against); this
    just renames the keys HCP expects.
    """
    renamed = {
        'first_name': changed.get('first_name'),
        'last_name': changed.get('last_name'),
        'company': changed.get('company_name'),
        'email': changed.get('email'),
        'mobile_number': changed.get('mobile_number'),
        'home_number': changed.get('home_number'),
        'work_number': changed.get('work_number'),
        'lead_source': changed.get('source'),
        'notes': changed.get('notes'),
    }
    return {key: value for key, value in renamed.items() if value is not None}


# --- leads / estimates / jobs (creation only) ----------------------------------------------


def lead_create_payload(properties: dict, hcp_customer_id: str) -> dict:
    """The only field the SOP requires is the linked customer. `lead_source` rides along when
    the GHL record happens to carry one; HCP fills in everything else (status, number, ...).
    """
    return present(
        {'customer_id': hcp_customer_id, 'lead_source': text(properties.get('lead_source'))}
    )


def estimate_create_payload(properties: dict, hcp_customer_id: str) -> dict:
    return present(
        {'customer_id': hcp_customer_id, 'lead_source': text(properties.get('lead_source'))}
    )


def job_create_payload(properties: dict, hcp_customer_id: str) -> dict:
    return present(
        {'customer_id': hcp_customer_id, 'lead_source': text(properties.get('lead_source'))}
    )


# --- job line items (genuinely two-way) ------------------------------------------------------


def line_item_payload(properties: dict) -> dict | None:
    """One job line item, as HCP's create/update body. `None` when GHL has no name for it - the
    one field the SOP calls mandatory, and the one HCP has no default for.
    """
    name = text(properties.get('name'))
    if not name:
        return None

    taxable = text(properties.get('taxable'))
    item_type = text(properties.get('item_type'))
    payload: dict[str, Any] = {
        'name': name,
        'description': text(properties.get('item_description')),
    }
    quantity = whole(properties.get('quantity'))
    if quantity is not None:
        payload['quantity'] = quantity
    unit_price = cents_of(properties.get('unit_price'))
    if unit_price is not None:
        payload['unit_price'] = unit_price
    unit_cost = cents_of(properties.get('unit_cost'))
    if unit_cost is not None:
        payload['unit_cost'] = unit_cost
    if taxable:
        payload['taxable'] = taxable.casefold() == TAXABLE_YES.casefold()
    if item_type in _HCP_ITEM_KIND:
        payload['kind'] = _HCP_ITEM_KIND[item_type]
    return payload


def line_item_changes(candidate: dict, existing) -> dict:
    """Only the keys of `candidate` that differ from the stored `JobLineItem` row.

    Quantity/price/cost are compared as the same units the row stores them in (`Decimal`
    dollars, a plain int), not re-derived, so a value HCP itself rounded slightly differently on
    the last write is not re-sent purely from float noise.
    """
    from decimal import Decimal

    changes: dict[str, Any] = {}
    if candidate.get('name', existing.name) != existing.name:
        changes['name'] = candidate['name']
    if candidate.get('description', existing.description) != existing.description:
        changes['description'] = candidate.get('description', '')
    if 'quantity' in candidate and Decimal(candidate['quantity']) != existing.quantity:
        changes['quantity'] = candidate['quantity']
    if 'unit_price' in candidate:
        stored = None if existing.unit_price is None else round(existing.unit_price * 100)
        if candidate['unit_price'] != stored:
            changes['unit_price'] = candidate['unit_price']
    if 'unit_cost' in candidate:
        stored = None if existing.unit_cost is None else round(existing.unit_cost * 100)
        if candidate['unit_cost'] != stored:
            changes['unit_cost'] = candidate['unit_cost']
    if 'taxable' in candidate and candidate['taxable'] != existing.taxable:
        changes['taxable'] = candidate['taxable']
    if 'kind' in candidate and candidate['kind'] != existing.item_type:
        changes['kind'] = candidate['kind']
    return changes


# --- appointments ---------------------------------------------------------------------------


def appointment_times(
    appointment: dict,
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """`(starts_at, ends_at, updated_at)` off a GHL `AppointmentUpdate`/`Delete` payload.

    `AppointmentIntegration.starts_at`/`ends_at`/`external_updated_at` are real `DateTimeField`s
    (unlike `Estimate`/`Job`'s schedule columns, which are the raw HCP strings) because this is
    the field the stale-event guard compares with `<=` - a string compare would just as often be
    comparing two different ways of spelling the same instant.
    """
    return (
        parse_datetime(text(appointment.get('startTime'))) or None,
        parse_datetime(text(appointment.get('endTime'))) or None,
        parse_datetime(text(appointment.get('dateUpdated'))) or None,
    )
