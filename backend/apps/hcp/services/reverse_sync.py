"""Mirroring GoHighLevel back into Housecall Pro - Phase 4, the asymmetric reverse half of the
sync the SOP describes.

Only what the SOP allows flows this way at all:

- **Contacts**: genuinely two-way (create and update), except an address only ever flows on
  create - an address edited in GHL afterwards is never pushed to HCP.
- **Leads, Estimates, Jobs**: creation only. There is no update handler for any of the three -
  not "an update that does nothing", but no dispatch path to one at all (`constants.py`'s
  `GHL_CREATE_ONLY_OBJECTS`) - because HCP is the system of record for every field on them once
  they exist.
- **Job line items**: genuinely two-way, including delete.
- **Appointments**: HCP schedules; GHL can only push a time change and, when a real (non-
  default) technician is mapped, a reassignment.

**Why this does not loop.** Every write Phase 3 makes to GHL eventually bounces back here as a
webhook, because GHL cannot tell our own write apart from a human's. Three guards stop that from
becoming a second write back to HCP, one per shape of entity:

1. **Create-only entities never dispatch an update at all** - see
   `constants.GHL_CREATE_ONLY_OBJECTS` above. The asymmetry itself is the guard; there is
   nothing here to make it safe.
2. **Creation, for every entity**: before creating anything, this checks whether the *incoming*
   GHL record already carries the HCP id as one of its own properties (Phase 3 writes it in the
   same call that creates the record, so it is on the record before GHL's own webhook for that
   creation can even be sent), and separately whether a local link row already exists. Either one
   is "we already know about this", not "GHL told us about it twice".
3. **Genuinely two-way entities (contacts, job line items) diff against the local mirror** and
   only call HCP for the fields that actually differ - then write the *new* values onto the local
   row regardless, whether or not anything was sent. The first echo of our own write compares
   clean against a cache that already matches it, and stops right there with no HCP call.
   **Appointments** add a fourth, cheaper check ahead of the diff: `AppointmentIntegration.
   external_updated_at`, a field Phase 1 already carries and Phase 3 never touches, rejects a
   webhook whose own timestamp is no newer than the one already recorded, before any comparison
   runs.

No new models and no migration - Phase 1's schema already carries everything this needs.
"""

import logging

from apps.ghl.exceptions import GHLError
from apps.ghl.services import GHLOAuthService
from apps.hcp import constants
from apps.hcp.constants import HCP_CUSTOMER_ID_FIELD, HCP_CUSTOMER_NOTE_FIELD, JOB_LINE_ITEMS_OBJECT
from apps.hcp.exceptions import HCPError
from apps.hcp.models import (
    APPOINTMENT_JOB,
    SOURCE_GHL,
    SYNCED,
    AppointmentIntegration,
    Estimate,
    HCPAccount,
    HCPEmployeeMapping,
    IntegratedContact,
    Job,
    JobLineItem,
    Lead,
)
from apps.hcp.services import mapping, reverse_mapping
from apps.hcp.services.ghl_objects import GHLObjects
from apps.hcp.services.hcp_client import HCPClient
from apps.hcp.services.sync import IGNORED, SKIPPED, SYNCED_OUTCOME

logger = logging.getLogger(__name__)

# `IntegratedContact` fields that flow both ways. Address is deliberately not in this list - see
# the module docstring - and neither is `tags`, which is a list and compared on its own below.
_CONTACT_FIELDS = (
    'first_name',
    'last_name',
    'company_name',
    'email',
    'mobile_number',
    'home_number',
    'work_number',
    'source',
)


