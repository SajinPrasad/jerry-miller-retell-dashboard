"""The entity handlers: what they write locally, what they push to GHL, and what they refuse.

GHL is faked at `GHLObjects` rather than over HTTP - these are about the sync's decisions
(upsert, dedup, association, reconciliation), and `test_ghl_objects.py` covers that the real
one builds the right requests. HCP is faked the same way where a handler re-reads from it.
"""

import pytest

from apps.ghl.exceptions import GHLError
from apps.hcp.constants import (
    ESTIMATES_OBJECT,
    JOB_LINE_ITEMS_OBJECT,
    JOBS_OBJECT,
    LEADS_OBJECT,
)
from apps.hcp.models import (
    APPOINTMENT_JOB,
    SYNCED,
    AppointmentIntegration,
    Estimate,
    EstimateJobRelation,
    EstimateOption,
    HCPCalendarSettings,
    HCPEmployeeMapping,
    IntegratedContact,
    Job,
    JobLineItem,
    Lead,
    LeadJobConversion,
)
from apps.hcp.services.sync import HCPToGHLSync
from apps.hcp.tests import fixtures
from apps.hcp.tests.conftest import LOCATION_ID

pytestmark = pytest.mark.django_db


class FakeGHLObjects:
    """Stands in for `GHLObjects`, recording every write so a test can assert on it."""

    def __init__(self, fail_relations=False):
        self.location_id = LOCATION_ID
        self.created_records = []
        self.updated_records = []
        self.deleted_records = []
        self.relations = []
        self.contacts_created = []
        self.contacts_updated = []
        self.appointments_created = []
        self.appointments_updated = []
        self.appointments_deleted = []
        self.contact_by_email = {}
        self.fail_relations = fail_relations
        self._next = 0

    def _id(self, prefix):
        self._next += 1
        return f'{prefix}-{self._next}'

    def contact_field_ids(self):
        return {'HCP Customer ID': 'cf-id', 'HCP Customer Note': 'cf-note'}

    def create_contact(self, payload):
        contact_id = self._id('ghl-contact')
        self.contacts_created.append((contact_id, payload))
        return contact_id

    def update_contact(self, contact_id, payload):
        self.contacts_updated.append((contact_id, payload))

    def find_contact_by_email(self, email):
        return self.contact_by_email.get(email, '')

    def create_record(self, object_key, properties, owners=None):
        record_id = self._id(f'ghl-{object_key.rsplit(".", 1)[-1]}')
        self.created_records.append((object_key, record_id, properties, owners))
        return record_id

    def update_record(self, object_key, record_id, properties, owners=None):
        self.updated_records.append((object_key, record_id, properties, owners))
        return record_id

    def delete_record(self, object_key, record_id):
        self.deleted_records.append((object_key, record_id))

    def create_relation(self, first_key, second_key, first_id, second_id):
        if self.fail_relations:
            raise GHLError('no such association')
        relation_id = self._id('rel')
        self.relations.append((first_key, second_key, first_id, second_id))
        return relation_id

    def create_appointment(self, payload):
        appointment_id = self._id('ghl-appt')
        self.appointments_created.append((appointment_id, payload))
        return appointment_id

    def update_appointment(self, appointment_id, payload):
        self.appointments_updated.append((appointment_id, payload))

    def delete_appointment(self, appointment_id):
        self.appointments_deleted.append(appointment_id)

    # --- assertions helpers ---

    def record_for(self, object_key):
        for key, record_id, properties, _ in self.created_records:
            if key == object_key:
                return record_id, properties
        raise AssertionError(f'no {object_key} record was created')


class FakeHCP:
    def __init__(self, line_items=None, customers=None):
        self._line_items = line_items if line_items is not None else []
        self._customers = customers or {}
        self.line_item_reads = 0

    def get_job_line_items(self, job_id):
        self.line_item_reads += 1
        return self._line_items

    def get_customer(self, customer_id):
        return self._customers.get(customer_id, {})


@pytest.fixture
def ghl():
    return FakeGHLObjects()


