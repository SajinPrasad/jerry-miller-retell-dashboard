"""`GHLToHCPSync`: what it pushes to HCP, what it refuses, and - the point of Phase 4's loop
guard - what it treats as an echo of this app's own forward-sync writes rather than a genuine
GHL-side edit.

GHL is faked at `GHLObjects`, HCP at `HCPClient`, the same style `test_sync.py` uses for the
forward direction.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apps.hcp.constants import JOB_LINE_ITEMS_OBJECT, LEADS_OBJECT
from apps.hcp.models import (
    APPOINTMENT_ESTIMATE,
    APPOINTMENT_JOB,
    SOURCE_GHL,
    AppointmentIntegration,
    Estimate,
    EstimateOption,
    HCPEmployeeMapping,
    IntegratedContact,
    Job,
    JobLineItem,
)
from apps.hcp.services.reverse_sync import IGNORED, SKIPPED, SYNCED_OUTCOME, GHLToHCPSync
from apps.hcp.tests import ghl_fixtures
from apps.hcp.tests.conftest import LOCATION_ID

pytestmark = pytest.mark.django_db


class FakeGHLObjects:
    def __init__(self):
        self.location_id = LOCATION_ID
        self.contacts_updated = []
        self.updated_records = []
        self.fail_updates = False

    def contact_field_ids(self):
        return {'HCP Customer ID': 'cf-id', 'HCP Customer Note': 'cf-note'}

    def update_contact(self, contact_id, payload):
        if self.fail_updates:
            from apps.ghl.exceptions import GHLError

            raise GHLError('boom')
        self.contacts_updated.append((contact_id, payload))

    def update_record(self, object_key, record_id, properties, owners=None):
        if self.fail_updates:
            from apps.ghl.exceptions import GHLError

            raise GHLError('boom')
        self.updated_records.append((object_key, record_id, properties))
        return record_id


class FakeHCPClient:
    def __init__(self):
        self.customers_created = []
        self.customers_updated = []
        self.leads_created = []
        self.estimates_created = []
        self.jobs_created = []
        self.line_items_created = []
        self.line_items_updated = []
        self.line_items_deleted = []
        self.estimate_schedules_updated = []
        self.job_appointments_updated = []
        self.job_appointments_deleted = []
        self._next = 0
        self.job_appointments: dict[str, list[dict]] = {}

    def _id(self, prefix):
        self._next += 1
        return f'{prefix}-{self._next}'

    def create_customer(self, payload):
        customer_id = self._id('hcp-customer')
        self.customers_created.append(payload)
        return {'id': customer_id, **payload}

    def update_customer(self, customer_id, payload):
        self.customers_updated.append((customer_id, payload))
        return {'id': customer_id, **payload}

    def create_lead(self, payload):
        self.leads_created.append(payload)
        return {'id': self._id('hcp-lead')}

    def create_estimate(self, payload):
        self.estimates_created.append(payload)
        return {'id': self._id('hcp-estimate')}

    def create_job(self, payload):
        self.jobs_created.append(payload)
        return {'id': self._id('hcp-job')}

    def create_job_line_item(self, job_id, payload):
        self.line_items_created.append((job_id, payload))
        return {
            'id': self._id('hcp-line-item'),
            'name': payload.get('name', ''),
            'description': payload.get('description', ''),
            'unit_price': payload.get('unit_price', 0),
            'unit_cost': payload.get('unit_cost'),
            'quantity': payload.get('quantity', 1),
            'taxable': payload.get('taxable', False),
            'kind': payload.get('kind', ''),
            'amount': payload.get('unit_price', 0) * payload.get('quantity', 1),
        }

    def update_job_line_item(self, job_id, line_item_id, payload):
        self.line_items_updated.append((job_id, line_item_id, payload))
        return {
            'id': line_item_id,
            'name': payload.get('name', 'Filter'),
            'description': payload.get('description', ''),
            'unit_price': payload.get('unit_price', 1250),
            'unit_cost': payload.get('unit_cost'),
            'quantity': payload.get('quantity', 1),
            'taxable': payload.get('taxable', False),
            'kind': payload.get('kind', ''),
            'amount': payload.get('unit_price', 1250),
        }

    def delete_job_line_item(self, job_id, line_item_id):
        self.line_items_deleted.append((job_id, line_item_id))

    def update_estimate_option_schedule(self, estimate_id, option_id, payload):
        self.estimate_schedules_updated.append((estimate_id, option_id, payload))
        return {}

    def update_job_appointment(self, job_id, appointment_id, payload):
        self.job_appointments_updated.append((job_id, appointment_id, payload))
        return {}

    def delete_job_appointment(self, job_id, appointment_id):
        self.job_appointments_deleted.append((job_id, appointment_id))

    def get_job_appointments(self, job_id):
        return self.job_appointments.get(job_id, [])


@pytest.fixture
def ghl():
    return FakeGHLObjects()


@pytest.fixture
def hcp():
    return FakeHCPClient()


@pytest.fixture
def sync(account, ghl, hcp):
    return GHLToHCPSync(account, ghl=ghl, hcp=hcp)


# --- contacts --------------------------------------------------------------------------


def test_contact_create_creates_the_hcp_customer_and_links_it(sync, hcp, ghl):
    outcome = sync.sync_contact_create(ghl_fixtures.contact())

    assert outcome == SYNCED_OUTCOME
    assert hcp.customers_created
    row = IntegratedContact.objects.get(location_id=LOCATION_ID, ghl_contact_id='ghl-contact-1')
    assert row.first_name == 'Jane'
    assert row.hcp_customer_id
    assert ghl.contacts_updated  # the "HCP Customer ID" field write-back


def test_contact_create_is_ignored_when_already_linked(sync, hcp):
    """Self-origination: forward sync links the row before GHL's own `ContactCreate` for a
    contact it just created can arrive."""
    IntegratedContact.objects.create(
        location_id=LOCATION_ID, ghl_contact_id='ghl-contact-1', hcp_customer_id='hcp-customer-1'
    )

    outcome = sync.sync_contact_create(ghl_fixtures.contact())

    assert outcome == IGNORED
    assert hcp.customers_created == []


def test_contact_create_is_skipped_when_unidentifiable(sync, hcp):
    outcome = sync.sync_contact_create(
        ghl_fixtures.contact(firstName='', lastName='', email='', phone='', companyName='')
    )

    assert outcome == SKIPPED
    assert hcp.customers_created == []


def test_contact_update_is_ignored_for_an_unlinked_contact(sync, hcp):
    outcome = sync.sync_contact_update(ghl_fixtures.contact())

    assert outcome == IGNORED
    assert hcp.customers_updated == []


def _linked_contact_matching_fixture(**overrides):
    """An `IntegratedContact` whose cached fields already match `ghl_fixtures.contact()`'s
    defaults - the state forward sync (or a previous reverse pass) leaves behind.
    """
    fields = {
        'location_id': LOCATION_ID,
        'ghl_contact_id': 'ghl-contact-1',
        'hcp_customer_id': 'hcp-customer-1',
        'first_name': 'Jane',
        'last_name': 'Doe',
        'company_name': '',
        'email': 'jane@example.test',
        'mobile_number': '+15551230001',
        'source': 'Google',
        'tags': ['vip'],
    }
    fields.update(overrides)
    return IntegratedContact.objects.create(**fields)


def test_contact_update_with_no_change_is_skipped(sync, hcp):
    """The echo this app's own forward-sync write produces: the local row already matches."""
    _linked_contact_matching_fixture()

    outcome = sync.sync_contact_update(ghl_fixtures.contact())

    assert outcome == SKIPPED
    assert hcp.customers_updated == []


