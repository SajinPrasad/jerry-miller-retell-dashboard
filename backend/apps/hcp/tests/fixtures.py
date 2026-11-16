"""Sample Housecall Pro payloads, shaped like the ones the reference project handles.

Each builder returns the *whole* webhook body - HCP puts the entity under its own key next to
`event` and `company_id`, and the handlers read it from there, so a fixture that returned only
the entity would not exercise the path the webhook actually takes.
"""

COMPANY_ID = 'hcp-company-1'


def envelope(event: str, **body) -> dict:
    return {'event': event, 'company_id': COMPANY_ID, **body}


def customer(customer_id='hcp-customer-1', **overrides) -> dict:
    data = {
        'id': customer_id,
        'first_name': 'Jane',
        'last_name': 'Doe',
        'company': 'Doe Residence',
        'email': 'jane@example.test',
        'mobile_number': '+15551230001',
        'home_number': '+15551230002',
        'lead_source': 'Google',
        'tags': ['VIP'],
        'notes': [{'content': 'Gate code 1234'}],
        'addresses': [
            {
                'street': '12 Elm St',
                'street_line_2': 'Apt 4',
                'city': 'Austin',
                'state': 'TX',
                'zip': '78701',
            }
        ],
    }
    data.update(overrides)
    return data


def customer_event(event='customer.created', **overrides) -> dict:
    return envelope(event, customer=customer(**overrides))


def lead(lead_id='hcp-lead-1', **overrides) -> dict:
    data = {
        'id': lead_id,
        'lead_number': 41,
        'status': 'new',
        'lead_source': 'Website',
        'estimated_value': 45000,
        'customer': customer(),
        'tags': ['hvac', 'urgent'],
        'address': {
            'street': '12 Elm St',
            'street_line_2': 'Apt 4',
            'city': 'Austin',
            'state': 'TX',
            'zip': '78701',
        },
    }
    data.update(overrides)
    return data


def lead_event(event='lead.created', **overrides) -> dict:
    return envelope(event, lead=lead(**overrides))


def option(option_id='hcp-option-1', approval_status='pro forma', **overrides) -> dict:
    data = {
        'id': option_id,
        'name': 'Good',
        'option_number': '1',
        'total_amount': 45000,
        'approval_status': approval_status,
        'status': 'sent',
        'line_items': [{'id': 'li-1', 'name': 'Condenser', 'amount': 40000}],
    }
    data.update(overrides)
    return data


def estimate(estimate_id='hcp-estimate-1', **overrides) -> dict:
    data = {
        'id': estimate_id,
        'estimate_number': '1042',
        'work_status': 'estimate_sent',
        'lead_source': 'Website',
        'sent_at': '2026-03-01T15:00:00Z',
        'customer': customer(),
        'options': [option()],
        'schedule': {
            'scheduled_start': '2026-03-04T15:00:00Z',
            'scheduled_end': '2026-03-04T17:00:00Z',
            'arrival_window': 60,
        },
        'work_timestamps': {
            'on_my_way_at': '2026-03-04T14:30:00Z',
            'started_at': '2026-03-04T15:05:00Z',
            'completed_at': '2026-03-04T16:40:00Z',
        },
        'address': {
            'street': '12 Elm St',
            'street_line_2': 'Apt 4',
            'city': 'Austin',
            'state': 'TX',
            'zip': '78701',
        },
    }
    data.update(overrides)
    return data


def estimate_event(event='estimate.created', **overrides) -> dict:
    return envelope(event, estimate=estimate(**overrides))


def line_item(item_id='hcp-line-1', **overrides) -> dict:
    data = {
        'id': item_id,
        'name': 'Condenser unit',
        'description': '3 ton, 16 SEER',
        'quantity': 1,
        'unit_price': 40000,
        'unit_cost': 28000,
        'amount': 40000,
        'taxable': True,
        'kind': 'materials',
    }
    data.update(overrides)
    return data


def job(job_id='hcp-job-1', **overrides) -> dict:
    data = {
        'id': job_id,
        'invoice_number': '5567',
        'description': 'Install replacement condenser',
        'work_status': 'scheduled',
        'lead_source': 'Website',
        'total_amount': 450000,
        'outstanding_balance': 150000,
        'job_fields': {'job_type': {'id': 'jt-1', 'name': 'Installation'}},
        'customer': customer(),
        'tags': ['install'],
        'notes': [{'content': 'Park in the alley'}],
        'line_items': [line_item()],
        'invoices': [{'invoice_number': 'INV-1', 'amount': 450000, 'status': 'sent'}],
        'schedule': {
            'scheduled_start': '2026-03-10T15:00:00Z',
            'scheduled_end': '2026-03-10T19:00:00Z',
            'arrival_window': 30,
        },
        'work_timestamps': {
            'on_my_way_at': None,
            'started_at': None,
            'completed_at': None,
        },
        'assigned_employees': [{'id': 'hcp-emp-1', 'first_name': 'Ana', 'last_name': 'Ruiz'}],
        'address': {
            'street': '12 Elm St',
            'street_line_2': 'Apt 4',
            'city': 'Austin',
            'state': 'TX',
            'zip': '78701',
        },
    }
    data.update(overrides)
    return data


def job_event(event='job.created', **overrides) -> dict:
    return envelope(event, job=job(**overrides))


def appointment(appointment_id='hcp-appt-1', **overrides) -> dict:
    data = {
        'id': appointment_id,
        'start_time': '2026-03-10T15:00:00Z',
        'end_time': '2026-03-10T19:00:00Z',
        'arrival_window_minutes': 30,
        'dispatched_employees': [{'id': 'hcp-emp-1', 'first_name': 'Ana', 'last_name': 'Ruiz'}],
    }
    data.update(overrides)
    return data


def appointment_event(event='job.appointment.scheduled', job_overrides=None, **overrides) -> dict:
    return envelope(
        event,
        job=job(**(job_overrides or {})),
        appointment=appointment(**overrides),
    )