@pytest.fixture
def sync(account, ghl):
    return HCPToGHLSync(account, ghl=ghl, hcp=FakeHCP())


def make_contact(sync_service, ghl_obj=None):
    """Put a synced customer in place, the way an earlier `customer.created` would have."""
    sync_service.sync_customer(fixtures.customer())
    return IntegratedContact.objects.get(location_id=LOCATION_ID)


# --- customers --------------------------------------------------------------------------


def test_syncing_a_customer_creates_the_ghl_contact_and_the_link(sync, ghl):
    assert sync.sync_customer(fixtures.customer()) == 'synced'

    contact = IntegratedContact.objects.get(location_id=LOCATION_ID)
    assert contact.hcp_customer_id == 'hcp-customer-1'
    assert contact.ghl_contact_id == ghl.contacts_created[0][0]
    assert contact.first_name == 'Jane'
    assert contact.city == 'Austin'


def test_syncing_the_same_customer_twice_updates_rather_than_duplicates(sync, ghl):
    sync.sync_customer(fixtures.customer())
    sync.sync_customer(fixtures.customer(first_name='Janet'))

    assert IntegratedContact.objects.count() == 1
    assert IntegratedContact.objects.get().first_name == 'Janet'
    assert len(ghl.contacts_created) == 1
    assert len(ghl.contacts_updated) == 1


def test_a_customer_already_in_ghl_is_matched_by_email_not_duplicated(sync, ghl):
    """Someone entered by hand or created by a form must not become a second contact."""
    ghl.contact_by_email['jane@example.test'] = 'ghl-existing-1'

    sync.sync_customer(fixtures.customer())

    assert ghl.contacts_created == []
    assert ghl.contacts_updated[0][0] == 'ghl-existing-1'
    assert IntegratedContact.objects.get().ghl_contact_id == 'ghl-existing-1'


def test_a_customer_with_nothing_to_call_it_is_skipped_not_failed(sync, ghl):
    empty = fixtures.customer(
        first_name=None, last_name=None, email=None, mobile_number=None, home_number=None
    )

    assert sync.sync_customer(empty) == 'skipped'
    assert not IntegratedContact.objects.exists()
    assert ghl.contacts_created == []


def test_a_customer_with_no_id_is_ignored(sync):
    assert sync.sync_customer(fixtures.customer(id=None)) == 'ignored'


# --- leads ------------------------------------------------------------------------------


def test_syncing_a_lead_creates_the_record_and_links_it_to_the_contact(sync, ghl):
    assert sync.sync_lead(fixtures.lead()) == 'synced'

    row = Lead.objects.get(location_id=LOCATION_ID)
    record_id, properties = ghl.record_for(LEADS_OBJECT)
    assert row.hcp_lead_id == 'hcp-lead-1'
    assert row.ghl_lead_record_id == record_id
    assert properties['external_lead_id'] == 'hcp-lead-1'
    assert ('contact', LEADS_OBJECT, row.contact.ghl_contact_id, record_id) in ghl.relations


def test_a_lead_creates_its_customers_contact_when_it_has_not_been_seen(sync, ghl):
    """HCP does not guarantee the customer event arrives first, and a lead with no contact
    cannot be associated to one.
    """
    sync.sync_lead(fixtures.lead())

    assert IntegratedContact.objects.filter(hcp_customer_id='hcp-customer-1').exists()


def test_a_second_lead_event_updates_the_same_record(sync, ghl):
    """Several HCP events carry the whole lead; one upsert handles them all."""
    sync.sync_lead(fixtures.lead())
    sync.sync_lead(fixtures.lead(status='won'))

    assert Lead.objects.count() == 1
    # One record created, one updated - not two leads in GHL.
    assert [key for key, *_ in ghl.created_records] == [LEADS_OBJECT]
    assert [key for key, *_ in ghl.updated_records] == [LEADS_OBJECT]
    assert Lead.objects.get().hcp_status == 'won'