def test_contact_update_pushes_only_the_changed_field(sync, hcp):
    _linked_contact_matching_fixture()

    outcome = sync.sync_contact_update(ghl_fixtures.contact(firstName='Janet'))

    assert outcome == SYNCED_OUTCOME
    customer_id, payload = hcp.customers_updated[0]
    assert customer_id == 'hcp-customer-1'
    assert payload == {'first_name': 'Janet'}
    assert IntegratedContact.objects.get(ghl_contact_id='ghl-contact-1').first_name == 'Janet'


def test_contact_update_never_pushes_an_address_change(sync, hcp):
    _linked_contact_matching_fixture()

    outcome = sync.sync_contact_update(ghl_fixtures.contact(address1='99 New Rd', city='Dallas'))

    assert outcome == SKIPPED
    assert hcp.customers_updated == []


# --- leads / estimates / jobs ------------------------------------------------------------


@pytest.fixture
def linked_contact():
    return IntegratedContact.objects.create(
        location_id=LOCATION_ID, ghl_contact_id='ghl-contact-1', hcp_customer_id='hcp-customer-1'
    )


def test_sync_lead_creates_from_a_contact_id_property(sync, hcp, linked_contact):
    record = ghl_fixtures.record(
        record_id='ghl-lead-1',
        object_key=LEADS_OBJECT,
        properties=[{'key': 'contact_id', 'valueString': 'ghl-contact-1'}],
    )

    outcome = sync.sync_lead(record, {'contact_id': 'ghl-contact-1'})

    assert outcome == SYNCED_OUTCOME
    assert hcp.leads_created[0]['customer_id'] == 'hcp-customer-1'


