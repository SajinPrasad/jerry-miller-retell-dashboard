"""The Celery tasks: account resolution, the activation gate, and how a delivery is settled.

Tasks are called directly rather than through a worker - `shared_task` leaves the function
callable, and what matters here is the decisions it makes, not Celery's transport.
"""

import pytest

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.hcp import tasks
from apps.hcp.models import HCPAccount, IntegratedContact, Job, JobLineItem
from apps.hcp.services.sync import HCPToGHLSync
from apps.hcp.tests import fixtures
from apps.hcp.tests.conftest import LOCATION_ID
from apps.hcp.tests.test_sync import FakeGHLObjects, FakeHCP

pytestmark = pytest.mark.django_db


@pytest.fixture
def active(account):
    """The fixture account, onboarded all the way through to activation."""
    HCPAccount.objects.filter(pk=account.pk).update(is_active=True)
    account.refresh_from_db()
    return account


@pytest.fixture
def ghl():
    return FakeGHLObjects()


@pytest.fixture(autouse=True)
def wired(monkeypatch, ghl):
    """Give every task the fake GHL and HCP, without changing how tasks build their sync."""
    hcp = FakeHCP()
    real_init = HCPToGHLSync.__init__

    def init(self, account, **kwargs):
        kwargs.setdefault('ghl', ghl)
        kwargs.setdefault('hcp', hcp)
        real_init(self, account, **kwargs)

    monkeypatch.setattr(HCPToGHLSync, '__init__', init)
    return hcp


def record(payload):
    return recording.webhook_received(
        WebhookEvent.HCP, payload, event_type=payload.get('event', '')
    )


# --- the activation gate -----------------------------------------------------------------


def test_an_event_for_an_unconnected_company_is_ignored(ghl):
    assert tasks.sync_customer(fixtures.customer_event()) == 'ignored'
    assert not IntegratedContact.objects.exists()
    assert ghl.contacts_created == []


def test_an_event_for_a_connected_but_inactive_account_is_ignored(account, ghl):
    """Until the backfill has run a live event would land ahead of the records it belongs
    with, so it is kept as a delivery and applied by replay once the account is activated.
    """
    assert account.is_active is False

    assert tasks.sync_customer(fixtures.customer_event()) == 'ignored'
    assert not IntegratedContact.objects.exists()


def test_an_ignored_delivery_is_marked_ignored_not_failed(account):
    event = record(fixtures.customer_event())

    tasks.sync_customer(fixtures.customer_event(), event.pk)

    event.refresh_from_db()
    assert event.status == WebhookEvent.IGNORED


def test_an_active_account_syncs(active, ghl):
    assert tasks.sync_customer(fixtures.customer_event()) == 'synced'
    assert IntegratedContact.objects.count() == 1


# --- settling the delivery ----------------------------------------------------------------


def test_a_synced_delivery_is_marked_processed(active):
    event = record(fixtures.customer_event())

    tasks.sync_customer(fixtures.customer_event(), event.pk)

    event.refresh_from_db()
    assert event.status == WebhookEvent.PROCESSED


def test_a_skipped_delivery_is_marked_ignored(active):
    """A customer too empty to mirror is a fact about the data, not a failure to retry."""
    payload = fixtures.customer_event(
        first_name=None, last_name=None, email=None, mobile_number=None, home_number=None
    )
    event = record(payload)

    assert tasks.sync_customer(payload, event.pk) == 'skipped'

    event.refresh_from_db()
    assert event.status == WebhookEvent.IGNORED


def test_a_raising_handler_leaves_the_delivery_failed_with_its_reason(active, monkeypatch):
    from apps.ghl.exceptions import GHLError

    def boom(self, payload):
        raise GHLError('GHL said no')

    monkeypatch.setattr(HCPToGHLSync, 'sync_customer', boom)
    event = record(fixtures.customer_event())

    with pytest.raises(GHLError):
        tasks.sync_customer(fixtures.customer_event(), event.pk)

    event.refresh_from_db()
    assert event.status == WebhookEvent.FAILED
    assert 'GHL said no' in event.error


def test_a_task_runs_without_a_webhook_event_too(active):
    """The backfill and `replay_webhook` both call these with no delivery to settle."""
    assert tasks.sync_customer(fixtures.customer_event()) == 'synced'


# --- what each task does --------------------------------------------------------------------


def test_the_lead_task_syncs_a_lead(active):
    from apps.hcp.models import Lead

    assert tasks.sync_lead(fixtures.lead_event()) == 'synced'
    assert Lead.objects.count() == 1