def test_the_contact_relation_is_only_created_once(sync, ghl):
    sync.sync_lead(fixtures.lead())
    sync.sync_lead(fixtures.lead(status='won'))

    assert len([r for r in ghl.relations if r[1] == LEADS_OBJECT]) == 1


def test_a_lead_whose_customer_cannot_be_mirrored_is_skipped(sync):
    anonymous = fixtures.customer(
        first_name=None, last_name=None, email=None, mobile_number=None, home_number=None
    )

    assert sync.sync_lead(fixtures.lead(customer=anonymous)) == 'skipped'
    assert not Lead.objects.exists()


def test_a_failed_association_does_not_cost_the_record(account):
    """The record is in GHL and correct; an association can be added later."""
    ghl = FakeGHLObjects(fail_relations=True)
    sync = HCPToGHLSync(account, ghl=ghl, hcp=FakeHCP())

    assert sync.sync_lead(fixtures.lead()) == 'synced'
    assert Lead.objects.get().ghl_lead_record_id
    assert Lead.objects.get().ghl_relation_id == ''


# --- estimates ----------------------------------------------------------------------------


def test_syncing_an_estimate_creates_the_record_and_its_options(sync, ghl):
    assert sync.sync_estimate(fixtures.estimate()) == 'synced'

    row = Estimate.objects.get(location_id=LOCATION_ID)
    record_id, properties = ghl.record_for(ESTIMATES_OBJECT)
    assert row.ghl_estimate_record_id == record_id
    assert properties['external_estimate_id'] == 'hcp-estimate-1'
    assert EstimateOption.objects.filter(estimate=row).count() == 1


def test_an_estimate_is_linked_to_its_lead_when_the_lead_exists(sync, ghl):
    sync.sync_lead(fixtures.lead())
    lead = Lead.objects.get()

    sync.sync_estimate(fixtures.estimate(lead_id='hcp-lead-1'))

    row = Estimate.objects.get()
    assert row.lead_id == lead.pk
    assert row.ghl_lead_relation_id
    assert (
        LEADS_OBJECT,
        ESTIMATES_OBJECT,
        lead.ghl_lead_record_id,
        row.ghl_estimate_record_id,
    ) in ghl.relations


def test_an_option_withdrawn_in_hcp_stops_being_mirrored(sync):
    """It must not keep showing as approvable here."""
    sync.sync_estimate(fixtures.estimate(options=[fixtures.option('o-1'), fixtures.option('o-2')]))
    assert EstimateOption.objects.count() == 2

    sync.sync_estimate(fixtures.estimate(options=[fixtures.option('o-1')]))

    assert list(EstimateOption.objects.values_list('hcp_option_id', flat=True)) == ['o-1']


def test_an_approval_change_updates_the_same_estimate_record(sync, ghl):
    sync.sync_estimate(fixtures.estimate())
    approved = fixtures.estimate(
        options=[fixtures.option('hcp-option-1', approval_status='approved')]
    )

    sync.sync_estimate(approved)

    assert Estimate.objects.count() == 1
    _, properties = ghl.updated_records[0][0], ghl.updated_records[0][2]
    assert properties['approved_total_amount'] == {'value': 450.0, 'currency': 'default'}


def test_an_estimate_with_no_id_is_ignored(sync):
    assert sync.sync_estimate(fixtures.estimate(id=None)) == 'ignored'


# --- jobs -----------------------------------------------------------------------------------


def test_syncing_a_job_creates_the_record_and_marks_it_synced(sync, ghl):
    assert sync.sync_job(fixtures.job()) == 'synced'

    row = Job.objects.get(location_id=LOCATION_ID)
    record_id, properties = ghl.record_for(JOBS_OBJECT)
    assert row.ghl_job_record_id == record_id
    assert row.sync_state == SYNCED
    assert row.source == 'hcp'
    assert properties['hcp_job_id'] == 'hcp-job-1'
    assert properties['job_amount'] == {'value': 4500.0, 'currency': 'default'}