def test_sync_lead_is_ignored_when_external_lead_id_is_already_set(sync, hcp, linked_contact):
    """Forward sync writes `external_lead_id` onto the GHL record in the same call that
    creates it - its own `RecordCreate` echo carries that property already populated."""
    outcome = sync.sync_lead(
        {'id': 'ghl-lead-1'}, {'contact_id': 'ghl-contact-1', 'external_lead_id': 'hcp-lead-9'}
    )

    assert outcome == IGNORED
    assert hcp.leads_created == []


def test_sync_lead_is_skipped_without_a_linked_contact(sync, hcp):
    outcome = sync.sync_lead({'id': 'ghl-lead-1'}, {'contact_id': 'not-linked'})

    assert outcome == SKIPPED
    assert hcp.leads_created == []


def test_sync_estimate_creates_from_a_contact_id_property(sync, hcp, linked_contact):
    outcome = sync.sync_estimate({'id': 'ghl-estimate-1'}, {'contact_id': 'ghl-contact-1'})

    assert outcome == SYNCED_OUTCOME
    assert Estimate.objects.get(ghl_estimate_record_id='ghl-estimate-1').hcp_estimate_id


def test_sync_job_creates_and_marks_the_source_as_ghl(sync, hcp, linked_contact):
    outcome = sync.sync_job({'id': 'ghl-job-1'}, {'contact_id': 'ghl-contact-1'})

    assert outcome == SYNCED_OUTCOME
    job = Job.objects.get(ghl_job_record_id='ghl-job-1')
    assert job.source == SOURCE_GHL


# --- job line items --------------------------------------------------------------------


@pytest.fixture
def linked_job(linked_contact):
    return Job.objects.create(
        location_id=LOCATION_ID,
        contact=linked_contact,
        ghl_job_record_id='ghl-job-1',
        hcp_job_id='hcp-job-1',
    )


def test_line_item_create_is_skipped_without_a_synced_job(sync, hcp):
    outcome = sync.sync_job_line_item(
        {'id': 'ghl-li-1'}, {'job_record_id': 'no-such-job', 'name': 'Filter'}
    )

    assert outcome == SKIPPED
    assert hcp.line_items_created == []


def test_line_item_create_is_ignored_when_hcp_line_item_id_is_already_set(sync, hcp, linked_job):
    outcome = sync.sync_job_line_item(
        {'id': 'ghl-li-1'},
        {'job_record_id': 'ghl-job-1', 'name': 'Filter', 'hcp_line_item_id': 'hcp-li-9'},
    )

    assert outcome == IGNORED
    assert hcp.line_items_created == []


def test_line_item_create_writes_back_hcp_totals(sync, hcp, ghl, linked_job):
    outcome = sync.sync_job_line_item(
        {'id': 'ghl-li-1'},
        {
            'job_record_id': 'ghl-job-1',
            'name': 'Filter',
            'unit_price': {'value': 12.5, 'currency': 'default'},
            'quantity': 2,
        },
    )

    assert outcome == SYNCED_OUTCOME
    assert JobLineItem.objects.get(ghl_line_item_record_id='ghl-li-1').source == SOURCE_GHL
    object_key, record_id, properties = ghl.updated_records[0]
    assert object_key == JOB_LINE_ITEMS_OBJECT
    assert record_id == 'ghl-li-1'
    assert 'hcp_line_item_id' in properties


