"""The names Housecall Pro and GoHighLevel use on the wire.

Kept in one module because both halves are **external vocabularies we do not control** and both
are easy to get subtly wrong:

- HCP's event names are the dispatch table. They are transcribed from the reference project's
  own `hcp_webhook` view (`HCP-Integration/integrations/views.py`), which is effectively the
  only written record of which events HCP actually emits - its docs do not enumerate them.
- GHL's property keys are derived by GoHighLevel from the *field name* at creation time and are
  immutable afterwards, so `Street Line 2 / Unit` is forever `street_line_2__unit`. Renaming
  the field in the UI does not change the key. They must match
  `local_folder/GHL_Snapshot_Specification.md` exactly or the write silently drops the field.
"""

# --- HCP webhook events ---------------------------------------------------------------------

CUSTOMER_CREATED = 'customer.created'
CUSTOMER_UPDATED = 'customer.updated'
CUSTOMER_EVENTS = frozenset({CUSTOMER_CREATED, CUSTOMER_UPDATED})

LEAD_CREATED = 'lead.created'
# `lead.converted` and `lead.lost` are updates to a lead that already exists, not new leads, so
# they take the update path with the rest.
LEAD_UPDATED_EVENTS = frozenset({'lead.updated', 'lead.converted', 'lead.lost'})
LEAD_EVENTS = frozenset({LEAD_CREATED}) | LEAD_UPDATED_EVENTS

# Nine of them, and every one carries the whole estimate, so they all take one handler that
# upserts from the payload rather than nine that each patch a field.
ESTIMATE_EVENTS = frozenset(
    {
        'estimate.created',
        'estimate.updated',
        'estimate.scheduled',
        'estimate.sent',
        'estimate.completed',
        'estimate.on_my_way',
        'estimate.copy_to_job',
        'estimate.option.created',
        'estimate.option.approval_status_changed',
    }
)

JOB_EVENTS = frozenset(
    {
        'job.created',
        'job.updated',
        'job.scheduled',
        'job.started',
        'job.completed',
        'job.canceled',
        'job.deleted',
        'job.paid',
        'job.on_my_way',
    }
)

# Appointment events carry an appointment id and are about scheduling, not the job's own fields.
#
# The five names below are the ones the reference dispatches on, i.e. the ones HCP is known to
# send. `job.appointment.canceled` / `.completed` are **not** among them - a cancellation
# arrives as `appointment_discarded`, and completion arrives on the job itself as
# `job.completed`. Both are accepted anyway so that an account whose HCP version does emit them
# is not silently ignored; an event HCP never sends simply never arrives.
APPOINTMENT_SCHEDULED = 'job.appointment.scheduled'
APPOINTMENT_RESCHEDULED = 'job.appointment.rescheduled'
APPOINTMENT_DISCARDED = 'job.appointment.appointment_discarded'
APPOINTMENT_PROS_ASSIGNED = 'job.appointment.appointment_pros_assigned'
APPOINTMENT_PROS_UNASSIGNED = 'job.appointment.appointment_pros_unassigned'
APPOINTMENT_CANCELED = 'job.appointment.canceled'
APPOINTMENT_COMPLETED = 'job.appointment.completed'
# The ones that remove a booking rather than write one.
APPOINTMENT_REMOVED_EVENTS = frozenset({APPOINTMENT_DISCARDED, APPOINTMENT_CANCELED})
APPOINTMENT_EVENTS = (
    frozenset(
        {
            APPOINTMENT_SCHEDULED,
            APPOINTMENT_RESCHEDULED,
            APPOINTMENT_PROS_ASSIGNED,
            APPOINTMENT_PROS_UNASSIGNED,
            APPOINTMENT_COMPLETED,
        }
    )
    | APPOINTMENT_REMOVED_EVENTS
)

# --- GHL custom objects ---------------------------------------------------------------------

LEADS_OBJECT = 'custom_objects.leads'
ESTIMATES_OBJECT = 'custom_objects.estimates'
JOBS_OBJECT = 'custom_objects.jobs'
JOB_LINE_ITEMS_OBJECT = 'custom_objects.job_line_items'

# --- GHL webhook events (Phase 4: GHL -> HCP reverse sync) --------------------------------
#
# GHL's own vocabulary, transcribed from the reference project's `luxiolab_webhook` view - the
# one place that names these events against GHL's real, running webhook delivery, since GHL's
# own docs describe the *shape* of a webhook but not which event types a marketplace app can
# subscribe to. Record-level events carry an `objectKey` naming which custom object fired;
# contact and appointment events do not need one.

GHL_CONTACT_CREATE = 'ContactCreate'
GHL_CONTACT_UPDATE = 'ContactUpdate'
GHL_CONTACT_EVENTS = frozenset({GHL_CONTACT_CREATE, GHL_CONTACT_UPDATE})

GHL_RECORD_CREATE = 'RecordCreate'
GHL_RECORD_UPDATE = 'RecordUpdate'
GHL_RECORD_DELETE = 'RecordDelete'