def test_a_jobs_owners_are_the_mapped_ghl_users(sync, ghl):
    HCPEmployeeMapping.objects.create(
        location_id=LOCATION_ID,
        hcp_employee_id='hcp-emp-1',
        ghl_user_id='ghl-user-1',
    )

    sync.sync_job(fixtures.job())

    owners = [owners for key, _, _, owners in ghl.created_records if key == JOBS_OBJECT][0]
    assert owners == ['ghl-user-1']


def test_an_unmapped_employee_leaves_the_job_unowned(sync, ghl):
    sync.sync_job(fixtures.job())

    owners = [owners for key, _, _, owners in ghl.created_records if key == JOBS_OBJECT][0]
    assert owners == []


def test_a_job_is_linked_to_the_estimate_it_came_from(sync, ghl):
    sync.sync_estimate(fixtures.estimate())
    estimate = Estimate.objects.get()

    sync.sync_job(fixtures.job(estimate_ids=['hcp-estimate-1']))

    job = Job.objects.get()
    assert EstimateJobRelation.objects.filter(estimate=estimate, job=job).exists()
    assert (
        ESTIMATES_OBJECT,
        JOBS_OBJECT,
        estimate.ghl_estimate_record_id,
        job.ghl_job_record_id,
    ) in ghl.relations


def test_a_job_linked_to_an_estimate_twice_only_relates_once(sync, ghl):
    sync.sync_estimate(fixtures.estimate())
    sync.sync_job(fixtures.job(estimate_ids=['hcp-estimate-1']))
    sync.sync_job(fixtures.job(estimate_ids=['hcp-estimate-1'], work_status='completed'))

    assert EstimateJobRelation.objects.count() == 1
    assert len([r for r in ghl.relations if r[0] == ESTIMATES_OBJECT]) == 1


def test_a_job_converted_from_a_lead_records_the_conversion(sync):
    sync.sync_lead(fixtures.lead())

    sync.sync_job(fixtures.job(lead_id='hcp-lead-1'))

    assert LeadJobConversion.objects.filter(hcp_job_id='hcp-job-1').exists()


def test_a_job_event_for_an_unknown_estimate_still_syncs_the_job(sync):
    assert sync.sync_job(fixtures.job(estimate_ids=['hcp-estimate-missing'])) == 'synced'
    assert not EstimateJobRelation.objects.exists()


def test_a_second_job_event_updates_the_same_record(sync, ghl):
    sync.sync_job(fixtures.job())
    sync.sync_job(fixtures.job(work_status='completed'))

    assert Job.objects.count() == 1
    assert Job.objects.get().status == 'completed'
    assert [key for key, *_ in ghl.updated_records if key == JOBS_OBJECT] == [JOBS_OBJECT]


# --- job line item reconciliation --------------------------------------------------------


def test_line_items_are_created_and_linked_to_the_job(sync, ghl):
    sync.sync_job(fixtures.job())
    job = Job.objects.get()

    result = sync.reconcile_line_items(job, [fixtures.line_item('li-1')])

    assert result == {'created': 1, 'updated': 0, 'deleted': 0}
    item = JobLineItem.objects.get()
    assert item.hcp_line_item_id == 'li-1'
    assert item.ghl_line_item_record_id
    assert (
        JOBS_OBJECT,
        JOB_LINE_ITEMS_OBJECT,
        job.ghl_job_record_id,
        item.ghl_line_item_record_id,
    ) in ghl.relations


def test_a_changed_line_item_is_updated_not_recreated(sync, ghl):
    sync.sync_job(fixtures.job())
    job = Job.objects.get()
    sync.reconcile_line_items(job, [fixtures.line_item('li-1')])
    before = JobLineItem.objects.get().ghl_line_item_record_id

    result = sync.reconcile_line_items(job, [fixtures.line_item('li-1', name='Bigger condenser')])

    assert result == {'created': 0, 'updated': 1, 'deleted': 0}
    assert JobLineItem.objects.get().name == 'Bigger condenser'
    assert JobLineItem.objects.get().ghl_line_item_record_id == before


