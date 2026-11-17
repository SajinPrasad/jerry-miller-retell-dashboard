"""Activation: the four backfill stages, the switch-on, and the held deliveries it replays.

The HCP list reads and the GHL writes are both faked, so what is asserted is which records the
stages pass to the shared `HCPToGHLSync` handlers and what happens around them - the handlers
themselves are covered in `test_sync.py`.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.ghl.exceptions import GHLError, GHLRateLimitError
from apps.hcp import tasks
from apps.hcp.exceptions import HCPAuthError, HCPRateLimitError
from apps.hcp.models import (
    BACKFILL_COMPLETED,
    BACKFILL_FAILED,
    BACKFILL_RUNNING,
    HCPAccount,
    IntegratedContact,
    Job,
    JobLineItem,
    Lead,
)
from apps.hcp.services import backfill
from apps.hcp.services.sync import HCPToGHLSync
from apps.hcp.tests import fixtures
from apps.hcp.tests.test_sync import FakeGHLObjects

pytestmark = pytest.mark.django_db


class ListingHCP:
    """The HCP reads a backfill makes: the four lists, and one job's line items/appointments."""

    def __init__(self, customers=(), leads=(), estimates=(), jobs=(), appointments=()):
        self.customers = list(customers)
        self.leads = list(leads)
        self.estimates = list(estimates)
        self.jobs = list(jobs)
        self.appointments = list(appointments)
        self.line_item_reads = []
        self.appointment_reads = []
        self.error = None

    def _rows(self, rows):
        for row in rows:
            if self.error:
                raise self.error
            yield row

    def iter_customers(self):
        return self._rows(self.customers)

    def iter_leads(self):
        return self._rows(self.leads)

    def iter_estimates(self):
        return self._rows(self.estimates)

    def iter_jobs(self):
        return self._rows(self.jobs)

    def get_job_line_items(self, job_id):
        self.line_item_reads.append(job_id)
        return [fixtures.line_item()]

    def get_job_appointments(self, job_id):
        self.appointment_reads.append(job_id)
        return self.appointments


@pytest.fixture
def ghl():
    return FakeGHLObjects()


def make_backfill(account, ghl, **hcp_rows):
    hcp = ListingHCP(**hcp_rows)
    return backfill.Backfill(account, HCPToGHLSync(account, ghl=ghl, hcp=hcp)), hcp


# --- the stages ------------------------------------------------------------------------


def test_the_stages_run_in_dependency_order():
    """Everything hangs off a customer's contact, and a job links to its estimate."""
    assert backfill.STAGES == ('customers', 'leads', 'estimates', 'jobs')


def test_the_customer_stage_mirrors_every_customer_and_counts_outcomes(account, ghl):
    runner, _ = make_backfill(
        account,
        ghl,
        customers=[
            fixtures.customer('c-1'),
            fixtures.customer('c-2', email='two@example.test'),
            {'id': 'c-3'},  # nothing to identify them by
        ],
    )

    counts = runner.run_stage('customers')

    assert counts == {'synced': 2, 'skipped': 1}
    assert set(IntegratedContact.objects.values_list('hcp_customer_id', flat=True)) == {
        'c-1',
        'c-2',
    }


def test_a_stage_run_twice_changes_nothing_the_second_time(account, ghl):
    runner, _ = make_backfill(account, ghl, customers=[fixtures.customer('c-1')])

    runner.run_stage('customers')
    runner.run_stage('customers')

    assert IntegratedContact.objects.count() == 1
    assert len(ghl.contacts_created) == 1


def test_a_record_that_fails_is_counted_and_the_stage_carries_on(account, ghl):
    """One refused customer must not hold back the rest of the account."""
    real_create = ghl.create_contact

    def create(payload):
        if payload.get('email') == 'bad@example.test':
            raise GHLError('GHL refused the contact')
        return real_create(payload)

    ghl.create_contact = create
    runner, _ = make_backfill(
        account,
        ghl,
        customers=[
            fixtures.customer('c-1', email='bad@example.test'),
            fixtures.customer('c-2', email='good@example.test'),
        ],
    )

    counts = runner.run_stage('customers')

    assert counts == {'failed': 1, 'synced': 1}
    assert IntegratedContact.objects.get().hcp_customer_id == 'c-2'


@pytest.mark.parametrize('error', [HCPAuthError('bad key'), HCPRateLimitError('slow down')])
def test_a_rejected_key_or_exhausted_rate_limit_stops_the_stage(account, ghl, error):
    """Nothing after either would work, so counting it as one failed record would hide it."""
    runner, hcp = make_backfill(account, ghl, customers=[fixtures.customer('c-1')])
    hcp.error = error

    with pytest.raises(type(error)):
        runner.run_stage('customers')