# Lead/Estimate/Job are create-only in this direction (SOP: "updates ... are not supported from
# [GHL] to Housecall Pro"), so only `RecordCreate` is ever handled for them - there is no
# `RecordUpdate`/`RecordDelete` entry for these three object keys, which is what makes the
# asymmetry self-enforcing: an update GHL sends for one of them simply has nothing to dispatch
# to, rather than needing a per-event check to reject it.
GHL_CREATE_ONLY_OBJECTS = frozenset({LEADS_OBJECT, ESTIMATES_OBJECT, JOBS_OBJECT})
# Job line items are the one genuinely two-way custom object.
GHL_TWO_WAY_OBJECTS = frozenset({JOB_LINE_ITEMS_OBJECT})

GHL_APPOINTMENT_UPDATE = 'AppointmentUpdate'
GHL_APPOINTMENT_DELETE = 'AppointmentDelete'
# `AppointmentCreate` is never handled: the SOP is explicit that appointment creation stays
# HCP-only in both directions, so there is no event name for it here at all.
GHL_APPOINTMENT_EVENTS = frozenset({GHL_APPOINTMENT_UPDATE, GHL_APPOINTMENT_DELETE})

# --- GHL contact custom fields (matched by name; the id differs per sub-account) ---------------

HCP_CUSTOMER_ID_FIELD = 'HCP Customer ID'
HCP_CUSTOMER_NOTE_FIELD = 'HCP Customer Note'

# --- GHL custom-object property keys ----------------------------------------------------------
# Each dict is {our name: GHL's immutable property key}. See the module docstring for why these
# cannot be derived at runtime from the field names in the spec.

LEAD_PROPERTIES = {
    'contact_id': 'contact_id',
    'external_lead_id': 'external_lead_id',
    'lead_status': 'lead_status',
    'lead_source': 'lead_source',
    'lead_value': 'lead_value',
    'street': 'street',
    'unit': 'unit',
    'city': 'city',
    'state': 'state',
    'zip': 'zip',
    'lead_number': 'lead_number',
    'tags': 'tags',
}

ESTIMATE_PROPERTIES = {
    'contact_id': 'contact_id',
    'external_estimate_id': 'external_estimate_id',
    'estimate_status': 'estimate_status',
    'lead_source': 'lead_source',
    'scheduled_start_at': 'scheduled_start_at',
    'scheduled_end_at': 'scheduled_end_at',
    'arrival_window_minutes': 'arrival_window_minutes',
    'technician_en_route_at': 'technician_en_route_at',
    'work_started_at': 'work_started_at',
    'work_completed_at': 'work_completed_at',
    'street': 'street',
    'unit': 'unit',
    'city': 'city',
    'state': 'state',
    'postal_code': 'postal_code',
    'approved_option_name': 'approved_option_name',
    'approved_option_number': 'approved_option_number',
    'approved_total_amount': 'approved_total_amount',
    'approved_line_items': 'approved_line_items',
    'all_line_items': 'all_line_items',
    'all_options_numbers': 'all_options_numbers',
    'all_options_names': 'all_options_names',
    'all_options_total_amount': 'all_options_total_amount',
    'all_options_details': 'all_options_details',
    'estimate_number': 'estimate_number',
    'estimate_sent_at': 'estimate_sent_at',
}

JOB_PROPERTIES = {
    'contact_id': 'contact_id',
    'job_number': 'job_number',
    'hcp_job_id': 'hcp_job_id',
    'job_description': 'job_description',
    'job_status': 'job_status',
    'lead_source': 'lead_source',
    'job_amount': 'job_amount',
    'due_amount': 'due_amount',
    'job_type': 'job_type',
    'tags': 'tags',
    'street_line_1': 'street_line_1',
    # "Street Line 2 / Unit" - the ` / ` in the field name becomes a double underscore.
    'street_line_2': 'street_line_2__unit',
    'city': 'city',
    'state': 'state',
    'postal_code': 'postal_code',
    'notes': 'notes',
    'scheduled_start_at': 'scheduled_start_at',
    'scheduled_end_at': 'scheduled_end_at',
    'arrival_window_minutes': 'arrival_window_minutes',
    'technician_en_route_at': 'technician_en_route_at',
    'work_started_at': 'work_started_at',
    'work_completed_at': 'work_completed_at',
    'line_items': 'line_items',
    'invoice_number': 'invoice_number',
    'invoices': 'invoices',
}

JOB_LINE_ITEM_PROPERTIES = {
    'job_record_id': 'job_record_id',
    'name': 'name',
    'item_description': 'item_description',
    'quantity': 'quantity',
    'unit_price': 'unit_price',
    'unit_cost': 'unit_cost',
    'taxable': 'taxable',
    'item_type': 'item_type',
    'hcp_line_item_id': 'hcp_line_item_id',
    'total_price': 'total_price',
}

# `Taxable` is a single-select dropdown in the snapshot spec, so it takes these words, not a bool.
TAXABLE_YES = 'Yes'
TAXABLE_NO = 'No'

# `Item Type` is a dropdown too. HCP's own values are lower-case with underscores; the snapshot
# spells them for humans. Anything HCP sends that is not in here is left off rather than guessed,
# because GHL rejects a dropdown value that is not one of the configured options.
ITEM_TYPES = {
    'materials': 'Materials',
    'labor': 'Labor',
    'fixed_gratuity': 'Fixed Gratuity',
    'fixed_discount': 'Fixed Discount',
    'percent_discount': 'Percent Discount',
}

# GHL wants `{'value': ..., 'currency': 'default'}` for a Monetary Value field, not a bare number.
MONETARY_CURRENCY = 'default'