def test_a_line_item_removed_in_hcp_is_deleted_in_ghl_too(sync, ghl):
    """A quote the customer can still see is worse than a missing one."""
    sync.sync_job(fixtures.job())
    job = Job.objects.get()
    sync.reconcile_line_items(job, [fixtures.line_item('li-1'), fixtures.line_item('li-2')])
    record_id = JobLineItem.objects.get(hcp_line_item_id='li-2').ghl_line_item_record_id

    result = sync.reconcile_line_items(job, [fixtures.line_item('li-1')])

    assert result == {'created': 0, 'updated': 1, 'deleted': 1}
    assert list(JobLineItem.objects.values_list('hcp_line_item_id', flat=True)) == ['li-1']
    assert (JOB_LINE_ITEMS_OBJECT, record_id) in ghl.deleted_records


def test_reconciling_with_no_list_reads_them_from_hcp(account, ghl):
    """Several `job.*` events send a trimmed job with no line items, and an empty list there
    means "this event did not mention them", not "the job has none".
    """
    hcp = FakeHCP(line_items=[fixtures.line_item('li-9')])
    sync = HCPToGHLSync(account, ghl=ghl, hcp=hcp)
    sync.sync_job(fixtures.job(line_items=None))
    job = Job.objects.get()

    sync.reconcile_line_items(job)

    assert hcp.line_item_reads == 1
    assert JobLineItem.objects.get().hcp_line_item_id == 'li-9'


def test_an_empty_hcp_list_deletes_every_line_item(sync, ghl):
    sync.sync_job(fixtures.job())
    job = Job.objects.get()
    sync.reconcile_line_items(job, [fixtures.line_item('li-1')])

    result = sync.reconcile_line_items(job, [])

    assert result == {'created': 0, 'updated': 0, 'deleted': 1}
    assert not JobLineItem.objects.exists()


def test_a_line_item_with_no_id_is_skipped(sync):
    sync.sync_job(fixtures.job())
    job = Job.objects.get()

    result = sync.reconcile_line_items(job, [fixtures.line_item(id=None)])

    assert result == {'created': 0, 'updated': 0, 'deleted': 0}


# --- appointments ----------------------------------------------------------------------------


@pytest.fixture
def scheduled(account):
    """A location configured well enough to book: calendar, mapping, fallback user."""
    mapping_row = HCPEmployeeMapping.objects.create(
        location_id=LOCATION_ID,
        hcp_employee_id='hcp-emp-1',
        hcp_employee_name='Ana Ruiz',
        ghl_user_id='ghl-user-1',
    )
    HCPCalendarSettings.objects.create(
        location_id=LOCATION_ID,
        estimate_calendar_id='cal-est',
        job_calendar_id='cal-job',
        default_user=mapping_row,
    )
    return mapping_row


def test_an_appointment_is_booked_on_the_job_calendar(sync, ghl, scheduled):
    assert sync.sync_job_appointment(fixtures.job(), fixtures.appointment()) == 'synced'

    _, payload = ghl.appointments_created[0]
    assert payload['calendarId'] == 'cal-job'
    assert payload['assignedUserId'] == 'ghl-user-1'
    assert payload['startTime'] == '2026-03-10T15:00:00Z'
    assert payload['appointmentStatus'] == 'confirmed'


def test_the_booking_is_recorded_against_the_hcp_appointment(sync, ghl, scheduled):
    sync.sync_job_appointment(fixtures.job(), fixtures.appointment())

    row = AppointmentIntegration.objects.get()
    assert row.appointment_type == APPOINTMENT_JOB
    assert row.hcp_appointment_id == 'hcp-appt-1'
    assert row.hcp_job_id == 'hcp-job-1'
    assert row.ghl_appointment_id == ghl.appointments_created[0][0]
    assert row.used_default_employee is False