def test_the_estimate_task_syncs_an_estimate(active):
    from apps.hcp.models import Estimate

    assert tasks.sync_estimate(fixtures.estimate_event()) == 'synced'
    assert Estimate.objects.count() == 1


def test_the_job_task_syncs_the_job_and_its_line_items(active):
    """One task, so a job and the items hanging off it cannot be half-applied."""
    assert tasks.sync_job(fixtures.job_event()) == 'synced'

    assert Job.objects.count() == 1
    assert JobLineItem.objects.get().hcp_line_item_id == 'hcp-line-1'


def test_the_job_task_reads_line_items_from_hcp_when_the_event_carries_none(active, wired):
    wired._line_items = [fixtures.line_item('li-from-hcp')]

    tasks.sync_job(fixtures.job_event(line_items=None))

    assert wired.line_item_reads == 1
    assert JobLineItem.objects.get().hcp_line_item_id == 'li-from-hcp'


def test_the_job_task_does_not_reconcile_when_the_job_was_skipped(active, wired):
    anonymous = fixtures.customer(
        first_name=None, last_name=None, email=None, mobile_number=None, home_number=None
    )

    assert tasks.sync_job(fixtures.job_event(customer=anonymous)) == 'skipped'
    assert wired.line_item_reads == 0
    assert not JobLineItem.objects.exists()


def test_the_appointment_task_books_a_scheduled_appointment(active, ghl):
    from apps.hcp.models import AppointmentIntegration, HCPCalendarSettings, HCPEmployeeMapping

    mapping_row = HCPEmployeeMapping.objects.create(
        location_id=LOCATION_ID, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )
    HCPCalendarSettings.objects.create(
        location_id=LOCATION_ID,
        estimate_calendar_id='cal-est',
        job_calendar_id='cal-job',
        default_user=mapping_row,
    )

    assert tasks.sync_job_appointment(fixtures.appointment_event()) == 'synced'
    assert AppointmentIntegration.objects.count() == 1
    assert len(ghl.appointments_created) == 1


def test_a_discarded_appointment_goes_down_the_removal_path(active, ghl):
    from apps.hcp.models import AppointmentIntegration, HCPCalendarSettings, HCPEmployeeMapping

    mapping_row = HCPEmployeeMapping.objects.create(
        location_id=LOCATION_ID, hcp_employee_id='hcp-emp-1', ghl_user_id='ghl-user-1'
    )
    HCPCalendarSettings.objects.create(
        location_id=LOCATION_ID,
        estimate_calendar_id='cal-est',
        job_calendar_id='cal-job',
        default_user=mapping_row,
    )
    tasks.sync_job_appointment(fixtures.appointment_event())

    outcome = tasks.sync_job_appointment(
        fixtures.appointment_event('job.appointment.appointment_discarded')
    )

    assert outcome == 'synced'
    assert not AppointmentIntegration.objects.exists()
    assert len(ghl.appointments_deleted) == 1


# --- replay ---------------------------------------------------------------------------------


def test_a_stored_delivery_can_be_replayed(active):
    """Every handler is an upsert, which is what makes replay safe."""
    from apps.audit.replay import replay

    event = record(fixtures.customer_event())
    tasks.sync_customer(fixtures.customer_event(), event.pk)
    event.refresh_from_db()

    assert replay(event) == 'synced'
    assert IntegratedContact.objects.count() == 1


def test_replaying_a_failed_delivery_applies_it(active, monkeypatch):
    from apps.audit.replay import replay
    from apps.ghl.exceptions import GHLError

    event = record(fixtures.customer_event())
    real = HCPToGHLSync.sync_customer
    monkeypatch.setattr(
        HCPToGHLSync, 'sync_customer', lambda self, payload: (_ for _ in ()).throw(GHLError('x'))
    )
    with pytest.raises(GHLError):
        tasks.sync_customer(fixtures.customer_event(), event.pk)
    event.refresh_from_db()
    assert event.status == WebhookEvent.FAILED

    monkeypatch.setattr(HCPToGHLSync, 'sync_customer', real)
    assert replay(event) == 'synced'

    event.refresh_from_db()
    assert event.status == WebhookEvent.PROCESSED
    assert IntegratedContact.objects.count() == 1


def test_replaying_an_unhandled_event_is_ignored(active):
    from apps.audit.replay import replay

    event = record(fixtures.envelope('invoice.new', invoice={'id': 'i-1'}))

    assert replay(event) == 'ignored'