def test_an_exhausted_ghl_rate_limit_aborts_the_stage_rather_than_counting_a_failure(account, ghl):
    ghl.create_contact = mock.Mock(side_effect=GHLRateLimitError('slow down'))
    runner, _ = make_backfill(account, ghl, customers=[fixtures.customer('c-1')])

    with pytest.raises(GHLRateLimitError):
        runner.run_stage('customers')


def test_a_stage_records_where_it_is_and_what_it_has_done(account, ghl):
    runner, _ = make_backfill(
        account, ghl, customers=[fixtures.customer('c-1')], leads=[fixtures.lead()]
    )

    runner.run_stage('customers')
    runner.run_stage('leads')

    account.refresh_from_db()
    assert account.backfill_stage == 'leads'
    assert account.backfill_progress == {'customers': {'synced': 1}, 'leads': {'synced': 1}}


def test_progress_is_written_part_way_through_a_stage(account, ghl, monkeypatch):
    monkeypatch.setattr(backfill, 'PROGRESS_EVERY', 2)
    seen = []
    runner, hcp = make_backfill(
        account,
        ghl,
        customers=[fixtures.customer(f'c-{n}', email=f'{n}@example.test') for n in range(5)],
    )
    real = runner._save_progress

    def spy(stage, counts):
        seen.append(sum(counts.values()))
        real(stage, counts)

    runner._save_progress = spy

    runner.run_stage('customers')

    assert seen == [2, 4, 5]


def test_leads_follow_the_customers_they_belong_to(account, ghl):
    runner, _ = make_backfill(
        account, ghl, customers=[fixtures.customer()], leads=[fixtures.lead()]
    )

    runner.run_stage('customers')
    counts = runner.run_stage('leads')

    assert counts == {'synced': 1}
    assert Lead.objects.get().hcp_lead_id == 'hcp-lead-1'


def test_a_lead_naming_no_customer_is_skipped_not_failed(account, ghl):
    runner, _ = make_backfill(account, ghl, leads=[fixtures.lead(customer={})])

    assert runner.run_stage('leads') == {'skipped': 1}


def test_the_job_stage_also_reconciles_line_items_and_books_appointments(account, ghl):
    runner, hcp = make_backfill(
        account,
        ghl,
        customers=[fixtures.customer()],
        jobs=[fixtures.job()],
        appointments=[fixtures.appointment()],
    )
    runner.run_stage('customers')

    with mock.patch.object(
        runner.sync, 'sync_job_appointment', wraps=runner.sync.sync_job_appointment
    ) as book:
        counts = runner.run_stage('jobs')

    assert counts == {'synced': 1}
    assert Job.objects.get().hcp_job_id == 'hcp-job-1'
    assert hcp.line_item_reads == ['hcp-job-1']
    assert hcp.appointment_reads == ['hcp-job-1']
    assert book.call_count == 1
    assert book.call_args.args[1]['id'] == 'hcp-appt-1'


def test_line_items_are_read_from_hcp_not_trusted_from_the_list_row(account, ghl):
    """An empty `line_items` on a list row can mean none or not included; reconciliation deletes
    whatever is missing, so the list row's own value must never decide it.
    """
    runner, hcp = make_backfill(
        account,
        ghl,
        customers=[fixtures.customer()],
        jobs=[fixtures.job(line_items=[])],
    )
    runner.run_stage('customers')

    runner.run_stage('jobs')

    assert hcp.line_item_reads == ['hcp-job-1']
    assert JobLineItem.objects.count() == 1


def test_a_job_that_does_not_sync_reads_nothing_more(account, ghl):
    runner, hcp = make_backfill(account, ghl, jobs=[fixtures.job(customer={})])

    assert runner.run_stage('jobs') == {'skipped': 1}
    assert hcp.line_item_reads == []
    assert hcp.appointment_reads == []


# --- switching on -----------------------------------------------------------------------


def held_event(account, event_type='customer.created', company_id=None, **overrides):
    payload = fixtures.customer_event(event_type)
    payload['company_id'] = company_id or account.hcp_company_id
    stored = recording.webhook_received(WebhookEvent.HCP, payload, event_type=event_type)
    recording.webhook_status(stored, WebhookEvent.IGNORED)
    return stored


def test_activate_switches_the_account_on(account):
    backfill.activate(account, timezone.now() - timedelta(minutes=1))

    account.refresh_from_db()
    assert account.is_active is True


def test_activate_replays_what_arrived_while_it_was_off(account, ghl, monkeypatch):
    monkeypatch.setattr(
        HCPToGHLSync, '__init__', _with_fakes(HCPToGHLSync.__init__, ghl, ListingHCP())
    )
    since = timezone.now() - timedelta(minutes=1)
    event = held_event(account)

    result = backfill.activate(account, since)

    assert result == {'replayed': 1, 'failed': 0}
    assert IntegratedContact.objects.filter(hcp_customer_id='hcp-customer-1').exists()
    event.refresh_from_db()
    assert event.status == WebhookEvent.PROCESSED