def test_line_item_update_with_no_change_is_skipped(sync, hcp, linked_job):
    existing = JobLineItem.objects.create(
        location_id=LOCATION_ID,
        job=linked_job,
        hcp_line_item_id='hcp-li-1',
        ghl_line_item_record_id='ghl-li-1',
        name='Filter',
        unit_price=Decimal('12.50'),
    )

    outcome = sync.sync_job_line_item(
        {'id': 'ghl-li-1'},
        {
            'job_record_id': 'ghl-job-1',
            'name': 'Filter',
            'unit_price': {'value': 12.5, 'currency': 'default'},
        },
    )

    assert outcome == SKIPPED
    assert hcp.line_items_updated == []
    existing.refresh_from_db()


def test_line_item_update_pushes_only_the_changed_field(sync, hcp, linked_job):
    JobLineItem.objects.create(
        location_id=LOCATION_ID,
        job=linked_job,
        hcp_line_item_id='hcp-li-1',
        ghl_line_item_record_id='ghl-li-1',
        name='Filter',
        unit_price=Decimal('12.50'),
    )

    outcome = sync.sync_job_line_item(
        {'id': 'ghl-li-1'},
        {
            'job_record_id': 'ghl-job-1',
            'name': 'Filter',
            'unit_price': {'value': 15.0, 'currency': 'default'},
        },
    )

    assert outcome == SYNCED_OUTCOME
    job_id, line_item_id, changes = hcp.line_items_updated[0]
    assert job_id == 'hcp-job-1' and line_item_id == 'hcp-li-1'
    assert changes == {'unit_price': 1500}


def test_line_item_delete_removes_it_from_hcp_and_locally(sync, hcp, linked_job):
    JobLineItem.objects.create(
        location_id=LOCATION_ID,
        job=linked_job,
        hcp_line_item_id='hcp-li-1',
        ghl_line_item_record_id='ghl-li-1',
        name='Filter',
    )

    outcome = sync.delete_job_line_item({'id': 'ghl-li-1'})

    assert outcome == SYNCED_OUTCOME
    assert hcp.line_items_deleted == [('hcp-job-1', 'hcp-li-1')]
    assert not JobLineItem.objects.filter(ghl_line_item_record_id='ghl-li-1').exists()


def test_line_item_delete_of_an_unknown_record_is_ignored(sync, hcp):
    outcome = sync.delete_job_line_item({'id': 'no-such-record'})

    assert outcome == IGNORED
    assert hcp.line_items_deleted == []


# --- appointments ------------------------------------------------------------------------


@pytest.fixture
def mapped_employee():
    return HCPEmployeeMapping.objects.create(
        location_id=LOCATION_ID,
        hcp_employee_id='hcp-emp-1',
        ghl_user_id='ghl-user-1',
    )


def test_appointment_event_for_an_unknown_id_is_ignored(sync):
    outcome = sync.sync_appointment(ghl_fixtures.appointment_event(appointment_id='unknown'))

    assert outcome == IGNORED


def test_stale_appointment_event_is_skipped(sync, hcp):
    row = AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='hcp-job-1',
        hcp_appointment_id='hcp-appt-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc),
        external_updated_at=datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc),
    )

    outcome = sync.sync_appointment(
        ghl_fixtures.appointment_event(dateUpdated='2026-11-17T12:00:00+00:00')
    )

    assert outcome == SKIPPED
    assert hcp.job_appointments_updated == []
    row.refresh_from_db()
    assert row.starts_at == datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


def test_job_appointment_pushes_the_new_time_with_a_mapped_technician(sync, hcp, mapped_employee):
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='hcp-job-1',
        hcp_appointment_id='hcp-appt-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
    )

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event(assignedUserId='ghl-user-1'))

    assert outcome == SYNCED_OUTCOME
    job_id, appointment_id, payload = hcp.job_appointments_updated[0]
    assert job_id == 'hcp-job-1' and appointment_id == 'hcp-appt-1'
    assert payload['dispatched_employees_ids'] == ['hcp-emp-1']
    assert payload['start_time']