def test_rescheduling_updates_the_same_ghl_appointment(sync, ghl, scheduled):
    sync.sync_job_appointment(fixtures.job(), fixtures.appointment())

    sync.sync_job_appointment(
        fixtures.job(), fixtures.appointment(start_time='2026-03-11T15:00:00Z')
    )

    assert len(ghl.appointments_created) == 1
    assert ghl.appointments_updated[0][1]['startTime'] == '2026-03-11T15:00:00Z'
    assert AppointmentIntegration.objects.count() == 1


def test_an_unmapped_technician_falls_back_to_the_calendar_default(sync, ghl, scheduled):
    appointment = fixtures.appointment(dispatched_employees=[{'id': 'hcp-emp-unmapped'}])

    sync.sync_job_appointment(fixtures.job(assigned_employees=[]), appointment)

    assert ghl.appointments_created[0][1]['assignedUserId'] == 'ghl-user-1'
    assert AppointmentIntegration.objects.get().used_default_employee is True


def test_no_calendar_configured_skips_rather_than_fails(sync, ghl, account):
    """The job itself has already synced; this is the part the SOP warns fails quietly."""
    assert sync.sync_job_appointment(fixtures.job(), fixtures.appointment()) == 'skipped'
    assert ghl.appointments_created == []


def test_no_technician_and_no_fallback_skips(sync, ghl, account):
    HCPCalendarSettings.objects.create(
        location_id=LOCATION_ID, estimate_calendar_id='cal-est', job_calendar_id='cal-job'
    )
    appointment = fixtures.appointment(dispatched_employees=[])

    outcome = sync.sync_job_appointment(fixtures.job(assigned_employees=[]), appointment)

    assert outcome == 'skipped'
    assert ghl.appointments_created == []


def test_an_appointment_with_no_start_time_is_skipped(sync, ghl, scheduled):
    assert (
        sync.sync_job_appointment(fixtures.job(), fixtures.appointment(start_time=None))
        == 'skipped'
    )


def test_a_discarded_appointment_is_deleted_in_ghl_and_here(sync, ghl, scheduled):
    sync.sync_job_appointment(fixtures.job(), fixtures.appointment())
    ghl_appointment_id = AppointmentIntegration.objects.get().ghl_appointment_id

    assert sync.remove_job_appointment(fixtures.appointment()) == 'synced'

    assert ghl.appointments_deleted == [ghl_appointment_id]
    assert not AppointmentIntegration.objects.exists()


def test_discarding_an_appointment_we_never_booked_is_ignored(sync, ghl, scheduled):
    assert sync.remove_job_appointment(fixtures.appointment()) == 'ignored'
    assert ghl.appointments_deleted == []


def test_an_appointment_already_gone_in_ghl_still_drops_the_local_link(account, scheduled):
    """Already gone is the state we wanted; keeping the row would make a re-book try to update
    an appointment that is not there.
    """

    class Gone(FakeGHLObjects):
        def delete_appointment(self, appointment_id):
            raise GHLError('404 not found')

    ghl = Gone()
    sync = HCPToGHLSync(account, ghl=ghl, hcp=FakeHCP())
    sync.sync_job_appointment(fixtures.job(), fixtures.appointment())

    assert sync.remove_job_appointment(fixtures.appointment()) == 'synced'
    assert not AppointmentIntegration.objects.exists()


def test_the_booking_title_uses_the_invoice_number(sync, ghl, scheduled):
    sync.sync_job_appointment(fixtures.job(), fixtures.appointment())

    assert ghl.appointments_created[0][1]['title'] == 'Job #5567'


def test_an_uninvoiced_job_is_titled_plainly(sync, ghl, scheduled):
    """`Job #` with nothing after it reads as broken."""
    sync.sync_job_appointment(fixtures.job(invoice_number=None), fixtures.appointment())

    assert ghl.appointments_created[0][1]['title'] == 'Job'


# --- estimate appointments ------------------------------------------------------------------