def test_activate_leaves_other_companies_and_older_deliveries_alone(account, ghl, monkeypatch):
    monkeypatch.setattr(
        HCPToGHLSync, '__init__', _with_fakes(HCPToGHLSync.__init__, ghl, ListingHCP())
    )
    older = held_event(account)
    since = timezone.now() + timedelta(seconds=1)
    held_event(account, company_id='another-company')
    HCPAccount.objects.filter(pk=account.pk).update(is_active=False)

    result = backfill.activate(account, since)

    assert result == {'replayed': 0, 'failed': 0}
    older.refresh_from_db()
    assert older.status == WebhookEvent.IGNORED
    assert not IntegratedContact.objects.exists()


def test_a_held_delivery_that_fails_is_counted_and_the_rest_still_replay(account, ghl, monkeypatch):
    monkeypatch.setattr(
        HCPToGHLSync, '__init__', _with_fakes(HCPToGHLSync.__init__, ghl, ListingHCP())
    )
    real_create = ghl.create_contact

    def create(payload):
        if payload.get('email') == 'bad@example.test':
            raise GHLError('GHL refused the contact')
        return real_create(payload)

    ghl.create_contact = create
    since = timezone.now() - timedelta(minutes=1)
    first = fixtures.customer_event()
    first['customer']['email'] = 'bad@example.test'
    for payload in (first, fixtures.customer_event()):
        payload['company_id'] = account.hcp_company_id
        stored = recording.webhook_received(
            WebhookEvent.HCP, payload, event_type='customer.created'
        )
        recording.webhook_status(stored, WebhookEvent.IGNORED)

    assert backfill.activate(account, since) == {'replayed': 1, 'failed': 1}


def _with_fakes(real_init, ghl, hcp):
    def init(self, account, **kwargs):
        kwargs.setdefault('ghl', ghl)
        kwargs.setdefault('hcp', hcp)
        real_init(self, account, **kwargs)

    return init


# --- the tasks --------------------------------------------------------------------------


def test_start_backfill_chains_the_stages_then_the_activation(monkeypatch):
    captured = {}

    class FakeChain:
        def __init__(self, *signatures):
            captured['signatures'] = signatures

        def delay(self):
            captured['queued'] = True

    monkeypatch.setattr(tasks, 'chain', FakeChain)

    tasks.start_backfill('loc-hcp-1')

    names = [signature.task.rsplit('.', 1)[-1] for signature in captured['signatures']]
    assert names == ['backfill_stage'] * 4 + ['finish_backfill']
    assert [s.args[1] for s in captured['signatures'][:4]] == list(backfill.STAGES)
    assert captured['queued'] is True


def test_the_stage_task_runs_one_stage_for_the_account(account, ghl, monkeypatch):
    monkeypatch.setattr(
        HCPToGHLSync,
        '__init__',
        _with_fakes(HCPToGHLSync.__init__, ghl, ListingHCP(customers=[fixtures.customer()])),
    )

    assert tasks.backfill_stage(account.location_id, 'customers') == {'synced': 1}


def test_the_finish_task_activates_the_account_and_completes_the_backfill(account):
    backfill.mark_running(account)
    started = (timezone.now() - timedelta(minutes=1)).isoformat()

    tasks.finish_backfill(account.location_id, started)

    account.refresh_from_db()
    assert account.is_active is True
    assert account.backfill_status == BACKFILL_COMPLETED


def test_mark_running_starts_a_clean_record(account):
    HCPAccount.objects.filter(pk=account.pk).update(
        backfill_status=BACKFILL_FAILED,
        backfill_stage='jobs',
        backfill_progress={'customers': {'synced': 9}},
    )

    backfill.mark_running(account)

    account.refresh_from_db()
    assert (account.backfill_status, account.backfill_stage, account.backfill_progress) == (
        BACKFILL_RUNNING,
        '',
        {},
    )


def test_an_aborting_stage_marks_the_backfill_failed(account, ghl, monkeypatch):
    backfill.mark_running(account)
    monkeypatch.setattr(
        HCPToGHLSync,
        '__init__',
        _with_fakes(HCPToGHLSync.__init__, ghl, _failing_hcp(HCPAuthError('bad key'))),
    )

    with pytest.raises(HCPAuthError):
        tasks.backfill_stage(account.location_id, 'customers')

    account.refresh_from_db()
    assert account.backfill_status == BACKFILL_FAILED
    assert account.backfill_stage == 'customers'
    assert account.is_active is False


def test_a_failing_activation_marks_the_backfill_failed(account, monkeypatch):
    backfill.mark_running(account)
    monkeypatch.setattr(backfill, 'activate', mock.Mock(side_effect=RuntimeError('boom')))

    with pytest.raises(RuntimeError):
        tasks.finish_backfill(account.location_id, timezone.now().isoformat())

    account.refresh_from_db()
    assert account.backfill_status == BACKFILL_FAILED


def _failing_hcp(error):
    hcp = ListingHCP(customers=[fixtures.customer()])
    hcp.error = error
    return hcp