def test_job_appointment_with_default_user_does_not_reassign(sync, hcp, mapped_employee):
    """A time-only change on a default-user appointment still syncs the time, but never writes
    the technician - a change that is *only* the assignee is never even detected as a change.
    """
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='hcp-job-1',
        hcp_appointment_id='hcp-appt-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
        used_default_employee=True,
    )
    hcp.job_appointments['hcp-job-1'] = [
        {'id': 'hcp-appt-1', 'dispatched_employees_ids': ['hcp-emp-existing']}
    ]

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event(assignedUserId='ghl-user-1'))

    assert outcome == SYNCED_OUTCOME
    _, _, payload = hcp.job_appointments_updated[0]
    assert payload['dispatched_employees_ids'] == ['hcp-emp-existing']


def test_job_appointment_assignee_only_change_on_default_user_is_not_a_change(
    sync, hcp, mapped_employee
):
    starts_at = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='hcp-job-1',
        hcp_appointment_id='hcp-appt-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=starts_at,
        ends_at=starts_at,
        used_default_employee=True,
    )

    outcome = sync.sync_appointment(
        ghl_fixtures.appointment_event(
            assignedUserId='ghl-user-1',
            startTime=starts_at.isoformat(),
            endTime=starts_at.isoformat(),
        )
    )

    assert outcome == SKIPPED
    assert hcp.job_appointments_updated == []


def test_job_appointment_with_no_hcp_employee_at_all_is_skipped(sync, hcp, mapped_employee):
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='hcp-job-1',
        hcp_appointment_id='hcp-appt-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
        used_default_employee=True,
    )
    # No entry in hcp.job_appointments: HCP has nobody on it either.

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event())

    assert outcome == SKIPPED
    assert hcp.job_appointments_updated == []


def test_job_appointment_delete_removes_it_from_hcp_and_locally(sync, hcp):
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='hcp-job-1',
        hcp_appointment_id='hcp-appt-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
    )

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event(event_type='AppointmentDelete'))

    assert outcome == SYNCED_OUTCOME
    assert hcp.job_appointments_deleted == [('hcp-job-1', 'hcp-appt-1')]
    assert not AppointmentIntegration.objects.filter(ghl_appointment_id='ghl-appt-1').exists()


def test_estimate_appointment_delete_is_ignored(sync, hcp):
    """The SOP's limit: estimate appointment cancellation/removal stays HCP-only."""
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_ESTIMATE,
        hcp_estimate_id='hcp-estimate-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
    )

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event(event_type='AppointmentDelete'))

    assert outcome == IGNORED


def test_estimate_appointment_pushes_the_new_time_to_the_first_option(sync, hcp, mapped_employee):
    estimate = Estimate.objects.create(
        location_id=LOCATION_ID,
        contact=IntegratedContact.objects.create(
            location_id=LOCATION_ID, ghl_contact_id='c-1', hcp_customer_id='hcp-cust-1'
        ),
        hcp_estimate_id='hcp-estimate-1',
    )
    EstimateOption.objects.create(estimate=estimate, hcp_option_id='hcp-option-1')
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_ESTIMATE,
        hcp_estimate_id='hcp-estimate-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
    )

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event(assignedUserId='ghl-user-1'))

    assert outcome == SYNCED_OUTCOME
    estimate_id, option_id, payload = hcp.estimate_schedules_updated[0]
    assert estimate_id == 'hcp-estimate-1' and option_id == 'hcp-option-1'
    assert payload['dispatched_employees'] == [{'employee_id': 'hcp-emp-1'}]


def test_estimate_appointment_without_an_option_is_skipped(sync, hcp):
    Estimate.objects.create(
        location_id=LOCATION_ID,
        contact=IntegratedContact.objects.create(
            location_id=LOCATION_ID, ghl_contact_id='c-1', hcp_customer_id='hcp-cust-1'
        ),
        hcp_estimate_id='hcp-estimate-1',
    )
    AppointmentIntegration.objects.create(
        location_id=LOCATION_ID,
        appointment_type=APPOINTMENT_ESTIMATE,
        hcp_estimate_id='hcp-estimate-1',
        ghl_appointment_id='ghl-appt-1',
        starts_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
    )

    outcome = sync.sync_appointment(ghl_fixtures.appointment_event())

    assert outcome == SKIPPED
    assert hcp.estimate_schedules_updated == []
