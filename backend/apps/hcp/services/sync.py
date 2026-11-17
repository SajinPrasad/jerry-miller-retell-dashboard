"""Mirroring Housecall Pro into GoHighLevel: one handler per entity, all of them upserts.

Every handler is **idempotent on `(location_id, hcp_*_id)`**. That is not a nicety - it is what
makes the whole design work:

- HCP sends several events for the same entity (`job.created`, then `job.scheduled`, then
  `job.started`), each carrying the whole object. Handling them all through one upsert means
  there is no per-event patching to get wrong.
- A delivery that fails can be replayed from `apps/audit` without double-writing.
- The historical backfill (a later phase) can call these same handlers for the same records the
  webhooks are already delivering, and the second one through changes nothing.

The local row is written **before** the GHL write, with its `ghl_*_record_id` left NULL. That is
the race-condition guard the models were built for: two deliveries for the same new job both
look it up, one creates the row and the other finds it, so only one of them creates the GHL
record. NULLs are distinct to the database, so two different jobs each waiting on their GHL half
never collide on the unique constraint - see `Lead`'s docstring in `models.py`.

Nothing here raises for an entity it cannot mirror. A customer with no name, a job whose
estimate has not arrived yet - those are facts about the data, returned as an outcome string the
webhook record keeps. Only an actual failure (GHL rejecting a write, HCP unreachable) raises, so
the delivery is marked failed and can be replayed once the cause is fixed.
"""

import logging

from django.db import transaction

from apps.common import backoff
from apps.ghl.exceptions import GHLError
from apps.ghl.services import GHLOAuthService
from apps.hcp.constants import (
    ESTIMATES_OBJECT,
    JOB_LINE_ITEMS_OBJECT,
    JOBS_OBJECT,
    LEADS_OBJECT,
)
from apps.hcp.models import (
    APPOINTMENT_ESTIMATE,
    APPOINTMENT_JOB,
    PENDING,
    SOURCE_HCP,
    SYNCED,
    AppointmentIntegration,
    Estimate,
    EstimateJobRelation,
    EstimateOption,
    HCPAccount,
    HCPCalendarSettings,
    HCPEmployeeMapping,
    IntegratedContact,
    Job,
    JobLineItem,
    Lead,
    LeadEstimateConversion,
    LeadJobConversion,
)
from apps.hcp.services import mapping
from apps.hcp.services.ghl_objects import GHLObjects
from apps.hcp.services.hcp_client import HCPClient

logger = logging.getLogger(__name__)

# Outcomes a handler can report. `synced` means GHL was written; the rest say why it was not,
# and none of them is an error.
SYNCED_OUTCOME = 'synced'
SKIPPED = 'skipped'
IGNORED = 'ignored'