def test_a_scheduled_estimate_is_booked_on_the_estimate_calendar(sync, ghl, scheduled):
    """An estimate carries its own schedule rather than sending appointment events."""
    sync.sync_estimate(fixtures.estimate())

    _, payload = ghl.appointments_created[0]
    assert payload['calendarId'] == 'cal-est'
    assert payload['startTime'] == '2026-03-04T15:00:00Z'
    assert payload['title'] == 'Estimate #1042'


def test_the_estimate_booking_is_keyed_on_the_estimate_not_an_appointment_id(sync, scheduled):
    """HCP gives an estimate one schedule and no appointment id of its own, which is what
    `unique_estimate_schedule_per_location` is built on.
    """
    sync.sync_estimate(fixtures.estimate())

    row = AppointmentIntegration.objects.get()
    assert row.appointment_type == 'estimate'
    assert row.hcp_estimate_id == 'hcp-estimate-1'
    assert row.hcp_appointment_id is None
    assert row.hcp_estimate_number == '1042'


def test_rescheduling_an_estimate_updates_the_same_booking(sync, ghl, scheduled):
    sync.sync_estimate(fixtures.estimate())

    sync.sync_estimate(
        fixtures.estimate(
            schedule={'scheduled_start': '2026-03-05T15:00:00Z', 'arrival_window': 60}
        )
    )

    assert len(ghl.appointments_created) == 1
    assert ghl.appointments_updated[0][1]['startTime'] == '2026-03-05T15:00:00Z'
    assert AppointmentIntegration.objects.count() == 1


def test_an_estimate_whose_schedule_is_cleared_is_unbooked(sync, ghl, scheduled):
    """A consultation the customer can still see after it was called off is worse than none."""
    sync.sync_estimate(fixtures.estimate())
    booked = AppointmentIntegration.objects.get().ghl_appointment_id

    sync.sync_estimate(fixtures.estimate(schedule={}))

    assert ghl.appointments_deleted == [booked]
    assert not AppointmentIntegration.objects.exists()


def test_an_unscheduled_estimate_books_nothing(sync, ghl, scheduled):
    sync.sync_estimate(fixtures.estimate(schedule={}))

    assert ghl.appointments_created == []
    assert not AppointmentIntegration.objects.exists()


def test_an_estimate_uses_the_estimate_specific_fallback_user(sync, ghl, scheduled, account):
    """The per-calendar override exists because different people lead consultations."""
    estimate_lead = HCPEmployeeMapping.objects.create(
        location_id=LOCATION_ID, hcp_employee_id='hcp-emp-2', ghl_user_id='ghl-user-2'
    )
    settings_row = HCPCalendarSettings.objects.get(location_id=LOCATION_ID)
    settings_row.estimate_default_user = estimate_lead
    settings_row.save()

    sync.sync_estimate(fixtures.estimate())

    assert ghl.appointments_created[0][1]['assignedUserId'] == 'ghl-user-2'
    assert AppointmentIntegration.objects.get().used_default_employee is True


def test_a_mapped_technician_on_the_estimate_beats_the_fallback(sync, ghl, scheduled):
    sync.sync_estimate(fixtures.estimate(assigned_employees=[{'id': 'hcp-emp-1'}]))

    assert ghl.appointments_created[0][1]['assignedUserId'] == 'ghl-user-1'
    assert AppointmentIntegration.objects.get().used_default_employee is False


def test_an_estimate_still_syncs_when_no_calendar_is_configured(sync, ghl, account):
    """The record is the point; the booking is the part the SOP warns fails quietly."""
    assert sync.sync_estimate(fixtures.estimate()) == 'synced'
    assert Estimate.objects.get().ghl_estimate_record_id
    assert ghl.appointments_created == []


def test_a_job_and_an_estimate_appointment_coexist(sync, ghl, scheduled):
    """Different `appointment_type`, so they do not compete for the same unique constraint."""
    sync.sync_estimate(fixtures.estimate())
    sync.sync_job_appointment(fixtures.job(), fixtures.appointment())

    assert AppointmentIntegration.objects.count() == 2
    assert set(AppointmentIntegration.objects.values_list('appointment_type', flat=True)) == {
        'estimate',
        'job',
    }