class GHLToHCPSync:
    """One connected sub-account's reverse mirror, built per delivery like `HCPToGHLSync`."""

    def __init__(
        self,
        account: HCPAccount,
        ghl: GHLObjects | None = None,
        hcp: HCPClient | None = None,
        service: GHLOAuthService | None = None,
    ):
        self.account = account
        self.location_id = account.location_id
        self.ghl = ghl or GHLObjects(account.location_id, service=service)
        self.hcp = hcp or HCPClient(account.api_key)

    # --- contacts ------------------------------------------------------------------------

    def sync_contact_create(self, contact: dict) -> str:
        ghl_contact_id = mapping.text(contact.get('id'))
        if not ghl_contact_id:
            return IGNORED
        if IntegratedContact.objects.filter(
            location_id=self.location_id, ghl_contact_id=ghl_contact_id
        ).exists():
            # Phase 3 links the row in the same call that creates the GHL contact, before GHL's
            # own `ContactCreate` for it can even be sent - this is that echo, not a new contact.
            return IGNORED

        fields = reverse_mapping.contact_from_ghl(contact)
        if not reverse_mapping.contact_is_creatable(fields):
            return SKIPPED

        note = self._contact_note(contact)
        payload = reverse_mapping.customer_create_payload(contact, fields, note)
        customer = self.hcp.create_customer(payload)
        hcp_customer_id = mapping.text(customer.get('id'))
        if not hcp_customer_id:
            raise HCPError('HCP returned no customer id.')

        IntegratedContact.objects.update_or_create(
            location_id=self.location_id,
            ghl_contact_id=ghl_contact_id,
            defaults={'hcp_customer_id': hcp_customer_id, 'notes': note, **fields},
        )
        self._write_hcp_customer_id(ghl_contact_id, hcp_customer_id)
        return SYNCED_OUTCOME

    def sync_contact_update(self, contact: dict) -> str:
        ghl_contact_id = mapping.text(contact.get('id'))
        if not ghl_contact_id:
            return IGNORED
        existing = IntegratedContact.objects.filter(
            location_id=self.location_id, ghl_contact_id=ghl_contact_id
        ).first()
        if existing is None:
            # Not every GHL contact is an HCP customer; one only becomes linked through
            # `ContactCreate` (or the forward direction). An update for one that never was is
            # not this integration's business.
            return IGNORED

        fields = reverse_mapping.contact_from_ghl(contact)
        note = self._contact_note(contact)
        tags = contact.get('tags') if isinstance(contact.get('tags'), list) else []

        changed = {
            field: fields[field]
            for field in _CONTACT_FIELDS
            if fields[field] != (getattr(existing, field) or '')
        }
        if note != (existing.notes or ''):
            changed['notes'] = note
        tags_changed = tags != (existing.tags or [])

        # The local cache is brought current either way - this is what makes the echo of our
        # own HCP->GHL write compare clean next time, even on the very first pass.
        for field in _CONTACT_FIELDS:
            setattr(existing, field, fields[field])
        existing.notes = note
        if tags_changed:
            existing.tags = tags
        existing.save()

        if not changed and not tags_changed:
            return SKIPPED

        update_payload = reverse_mapping.customer_update_payload(changed)
        if tags_changed:
            update_payload['tags'] = tags
        if not update_payload:
            return SKIPPED

        self.hcp.update_customer(existing.hcp_customer_id, update_payload)
        return SYNCED_OUTCOME

    def _contact_note(self, contact: dict) -> str:
        field_id = self.ghl.contact_field_ids().get(HCP_CUSTOMER_NOTE_FIELD)
        if not field_id:
            return ''
        for field in contact.get('customFields') or []:
            if isinstance(field, dict) and mapping.text(field.get('id')) == field_id:
                return mapping.text(field.get('value'))
        return ''

    def _write_hcp_customer_id(self, ghl_contact_id: str, hcp_customer_id: str) -> None:
        """So a contact GHL created, not HCP, shows the same linking id a mirrored one gets.

        Best effort: the contact and the HCP customer both exist and are linked locally either
        way, and this write's own echo back is a custom field GHL has no `ContactUpdate`-visible
        change from that this app's diff would act on regardless.
        """
        field_id = self.ghl.contact_field_ids().get(HCP_CUSTOMER_ID_FIELD)
        if not field_id:
            return
        try:
            self.ghl.update_contact(
                ghl_contact_id, {'customFields': [{'id': field_id, 'field_value': hcp_customer_id}]}
            )
        except GHLError:
            logger.warning(
                'hcp: could not write the HCP customer id onto GHL contact %s',
                ghl_contact_id,
                extra={'context': {'location_id': self.location_id}},
            )

    def _contact_for_ghl_record(self, ghl_contact_id) -> IntegratedContact | None:
        ghl_contact_id = mapping.text(ghl_contact_id)
        if not ghl_contact_id:
            return None
        return IntegratedContact.objects.filter(
            location_id=self.location_id, ghl_contact_id=ghl_contact_id
        ).first()

    # --- leads / estimates / jobs (creation only) -----------------------------------------

    def sync_lead(self, record: dict, properties: dict) -> str:
        ghl_lead_record_id = mapping.text(record.get('id'))
        if not ghl_lead_record_id:
            return IGNORED
        if mapping.text(properties.get('external_lead_id')):
            return IGNORED
        if Lead.objects.filter(
            location_id=self.location_id, ghl_lead_record_id=ghl_lead_record_id
        ).exists():
            return IGNORED

        contact = self._contact_for_ghl_record(properties.get('contact_id'))
        if contact is None:
            return SKIPPED

        payload = reverse_mapping.lead_create_payload(properties, contact.hcp_customer_id)
        hcp_lead = self.hcp.create_lead(payload)
        hcp_lead_id = mapping.text(hcp_lead.get('id'))
        if not hcp_lead_id:
            raise HCPError('HCP returned no lead id.')

        Lead.objects.create(
            location_id=self.location_id,
            contact=contact,
            ghl_lead_record_id=ghl_lead_record_id,
            hcp_lead_id=hcp_lead_id,
        )
        return SYNCED_OUTCOME

    def sync_estimate(self, record: dict, properties: dict) -> str:
        ghl_estimate_record_id = mapping.text(record.get('id'))
        if not ghl_estimate_record_id:
            return IGNORED
        if mapping.text(properties.get('external_estimate_id')):
            return IGNORED
        if Estimate.objects.filter(
            location_id=self.location_id, ghl_estimate_record_id=ghl_estimate_record_id
        ).exists():
            return IGNORED

        contact = self._contact_for_ghl_record(properties.get('contact_id'))
        if contact is None:
            return SKIPPED

        payload = reverse_mapping.estimate_create_payload(properties, contact.hcp_customer_id)
        hcp_estimate = self.hcp.create_estimate(payload)
        hcp_estimate_id = mapping.text(hcp_estimate.get('id'))
        if not hcp_estimate_id:
            raise HCPError('HCP returned no estimate id.')

        Estimate.objects.create(
            location_id=self.location_id,
            contact=contact,
            ghl_estimate_record_id=ghl_estimate_record_id,
            hcp_estimate_id=hcp_estimate_id,
        )
        return SYNCED_OUTCOME

    def sync_job(self, record: dict, properties: dict) -> str:
        ghl_job_record_id = mapping.text(record.get('id'))
        if not ghl_job_record_id:
            return IGNORED
        if mapping.text(properties.get('hcp_job_id')):
            return IGNORED
        if Job.objects.filter(
            location_id=self.location_id, ghl_job_record_id=ghl_job_record_id
        ).exists():
            return IGNORED

        contact = self._contact_for_ghl_record(properties.get('contact_id'))
        if contact is None:
            return SKIPPED

        payload = reverse_mapping.job_create_payload(properties, contact.hcp_customer_id)
        hcp_job = self.hcp.create_job(payload)
        hcp_job_id = mapping.text(hcp_job.get('id'))
        if not hcp_job_id:
            raise HCPError('HCP returned no job id.')

        Job.objects.create(
            location_id=self.location_id,
            contact=contact,
            ghl_job_record_id=ghl_job_record_id,
            hcp_job_id=hcp_job_id,
            # The marker Phase 1 already carries for exactly this: a job this sync created,
            # not one it is mirroring - `sync_job` (Phase 3) stamps `SOURCE_HCP` on its own.
            source=SOURCE_GHL,
            sync_state=SYNCED,
        )
        return SYNCED_OUTCOME

    # --- job line items (genuinely two-way) -------------------------------------------------

    def sync_job_line_item(self, record: dict, properties: dict) -> str:
        ghl_line_item_record_id = mapping.text(record.get('id'))
        if not ghl_line_item_record_id:
            return IGNORED
        if mapping.text(properties.get('hcp_line_item_id')):
            # Phase 3 writes this property in the same call that creates or updates the GHL
            # record, so a non-empty value here is our own write, not a GHL-side edit.
            return IGNORED

        existing = JobLineItem.objects.filter(
            location_id=self.location_id, ghl_line_item_record_id=ghl_line_item_record_id
        ).first()
        if existing is not None:
            return self._update_job_line_item(existing, properties)
        return self._create_job_line_item(ghl_line_item_record_id, properties)

    def _create_job_line_item(self, ghl_line_item_record_id: str, properties: dict) -> str:
        job = Job.objects.filter(
            location_id=self.location_id,
            ghl_job_record_id=mapping.text(properties.get('job_record_id')),
        ).first()
        if job is None or not job.hcp_job_id:
            return SKIPPED

        payload = reverse_mapping.line_item_payload(properties)
        if payload is None:
            return SKIPPED

        result = self.hcp.create_job_line_item(job.hcp_job_id, payload)
        hcp_line_item_id = mapping.text(result.get('id'))
        if not hcp_line_item_id:
            raise HCPError('HCP returned no line item id.')

        JobLineItem.objects.create(
            location_id=self.location_id,
            job=job,
            hcp_line_item_id=hcp_line_item_id,
            ghl_line_item_record_id=ghl_line_item_record_id,
            source=SOURCE_GHL,
            sync_state=SYNCED,
            **mapping.line_item_fields(result),
        )
        self._write_back_hcp_totals(ghl_line_item_record_id, result)
        return SYNCED_OUTCOME

    def _update_job_line_item(self, existing: JobLineItem, properties: dict) -> str:
        candidate = reverse_mapping.line_item_payload(properties)
        if candidate is None:
            return SKIPPED
        changes = reverse_mapping.line_item_changes(candidate, existing)
        if not changes:
            return SKIPPED

        result = self.hcp.update_job_line_item(
            existing.job.hcp_job_id, existing.hcp_line_item_id, changes
        )
        if mapping.text(result.get('id')):
            for field, value in mapping.line_item_fields(result).items():
                setattr(existing, field, value)
            existing.save()
            self._write_back_hcp_totals(existing.ghl_line_item_record_id, result)
        return SYNCED_OUTCOME

    def delete_job_line_item(self, record: dict) -> str:
        ghl_line_item_record_id = mapping.text(record.get('id'))
        if not ghl_line_item_record_id:
            return IGNORED
        existing = JobLineItem.objects.filter(
            location_id=self.location_id, ghl_line_item_record_id=ghl_line_item_record_id
        ).first()
        if existing is None:
            return IGNORED

        self.hcp.delete_job_line_item(existing.job.hcp_job_id, existing.hcp_line_item_id)
        existing.delete()
        return SYNCED_OUTCOME

    def _write_back_hcp_totals(self, ghl_line_item_record_id: str, result: dict) -> None:
        """HCP's own recalculated quantity/price/cost/total, back onto the GHL record.

        HCP computes these (rounding, in particular) authoritatively; without this, the two
        would drift the moment HCP's number differs from what was sent by even a cent, and
        nothing else re-visits a line item outside a job event. Best effort, like every other
        GHL write this sync makes after the thing it is mirroring already exists.
        """
        properties = mapping.present(
            {
                'hcp_line_item_id': mapping.text(result.get('id')),
                'quantity': mapping.whole(result.get('quantity')),
                'unit_price': mapping.money(result.get('unit_price')),
                'unit_cost': mapping.money(result.get('unit_cost')),
                'total_price': mapping.money(
                    result.get('amount')
                    if result.get('amount') is not None
                    else result.get('total_price')
                ),
            }
        )
        if not properties:
            return
        try:
            self.ghl.update_record(JOB_LINE_ITEMS_OBJECT, ghl_line_item_record_id, properties)
        except GHLError:
            logger.warning(
                'hcp: could not write HCP totals back onto GHL line item %s',
                ghl_line_item_record_id,
                extra={'context': {'location_id': self.location_id}},
            )

    # --- appointments -----------------------------------------------------------------------

    def sync_appointment(self, payload: dict) -> str:
        """Route one `AppointmentUpdate`/`AppointmentDelete` by what it is already linked to.

        The calendar the reference checks `calendarId` against to tell an estimate appointment
        from a job one is redundant here: `AppointmentIntegration.appointment_type`, on the row
        this is already looked up by `ghl_appointment_id`, says the same thing without a second
        lookup.
        """
        appointment = (
            payload.get('appointment') if isinstance(payload.get('appointment'), dict) else {}
        )
        ghl_appointment_id = mapping.text(appointment.get('id'))
        if not ghl_appointment_id:
            return IGNORED

        row = AppointmentIntegration.objects.filter(
            location_id=self.location_id, ghl_appointment_id=ghl_appointment_id
        ).first()
        if row is None:
            return IGNORED

        event_type = payload.get('type')
        if row.appointment_type != APPOINTMENT_JOB:
            # Estimate appointment creation/cancellation/completion stays HCP-only; GHL can only
            # move its time or technician, never delete it from here.
            if event_type != constants.GHL_APPOINTMENT_UPDATE:
                return IGNORED
            return self._sync_estimate_appointment(row, appointment)

        if event_type == constants.GHL_APPOINTMENT_DELETE:
            return self._delete_job_appointment(row)
        return self._sync_job_appointment(row, appointment)

    def _delete_job_appointment(self, row: AppointmentIntegration) -> str:
        try:
            self.hcp.delete_job_appointment(row.hcp_job_id, row.hcp_appointment_id)
        except HCPError:
            # Already gone in HCP is the state we wanted; dropping the local link anyway keeps
            # a later re-book from trying to update an appointment that is not there.
            logger.warning(
                'hcp: could not delete HCP appointment %s; dropping the link anyway',
                row.hcp_appointment_id,
                extra={'context': {'location_id': self.location_id}},
            )
        row.delete()
        return SYNCED_OUTCOME

    def _sync_job_appointment(self, row: AppointmentIntegration, appointment: dict) -> str:
        starts_at, ends_at, updated_at = reverse_mapping.appointment_times(appointment)
        if self._is_stale(row, updated_at):
            return SKIPPED

        emp_mapping = self._mapped_employee(appointment)
        has_change = self._apply_appointment_changes(row, starts_at, ends_at, emp_mapping)
        if not has_change or not row.starts_at or not row.ends_at:
            return SKIPPED

        update_payload = {
            'start_time': row.starts_at.isoformat(),
            'end_time': row.ends_at.isoformat(),
        }
        if emp_mapping and not row.used_default_employee:
            update_payload['dispatched_employees_ids'] = [emp_mapping.hcp_employee_id]
        else:
            # HCP refuses a job appointment update with no employee on it at all (verified
            # live), so with no mapped technician of our own, whichever HCP already has is
            # resent unchanged - the schedule still moves, the assignment does not.
            existing_ids = self._current_job_employee_ids(row)
            if not existing_ids:
                return SKIPPED
            update_payload['dispatched_employees_ids'] = existing_ids

        row.external_updated_at = updated_at
        row.save()
        self.hcp.update_job_appointment(row.hcp_job_id, row.hcp_appointment_id, update_payload)
        return SYNCED_OUTCOME

    def _sync_estimate_appointment(self, row: AppointmentIntegration, appointment: dict) -> str:
        starts_at, ends_at, updated_at = reverse_mapping.appointment_times(appointment)
        if self._is_stale(row, updated_at):
            return SKIPPED

        emp_mapping = self._mapped_employee(appointment)
        has_change = self._apply_appointment_changes(row, starts_at, ends_at, emp_mapping)
        if not has_change or not row.starts_at:
            return SKIPPED

        estimate = Estimate.objects.filter(
            location_id=self.location_id, hcp_estimate_id=row.hcp_estimate_id
        ).first()
        option = estimate.options.first() if estimate else None
        if option is None:
            return SKIPPED

        update_payload = {'start_time': row.starts_at.isoformat()}
        if row.ends_at:
            update_payload['end_time'] = row.ends_at.isoformat()
        if emp_mapping and not row.used_default_employee:
            update_payload['dispatched_employees'] = [{'employee_id': emp_mapping.hcp_employee_id}]

        row.external_updated_at = updated_at
        row.save()
        self.hcp.update_estimate_option_schedule(
            row.hcp_estimate_id, option.hcp_option_id, update_payload
        )
        return SYNCED_OUTCOME

    def _mapped_employee(self, appointment: dict) -> HCPEmployeeMapping | None:
        ghl_user_id = mapping.text(appointment.get('assignedUserId'))
        if not ghl_user_id:
            return None
        return HCPEmployeeMapping.objects.filter(
            location_id=self.location_id, ghl_user_id=ghl_user_id
        ).first()

    @staticmethod
    def _is_stale(row: AppointmentIntegration, updated_at) -> bool:
        return bool(
            row.external_updated_at and updated_at and updated_at <= row.external_updated_at
        )

    @staticmethod
    def _apply_appointment_changes(row, starts_at, ends_at, emp_mapping) -> bool:
        """Diff GHL's values against the stored row, writing in place. `True` if anything of
        what GHL can legally move actually changed.

        The technician half is gated on `not row.used_default_employee` even for *detecting* a
        change, matching the live-verified job behaviour: an appointment standing in for "nobody
        was assigned" must not be re-assigned from here just because a round-robin slot changed
        who the calendar shows.
        """
        changed = False
        if starts_at and row.starts_at != starts_at:
            row.starts_at = starts_at
            changed = True
        if ends_at and row.ends_at != ends_at:
            row.ends_at = ends_at
            changed = True
        if (
            emp_mapping
            and row.assigned_employee_id != emp_mapping.pk
            and not row.used_default_employee
        ):
            row.assigned_employee = emp_mapping
            changed = True
        return changed

    def _current_job_employee_ids(self, row: AppointmentIntegration) -> list[str]:
        for appt in self.hcp.get_job_appointments(row.hcp_job_id):
            if not isinstance(appt, dict) or mapping.text(appt.get('id')) != row.hcp_appointment_id:
                continue
            ids = [
                mapping.text(e)
                for e in (appt.get('dispatched_employees_ids') or [])
                if mapping.text(e)
            ]
            if ids:
                return ids
            return [
                mapping.text(e.get('id'))
                for e in (appt.get('dispatched_employees') or [])
                if isinstance(e, dict) and mapping.text(e.get('id'))
            ]
        return []