class HCPToGHLSync:
    """One connected sub-account's mirror into GoHighLevel.

    Built per delivery rather than kept around: it caches GHL's per-sub-account object and
    association ids, and a snapshot repaired mid-flight has to take effect without a restart.
    """

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
        self.hcp = hcp or HCPClient(account.api_key, max_retries=backoff.MAX_RETRIES)

    # --- customers ---------------------------------------------------------------------

    def sync_customer(self, customer: dict) -> str:
        """An HCP customer as a GHL contact, plus the `IntegratedContact` that links them.

        The contact is matched by HCP customer id first (our own link), then by email. The email
        fallback is what stops a customer who already exists in GHL - entered by hand, or
        created by a form - from becoming a second contact for the same person.
        """
        hcp_customer_id = mapping.text(customer.get('id'))
        if not hcp_customer_id:
            return IGNORED

        payload = mapping.contact_payload(customer, self.ghl.contact_field_ids())
        if not mapping.contact_is_identifiable(payload):
            logger.info(
                'hcp: customer %s has no name, email or phone; not mirrored',
                hcp_customer_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return SKIPPED

        existing = IntegratedContact.objects.filter(
            location_id=self.location_id, hcp_customer_id=hcp_customer_id
        ).first()

        contact_id = existing.ghl_contact_id if existing else ''
        if not contact_id and payload.get('email'):
            contact_id = self.ghl.find_contact_by_email(payload['email'])

        if contact_id:
            self.ghl.update_contact(contact_id, payload)
        else:
            contact_id = self.ghl.create_contact(payload)

        address = mapping.address_of(customer)
        IntegratedContact.objects.update_or_create(
            location_id=self.location_id,
            hcp_customer_id=hcp_customer_id,
            defaults={
                'ghl_contact_id': contact_id,
                'first_name': mapping.text(customer.get('first_name')),
                'last_name': mapping.text(customer.get('last_name')),
                'company_name': mapping.text(customer.get('company')),
                'email': mapping.text(customer.get('email')),
                'mobile_number': mapping.text(customer.get('mobile_number')),
                'home_number': mapping.text(customer.get('home_number')),
                'work_number': mapping.text(customer.get('work_number')),
                'source': mapping.text(customer.get('lead_source')),
                'tags': customer.get('tags') if isinstance(customer.get('tags'), list) else [],
                **mapping.address_fields(address),
            },
        )
        return SYNCED_OUTCOME

    def contact_for(self, entity: dict) -> IntegratedContact | None:
        """The `IntegratedContact` for whatever customer an entity hangs off.

        Syncs the customer first when it has not been seen. HCP does not guarantee the customer
        event arrives before the job that references it, and a job whose contact does not exist
        yet cannot be associated to one - so the nested customer on the payload is used to
        create it rather than dropping the job.
        """
        customer = entity.get('customer')
        customer = customer if isinstance(customer, dict) else {}
        hcp_customer_id = mapping.text(customer.get('id')) or mapping.text(
            entity.get('customer_id')
        )
        if not hcp_customer_id:
            return None

        contact = IntegratedContact.objects.filter(
            location_id=self.location_id, hcp_customer_id=hcp_customer_id
        ).first()
        if contact is not None:
            return contact

        if not customer:
            customer = self.hcp.get_customer(hcp_customer_id)
        if self.sync_customer(customer) != SYNCED_OUTCOME:
            return None
        return IntegratedContact.objects.filter(
            location_id=self.location_id, hcp_customer_id=hcp_customer_id
        ).first()

    # --- leads -------------------------------------------------------------------------

    def sync_lead(self, lead: dict) -> str:
        hcp_lead_id = mapping.text(lead.get('id'))
        if not hcp_lead_id:
            return IGNORED

        contact = self.contact_for(lead)
        if contact is None:
            return SKIPPED

        address = mapping.address_of(lead)
        row, _ = Lead.objects.update_or_create(
            location_id=self.location_id,
            hcp_lead_id=hcp_lead_id,
            defaults={
                'contact': contact,
                'hcp_lead_number': mapping.whole(lead.get('lead_number')),
                'hcp_lead_source': mapping.text(lead.get('lead_source')),
                'hcp_status': mapping.text(lead.get('status')),
                'hcp_pipeline_status': mapping.text(lead.get('pipeline_status')),
                'hcp_tags': lead.get('tags') if isinstance(lead.get('tags'), list) else [],
                'total_amount': mapping.text(lead.get('estimated_value')),
                'assigned_employee': self.assigned_mapping(lead),
                **mapping.address_fields(address),
            },
        )

        properties = mapping.lead_properties(lead, contact.ghl_contact_id)
        row.ghl_lead_record_id = self._upsert_record(
            LEADS_OBJECT, row.ghl_lead_record_id, properties
        )
        if not row.ghl_relation_id:
            row.ghl_relation_id = self._relate_to_contact(
                LEADS_OBJECT, row.ghl_lead_record_id, contact
            )
        row.save(update_fields=['ghl_lead_record_id', 'ghl_relation_id'])
        return SYNCED_OUTCOME

    # --- estimates ---------------------------------------------------------------------

    def sync_estimate(self, estimate: dict) -> str:
        hcp_estimate_id = mapping.text(estimate.get('id'))
        if not hcp_estimate_id:
            return IGNORED

        contact = self.contact_for(estimate)
        if contact is None:
            return SKIPPED

        lead = self._lead_for(estimate)
        address = mapping.address_of(estimate)
        schedule = estimate.get('schedule') if isinstance(estimate.get('schedule'), dict) else {}
        stamps = (
            estimate.get('work_timestamps')
            if isinstance(estimate.get('work_timestamps'), dict)
            else {}
        )

        with transaction.atomic():
            row, _ = Estimate.objects.update_or_create(
                location_id=self.location_id,
                hcp_estimate_id=hcp_estimate_id,
                defaults={
                    'contact': contact,
                    'lead': lead,
                    'hcp_estimate_number': mapping.text(estimate.get('estimate_number')),
                    'hcp_estimate_status': mapping.text(estimate.get('work_status')),
                    'hcp_lead_source': mapping.text(estimate.get('lead_source')),
                    'schedule_start_at': mapping.text(schedule.get('scheduled_start')),
                    'schedule_end_at': mapping.text(schedule.get('scheduled_end')),
                    'arrival_window': mapping.whole(schedule.get('arrival_window')) or 0,
                    'technician_en_route_at': mapping.text(stamps.get('on_my_way_at')),
                    'work_started_at': mapping.text(stamps.get('started_at')),
                    'work_completed_at': mapping.text(stamps.get('completed_at')),
                    **mapping.address_fields(address),
                },
            )
            self._store_options(row, estimate)

        properties = mapping.estimate_properties(estimate, contact.ghl_contact_id)
        row.ghl_estimate_record_id = self._upsert_record(
            ESTIMATES_OBJECT, row.ghl_estimate_record_id, properties
        )
        if not row.ghl_relation_id:
            row.ghl_relation_id = self._relate_to_contact(
                ESTIMATES_OBJECT, row.ghl_estimate_record_id, contact
            )
        if lead and lead.ghl_lead_record_id and not row.ghl_lead_relation_id:
            row.ghl_lead_relation_id = self._relate(
                LEADS_OBJECT, ESTIMATES_OBJECT, lead.ghl_lead_record_id, row.ghl_estimate_record_id
            )
            LeadEstimateConversion.objects.get_or_create(
                location_id=self.location_id,
                lead=lead,
                hcp_estimate_id=hcp_estimate_id,
                defaults={'hcp_estimate_option_id': ''},
            )
        row.save(
            update_fields=['ghl_estimate_record_id', 'ghl_relation_id', 'ghl_lead_relation_id']
        )

        # An estimate carries its own schedule rather than sending separate appointment events
        # the way a job does, so booking it belongs here, after the record exists.
        self.sync_estimate_appointment(estimate)
        return SYNCED_OUTCOME

    def _store_options(self, estimate_row: Estimate, estimate: dict) -> None:
        """Mirror the estimate's options locally, and retire any HCP no longer lists.

        Deleted rather than left behind: an option withdrawn in HCP must not keep showing as
        approvable here, and the option set is small enough that replacing it is honest.
        """
        options = estimate.get('options') if isinstance(estimate.get('options'), list) else []
        seen = []
        for option in options:
            if not isinstance(option, dict) or not mapping.text(option.get('id')):
                continue
            hcp_option_id = mapping.text(option.get('id'))
            seen.append(hcp_option_id)
            EstimateOption.objects.update_or_create(
                estimate=estimate_row,
                hcp_option_id=hcp_option_id,
                defaults={
                    'hcp_option_name': mapping.text(option.get('name'))[:155],
                    'hcp_option_number': mapping.text(option.get('option_number')),
                    'total_amount': mapping.text(option.get('total_amount')),
                    'approval_status': mapping.text(option.get('approval_status')),
                    'status': mapping.text(option.get('status'))[:155],
                    'tags': option.get('tags') if isinstance(option.get('tags'), list) else [],
                    'notes': option.get('notes') if isinstance(option.get('notes'), list) else [],
                    'line_items': option.get('line_items')
                    if isinstance(option.get('line_items'), list)
                    else [],
                },
            )
        if options:
            estimate_row.options.exclude(hcp_option_id__in=seen).delete()

    # --- jobs ----------------------------------------------------------------------------

    def sync_job(self, job: dict) -> str:
        hcp_job_id = mapping.text(job.get('id'))
        if not hcp_job_id:
            return IGNORED

        contact = self.contact_for(job)
        if contact is None:
            return SKIPPED

        lead = self._lead_for(job)
        address = mapping.address_of(job)
        schedule = job.get('schedule') if isinstance(job.get('schedule'), dict) else {}
        stamps = job.get('work_timestamps') if isinstance(job.get('work_timestamps'), dict) else {}

        row, _ = Job.objects.update_or_create(
            location_id=self.location_id,
            hcp_job_id=hcp_job_id,
            defaults={
                'contact': contact,
                'lead': lead,
                'hcp_invoice_number': mapping.text(job.get('invoice_number')),
                'hcp_description': mapping.text(job.get('description')),
                'status': mapping.text(job.get('work_status')),
                'lead_source': mapping.text(job.get('lead_source')),
                'total_amount': mapping.text(job.get('total_amount')),
                'outstanding_balance': mapping.text(job.get('outstanding_balance')),
                'job_type': mapping.job_type(job),
                'tags': job.get('tags') if isinstance(job.get('tags'), list) else [],
                'notes': job.get('notes') if isinstance(job.get('notes'), list) else [],
                'line_items': job.get('line_items')
                if isinstance(job.get('line_items'), list)
                else [],
                'invoices': job.get('invoices') if isinstance(job.get('invoices'), list) else [],
                'schedule_start_at': mapping.text(schedule.get('scheduled_start')),
                'schedule_end_at': mapping.text(schedule.get('scheduled_end')),
                'arrival_window': mapping.whole(schedule.get('arrival_window')) or 0,
                'technician_en_route_at': mapping.text(stamps.get('on_my_way_at')),
                'work_started_at': mapping.text(stamps.get('started_at')),
                'work_completed_at': mapping.text(stamps.get('completed_at')),
                'source': SOURCE_HCP,
                'sync_state': PENDING,
                **mapping.address_fields(address),
            },
        )

        properties = mapping.job_properties(job, contact.ghl_contact_id)
        row.ghl_job_record_id = self._upsert_record(
            JOBS_OBJECT, row.ghl_job_record_id, properties, owners=self._owners(job)
        )
        if not row.ghl_relation_id:
            row.ghl_relation_id = self._relate_to_contact(
                JOBS_OBJECT, row.ghl_job_record_id, contact
            )
        if lead and lead.ghl_lead_record_id and not row.ghl_lead_relation_id:
            row.ghl_lead_relation_id = self._relate(
                LEADS_OBJECT, JOBS_OBJECT, lead.ghl_lead_record_id, row.ghl_job_record_id
            )
            LeadJobConversion.objects.get_or_create(
                location_id=self.location_id, lead=lead, hcp_job_id=hcp_job_id
            )
        row.sync_state = SYNCED
        row.save(
            update_fields=[
                'ghl_job_record_id',
                'ghl_relation_id',
                'ghl_lead_relation_id',
                'sync_state',
            ]
        )

        self._link_estimates(row, job)
        return SYNCED_OUTCOME

    def _link_estimates(self, job_row: Job, job: dict) -> None:
        """Associate a job with the estimates it came from, where both sides exist in GHL.

        Many-to-many per the snapshot spec: one job can come from several estimates, and one
        estimate can produce several jobs.
        """
        estimate_ids = job.get('estimate_ids')
        if not isinstance(estimate_ids, list):
            single = mapping.text(job.get('estimate_id'))
            estimate_ids = [single] if single else []

        for estimate_id in estimate_ids:
            estimate_id = mapping.text(estimate_id)
            if not estimate_id:
                continue
            estimate = Estimate.objects.filter(
                location_id=self.location_id, hcp_estimate_id=estimate_id
            ).first()
            if estimate is None or not estimate.ghl_estimate_record_id:
                continue
            relation, created = EstimateJobRelation.objects.get_or_create(
                location_id=self.location_id, estimate=estimate, job=job_row
            )
            if created and job_row.ghl_job_record_id:
                relation.ghl_relation_id = self._relate(
                    ESTIMATES_OBJECT,
                    JOBS_OBJECT,
                    estimate.ghl_estimate_record_id,
                    job_row.ghl_job_record_id,
                )
                relation.save(update_fields=['ghl_relation_id'])

    # --- job line items ---------------------------------------------------------------

    def reconcile_line_items(self, job_row: Job, line_items: list | None = None) -> dict:
        """Make GHL's line items for one job match HCP's, exactly.

        A three-way diff, which is why it is its own method rather than part of `sync_job`:
        HCP's current list, our mirrored rows, and GHL's records. Items HCP no longer has are
        deleted in GHL as well as here - a line item removed from a job in HCP that lingers in
        GHL is a quote the customer can still see.

        `line_items` is read from HCP when not supplied, because several `job.*` events carry a
        trimmed job object with no line items at all, and an empty list there means "this event
        did not mention them", not "the job has none".
        """
        if line_items is None:
            line_items = self.hcp.get_job_line_items(job_row.hcp_job_id)

        wanted = {
            mapping.text(item.get('id')): item
            for item in line_items
            if isinstance(item, dict) and mapping.text(item.get('id'))
        }
        existing = {
            row.hcp_line_item_id: row
            for row in JobLineItem.objects.filter(location_id=self.location_id, job=job_row)
        }

        created = updated = deleted = 0
        for hcp_line_item_id, item in wanted.items():
            row = existing.get(hcp_line_item_id)
            properties = mapping.line_item_properties(item, job_row.ghl_job_record_id or '')
            record_id = self._upsert_record(
                JOB_LINE_ITEMS_OBJECT, row.ghl_line_item_record_id if row else '', properties
            )
            fields = mapping.line_item_fields(item)
            JobLineItem.objects.update_or_create(
                location_id=self.location_id,
                job=job_row,
                hcp_line_item_id=hcp_line_item_id,
                defaults={
                    **fields,
                    'ghl_line_item_record_id': record_id,
                    'source': SOURCE_HCP,
                    'sync_state': SYNCED,
                },
            )
            if row is None:
                created += 1
                if job_row.ghl_job_record_id:
                    self._relate(
                        JOBS_OBJECT,
                        JOB_LINE_ITEMS_OBJECT,
                        job_row.ghl_job_record_id,
                        record_id,
                    )
            else:
                updated += 1

        for hcp_line_item_id, row in existing.items():
            if hcp_line_item_id in wanted:
                continue
            if row.ghl_line_item_record_id:
                self._delete_record(JOB_LINE_ITEMS_OBJECT, row.ghl_line_item_record_id)
            row.delete()
            deleted += 1

        return {'created': created, 'updated': updated, 'deleted': deleted}

    # --- appointments -------------------------------------------------------------------

    def sync_job_appointment(self, job: dict, appointment: dict) -> str:
        """Book, move or re-assign one HCP job appointment on the GHL job calendar.

        Needs three things configured before it can do anything: the job calendar, a technician
        (mapped, or the calendar's fallback), and the contact. A missing one is reported, not
        raised - the job itself has already synced, and this is the part the SOP warns fails
        quietly.
        """
        hcp_appointment_id = mapping.text(appointment.get('id'))
        hcp_job_id = mapping.text(job.get('id'))
        if not hcp_appointment_id or not hcp_job_id:
            return IGNORED

        settings_row = HCPCalendarSettings.objects.filter(location_id=self.location_id).first()
        if settings_row is None or not settings_row.job_calendar_id:
            logger.warning(
                'hcp: no job calendar configured for %s; appointment %s not booked',
                self.location_id,
                hcp_appointment_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return SKIPPED

        contact = self.contact_for(job)
        if contact is None:
            return SKIPPED

        assigned = self.assigned_mapping(appointment) or self.assigned_mapping(job)
        used_default = assigned is None
        if assigned is None:
            assigned = settings_row.job_appointment_default_user
        if assigned is None:
            logger.warning(
                'hcp: appointment %s has no mapped technician and %s has no fallback user',
                hcp_appointment_id,
                self.location_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return SKIPPED

        starts_at = mapping.text(appointment.get('start_time')) or mapping.text(
            appointment.get('scheduled_start')
        )
        if not starts_at:
            return SKIPPED
        ends_at = mapping.text(appointment.get('end_time')) or mapping.text(
            appointment.get('scheduled_end')
        )
        address = mapping.address_of(job)

        return self._book(
            key={
                'appointment_type': APPOINTMENT_JOB,
                'hcp_appointment_id': hcp_appointment_id,
            },
            calendar_id=settings_row.job_calendar_id,
            contact=contact,
            assigned=assigned,
            used_default=used_default,
            title=_job_title(job),
            starts_at=starts_at,
            ends_at=ends_at,
            address=address,
            status=mapping.text(appointment.get('status')),
            extra={
                'hcp_job_id': hcp_job_id,
                'hcp_job_number': mapping.text(job.get('invoice_number')),
            },
        )

    def sync_estimate_appointment(self, estimate: dict) -> str:
        """Book or move the one appointment an HCP estimate carries, on the estimate calendar.

        Unlike a job - which can be visited several times and sends its own
        `job.appointment.*` events - an estimate has **one** schedule, carried on the estimate
        itself and arriving as part of `estimate.scheduled` / `estimate.updated`. So there is no
        appointment id of its own: the estimate's id is the key, which is exactly what
        `unique_estimate_schedule_per_location` enforces.

        An estimate whose schedule HCP has cleared is unbooked rather than left behind - a
        consultation the customer can still see in their calendar after it was called off is
        worse than one that quietly disappears.
        """
        hcp_estimate_id = mapping.text(estimate.get('id'))
        if not hcp_estimate_id:
            return IGNORED

        schedule = estimate.get('schedule') if isinstance(estimate.get('schedule'), dict) else {}
        starts_at = mapping.text(schedule.get('scheduled_start'))
        if not starts_at:
            return self._unbook_estimate(hcp_estimate_id)

        settings_row = HCPCalendarSettings.objects.filter(location_id=self.location_id).first()
        if settings_row is None or not settings_row.estimate_calendar_id:
            logger.warning(
                'hcp: no estimate calendar configured for %s; estimate %s not booked',
                self.location_id,
                hcp_estimate_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return SKIPPED

        contact = self.contact_for(estimate)
        if contact is None:
            return SKIPPED

        assigned = self.assigned_mapping(estimate)
        used_default = assigned is None
        if assigned is None:
            assigned = settings_row.estimate_appointment_default_user
        if assigned is None:
            logger.warning(
                'hcp: estimate %s has no mapped technician and %s has no fallback user',
                hcp_estimate_id,
                self.location_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return SKIPPED

        number = mapping.text(estimate.get('estimate_number'))
        return self._book(
            key={
                'appointment_type': APPOINTMENT_ESTIMATE,
                'hcp_estimate_id': hcp_estimate_id,
            },
            calendar_id=settings_row.estimate_calendar_id,
            contact=contact,
            assigned=assigned,
            used_default=used_default,
            title=f'Estimate #{number}' if number else 'Estimate',
            starts_at=starts_at,
            ends_at=mapping.text(schedule.get('scheduled_end')),
            address=mapping.address_of(estimate),
            status=mapping.text(estimate.get('work_status')),
            extra={'hcp_estimate_number': number},
        )

    def _unbook_estimate(self, hcp_estimate_id: str) -> str:
        row = AppointmentIntegration.objects.filter(
            location_id=self.location_id,
            appointment_type=APPOINTMENT_ESTIMATE,
            hcp_estimate_id=hcp_estimate_id,
        ).first()
        if row is None:
            return IGNORED
        if row.ghl_appointment_id:
            try:
                self.ghl.delete_appointment(row.ghl_appointment_id)
            except GHLError:
                logger.warning(
                    'hcp: GHL appointment %s could not be deleted; dropping the link anyway',
                    row.ghl_appointment_id,
                    extra={'context': {'location_id': self.location_id}},
                )
        row.delete()
        return SYNCED_OUTCOME

    def _book(
        self,
        *,
        key: dict,
        calendar_id: str,
        contact: IntegratedContact,
        assigned: HCPEmployeeMapping,
        used_default: bool,
        title: str,
        starts_at: str,
        ends_at: str,
        address: dict,
        status: str,
        extra: dict,
    ) -> str:
        """Create or move one GHL booking, and record the link.

        Shared by the job and estimate paths, which differ only in which calendar they use and
        what identifies the appointment - `key` is what tells those two apart, and it is the
        same lookup the `AppointmentIntegration` constraints are built on.
        """
        payload = mapping.appointment_payload(
            calendar_id=calendar_id,
            contact_id=contact.ghl_contact_id,
            assigned_user_id=assigned.ghl_user_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            address=mapping.address_line(address),
        )

        row = AppointmentIntegration.objects.filter(location_id=self.location_id, **key).first()
        if row and row.ghl_appointment_id:
            self.ghl.update_appointment(row.ghl_appointment_id, payload)
            ghl_appointment_id = row.ghl_appointment_id
        else:
            ghl_appointment_id = self.ghl.create_appointment(payload)

        AppointmentIntegration.objects.update_or_create(
            location_id=self.location_id,
            **key,
            defaults={
                'ghl_appointment_id': ghl_appointment_id,
                'assigned_employee': assigned,
                'used_default_employee': used_default,
                'starts_at': starts_at,
                'ends_at': ends_at or None,
                'status': status,
                'address': mapping.address_line(address),
                **extra,
            },
        )
        return SYNCED_OUTCOME

    def remove_job_appointment(self, appointment: dict) -> str:
        """Unbook an appointment HCP discarded or cancelled."""
        hcp_appointment_id = mapping.text(appointment.get('id'))
        if not hcp_appointment_id:
            return IGNORED

        row = AppointmentIntegration.objects.filter(
            location_id=self.location_id,
            appointment_type=APPOINTMENT_JOB,
            hcp_appointment_id=hcp_appointment_id,
        ).first()
        if row is None:
            return IGNORED

        if row.ghl_appointment_id:
            try:
                self.ghl.delete_appointment(row.ghl_appointment_id)
            except GHLError:
                # Already gone in GHL is the state we wanted. Dropping the local row anyway
                # keeps a later re-book from trying to update an appointment that is not there.
                logger.warning(
                    'hcp: GHL appointment %s could not be deleted; dropping the link anyway',
                    row.ghl_appointment_id,
                    extra={'context': {'location_id': self.location_id}},
                )
        row.delete()
        return SYNCED_OUTCOME

    # --- shared ---------------------------------------------------------------------------

    def assigned_mapping(self, payload: dict) -> HCPEmployeeMapping | None:
        """The mapped GHL user for whoever HCP assigned, or `None`.

        HCP names them differently per entity (`assigned_employees` on a job,
        `dispatched_employees` on an appointment, `assigned_employee` singular on a lead), so
        all three are read. The first one that has a mapping wins - GHL assigns an appointment
        to one user, and round-robin picks among the calendar team for the rest.
        """
        candidates: list[dict] = []
        for key in ('dispatched_employees', 'assigned_employees'):
            value = payload.get(key)
            if isinstance(value, list):
                candidates += [item for item in value if isinstance(item, dict)]
        single = payload.get('assigned_employee')
        if isinstance(single, dict):
            candidates.append(single)

        for employee in candidates:
            employee_id = mapping.text(employee.get('id'))
            if not employee_id:
                continue
            found = HCPEmployeeMapping.objects.filter(
                location_id=self.location_id, hcp_employee_id=employee_id
            ).first()
            if found is not None:
                return found
        return None

    def _owners(self, payload: dict) -> list[str]:
        """Every mapped GHL user among the assigned employees, for a record's `owners`."""
        employees = payload.get('assigned_employees')
        if not isinstance(employees, list):
            return []
        ids = [
            mapping.text(employee.get('id'))
            for employee in employees
            if isinstance(employee, dict) and mapping.text(employee.get('id'))
        ]
        if not ids:
            return []
        return list(
            HCPEmployeeMapping.objects.filter(
                location_id=self.location_id, hcp_employee_id__in=ids
            ).values_list('ghl_user_id', flat=True)
        )

    def _lead_for(self, entity: dict) -> Lead | None:
        lead_id = mapping.text(entity.get('lead_id'))
        if not lead_id:
            lead = entity.get('lead')
            lead_id = mapping.text(lead.get('id')) if isinstance(lead, dict) else ''
        if not lead_id:
            return None
        return Lead.objects.filter(location_id=self.location_id, hcp_lead_id=lead_id).first()

    def _upsert_record(
        self, object_key: str, record_id: str | None, properties: dict, owners: list | None = None
    ) -> str:
        if record_id:
            return self.ghl.update_record(object_key, record_id, properties, owners=owners)
        return self.ghl.create_record(object_key, properties, owners=owners)

    def _delete_record(self, object_key: str, record_id: str) -> None:
        try:
            self.ghl.delete_record(object_key, record_id)
        except GHLError:
            # Already deleted in GHL is the state we were aiming for; anything else is still
            # not worth failing a whole reconciliation over one stale record.
            logger.warning(
                'hcp: could not delete %s record %s',
                object_key,
                record_id,
                extra={'context': {'location_id': self.location_id}},
            )

    def _relate_to_contact(
        self, object_key: str, record_id: str, contact: IntegratedContact
    ) -> str:
        """Associate a custom-object record with its GHL contact.

        A failure here is logged, not raised: the record itself is in GHL and correct, and an
        association that can be added later is worth less than losing the record over.
        """
        try:
            return self.ghl.create_relation(
                'contact', object_key, contact.ghl_contact_id, record_id
            )
        except GHLError:
            logger.warning(
                'hcp: could not associate %s %s with contact %s',
                object_key,
                record_id,
                contact.ghl_contact_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return ''

    def _relate(self, first_key: str, second_key: str, first_id: str, second_id: str) -> str:
        try:
            return self.ghl.create_relation(first_key, second_key, first_id, second_id)
        except GHLError:
            logger.warning(
                'hcp: could not associate %s %s with %s %s',
                first_key,
                first_id,
                second_key,
                second_id,
                extra={'context': {'location_id': self.location_id}},
            )
            return ''


def _job_title(job: dict) -> str:
    """What the booking is called on the GHL calendar.

    The invoice number is what both the office and the customer say out loud, so it leads. A
    job that has not been invoiced yet has none, and `Job #` with nothing after it reads as
    broken - so that case is just `Job`.
    """
    number = mapping.text(job.get('invoice_number'))
    return f'Job #{number}' if number else 'Job'
