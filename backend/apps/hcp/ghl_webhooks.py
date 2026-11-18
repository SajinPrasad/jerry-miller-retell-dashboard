"""The routing table for the GHL events Phase 4 (GHL -> HCP reverse sync) handles.

The endpoint that receives them is `apps/ghl/webhooks.py` (`POST /api/ghl/webhooks/`), the one GHL
webhook URL for this marketplace app. That view checks the signature, records the delivery through
`apps.audit`, and then calls `task_for` here to find the Celery task. Nothing in this module reads
a request.

The events this table routes must be subscribed on the marketplace app: `ContactCreate`,
`ContactUpdate`, `RecordCreate`, `RecordUpdate`, `RecordDelete`, `AppointmentUpdate` and
`AppointmentDelete`. See `handover.md` §6A.8.
"""

from apps.hcp import constants
from apps.hcp import reverse_tasks as tasks

# Every `custom_objects.*` + event-type combination Phase 4 actually handles. Leads, Estimates
# and Jobs carry `RecordCreate` only - the SOP's asymmetry itself, not a per-event check, is why
# an update for one of them is simply absent from this table. Job Line Items are the one
# genuinely two-way custom object.
RECORD_DISPATCH = {
    (constants.LEADS_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_lead,
    (constants.ESTIMATES_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_estimate,
    (constants.JOBS_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_job,
    (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_CREATE): tasks.sync_ghl_job_line_item,
    (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_UPDATE): tasks.sync_ghl_job_line_item,
    (constants.JOB_LINE_ITEMS_OBJECT, constants.GHL_RECORD_DELETE): tasks.delete_ghl_job_line_item,
}

_RECORD_EVENTS = frozenset(
    {constants.GHL_RECORD_CREATE, constants.GHL_RECORD_UPDATE, constants.GHL_RECORD_DELETE}
)


def task_for(event_type: str, object_key: str):
    """The Celery task that handles one GHL event, or `None` if we do not handle it.

    A `RecordCreate`/`Update`/`Delete` with no matching `(object_key, event_type)` pair -
    including an update for a create-only object, which has no entry at all - resolves to
    `None` the same as an event type this app never subscribed to.
    """
    if event_type == constants.GHL_CONTACT_CREATE:
        return tasks.sync_ghl_contact_create
    if event_type == constants.GHL_CONTACT_UPDATE:
        return tasks.sync_ghl_contact_update
    if event_type in constants.GHL_APPOINTMENT_EVENTS:
        return tasks.sync_ghl_appointment
    if event_type in _RECORD_EVENTS:
        return RECORD_DISPATCH.get((object_key, event_type))
    return None
