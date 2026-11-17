"""Sample GoHighLevel webhook payloads for Phase 4 (GHL -> HCP), shaped like the reference
project's own `luxiolab_webhook` dispatches them - see the module docstring convention in
`fixtures.py` for why each builder returns the whole webhook body rather than just the entity.
"""

from apps.hcp.tests.conftest import LOCATION_ID

WEBHOOK_ID = 'ghl-webhook-1'


def envelope(event_type: str, **body) -> dict:
    return {'type': event_type, 'locationId': LOCATION_ID, 'webhookId': WEBHOOK_ID, **body}


def contact(contact_id='ghl-contact-1', **overrides) -> dict:
    data = {
        'id': contact_id,
        'firstName': 'Jane',
        'lastName': 'Doe',
        'companyName': '',
        'email': 'jane@example.test',
        'phone': '+15551230001',
        'phoneLabel': 'Mobile',
        'additionalPhones': [],
        'source': 'Google',
        'tags': ['vip'],
        'customFields': [],
        'address1': '12 Elm St',
        'city': 'Austin',
        'state': 'TX',
        'postalCode': '78701',
    }
    data.update(overrides)
    return data


def contact_event(event_type='ContactCreate', **overrides) -> dict:
    return envelope(event_type, **contact(**overrides))


def property_entry(key: str, value, value_type: str = 'valueString') -> dict:
    return {'key': key, value_type: value}


def record(record_id='ghl-record-1', object_key='custom_objects.leads', properties=None) -> dict:
    return {'id': record_id, 'objectKey': object_key, 'properties': properties or []}


def record_event(event_type: str, **record_kwargs) -> dict:
    return envelope(event_type, **record(**record_kwargs))


def appointment(appointment_id='ghl-appt-1', **overrides) -> dict:
    data = {
        'id': appointment_id,
        'startTime': '2026-11-20T15:00:00+00:00',
        'endTime': '2026-11-20T16:00:00+00:00',
        'assignedUserId': '',
        'dateUpdated': '2026-11-17T12:00:00+00:00',
    }
    data.update(overrides)
    return data


def appointment_event(event_type='AppointmentUpdate', **overrides) -> dict:
    return envelope(event_type, appointment=appointment(**overrides))
