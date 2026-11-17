"""Activation: pull a connected company's history into GoHighLevel, then switch it on.

Onboarding stops with the account `is_active=False`, which means webhooks are recorded and
ignored. Activation runs the four stages below, in order, through the **same handlers the webhook
uses** (`HCPToGHLSync`) - one code path per entity, whichever way a record arrives - and only
then flips `is_active`. The account stays inactive for the whole backfill on purpose: a live
event applied mid-way could land ahead of the customer or estimate it belongs with. Deliveries
that arrive meanwhile are kept, and `activate` replays them once the account is live.

The order is the dependency order: leads, estimates and jobs all hang off a customer's GHL
contact, and a job links to its estimate. One record at a time, so the load on HCP and GHL is a
single stream, and either client waits out a 429 (`apps.common.backoff`) rather than failing it.
"""

import logging
from collections import Counter

from apps.audit import replay
from apps.audit.models import WebhookEvent
from apps.ghl.exceptions import GHLError, GHLRateLimitError
from apps.hcp.exceptions import HCPAuthError, HCPError, HCPRateLimitError
from apps.hcp.models import (
    BACKFILL_COMPLETED,
    BACKFILL_FAILED,
    BACKFILL_RUNNING,
    HCPAccount,
    Job,
)
from apps.hcp.services import mapping
from apps.hcp.services.sync import SYNCED_OUTCOME, HCPToGHLSync

logger = logging.getLogger(__name__)

STAGES = ('customers', 'leads', 'estimates', 'jobs')

# How many records between progress writes: often enough for a page to move, rare enough that a
# large account is not one database write per record.
PROGRESS_EVERY = 25


def mark_running(account: HCPAccount) -> None:
    """Start a backfill's record: running, nothing done yet."""
    HCPAccount.objects.filter(pk=account.pk).update(
        backfill_status=BACKFILL_RUNNING, backfill_stage='', backfill_progress={}
    )


def mark_failed(location_id: str) -> None:
    HCPAccount.objects.filter(location_id=location_id).update(backfill_status=BACKFILL_FAILED)


class Backfill:
    """One connected account's history, stage by stage."""

    def __init__(self, account: HCPAccount, sync: HCPToGHLSync | None = None):
        self.account = account
        self.sync = sync or HCPToGHLSync(account)

    def run_stage(self, stage: str) -> dict:
        """Mirror every HCP record of one kind. Returns how many ended in each outcome.

        A record GHL or HCP refuses is counted as `failed` and logged, and the stage carries
        on: one malformed customer must not hold back the other four thousand. Everything is an
        upsert, so running the stage again repairs what failed. A rejected API key or a rate
        limit that outlasts the backoff stops the stage instead - nothing after it would work.
        """
        rows, handle = {
            'customers': (self.sync.hcp.iter_customers, self.sync.sync_customer),
            'leads': (self.sync.hcp.iter_leads, self.sync.sync_lead),
            'estimates': (self.sync.hcp.iter_estimates, self.sync.sync_estimate),
            'jobs': (self.sync.hcp.iter_jobs, self._sync_job),
        }[stage]

        HCPAccount.objects.filter(pk=self.account.pk).update(backfill_stage=stage)
        counts: Counter = Counter()
        for record in rows():
            try:
                counts[handle(record)] += 1
            except (HCPAuthError, HCPRateLimitError, GHLRateLimitError):
                raise
            except (HCPError, GHLError) as exc:
                counts['failed'] += 1
                logger.warning(
                    'hcp: backfill %s %s failed: %s',
                    stage,
                    mapping.text(record.get('id')),
                    exc,
                    extra={'context': {'location_id': self.account.location_id}},
                )
            if sum(counts.values()) % PROGRESS_EVERY == 0:
                self._save_progress(stage, counts)
        self._save_progress(stage, counts)
        logger.info(
            'hcp: backfill %s finished for %s: %s',
            stage,
            self.account.location_id,
            dict(counts),
            extra={'context': {'location_id': self.account.location_id}},
        )
        return dict(counts)

    def _save_progress(self, stage: str, counts: Counter) -> None:
        # Re-read, so a write for one stage never clobbers the counts another has stored.
        current = HCPAccount.objects.values_list('backfill_progress', flat=True).get(
            pk=self.account.pk
        )
        HCPAccount.objects.filter(pk=self.account.pk).update(
            backfill_progress={**current, stage: dict(counts)}
        )

    def _sync_job(self, job: dict) -> str:
        """A job, its line items, then its appointments.

        The line items are read from HCP rather than taken from the list row: an empty list
        there could mean "none" or "not included", and reconciliation deletes what is missing.
        """
        outcome = self.sync.sync_job(job)
        if outcome != SYNCED_OUTCOME:
            return outcome
        row = Job.objects.get(
            location_id=self.account.location_id, hcp_job_id=mapping.text(job.get('id'))
        )
        self.sync.reconcile_line_items(row)
        for appointment in self.sync.hcp.get_job_appointments(row.hcp_job_id):
            self.sync.sync_job_appointment(job, appointment)
        return outcome


def activate(account: HCPAccount, since) -> dict:
    """Switch the account on, then apply the deliveries that were held back while it was off.

    `since` is when activation started: every HCP delivery for this company recorded after that
    was ignored by the inactive gate. Replayed oldest first, through the ordinary replay path,
    so each one is audited as a replay. One failing is counted and the rest still run.
    """
    account.is_active = True
    account.backfill_status = BACKFILL_COMPLETED
    account.save(update_fields=['is_active', 'backfill_status', 'updated_at'])

    held = WebhookEvent.objects.filter(
        source=WebhookEvent.HCP,
        status=WebhookEvent.IGNORED,
        received_at__gte=since,
        payload__company_id=account.hcp_company_id,
    ).order_by('id')

    replayed = failed = 0
    for event in held:
        try:
            replay.replay(event)
            replayed += 1
        except (HCPError, GHLError):
            failed += 1
    logger.info(
        'hcp: activated %s; replayed %s held deliveries (%s failed)',
        account.location_id,
        replayed,
        failed,
        extra={'context': {'location_id': account.location_id}},
    )
    return {'replayed': replayed, 'failed': failed}
