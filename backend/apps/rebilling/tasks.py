"""Scheduled work for voice rebilling: the Retell audit, the Twilio pull and charge retries."""

import logging

from celery import shared_task

from .services import (
    RetellReconciliationService,
    TwilioUsageService,
    calculate_bills,
    previous_billing_period,
)
from .stripe_billing import charge_bills, retry_due_bills

logger = logging.getLogger(__name__)


@shared_task
def reconcile_retell_calls() -> dict:
    """The nightly audit, on Celery beat. Same work as `manage.py reconcile_retell_calls`.

    A `RetellError` is left to fail the task: Celery logs it, and the next night covers the same
    window again, so there is nothing worth retrying here.
    """
    result = RetellReconciliationService().reconcile()
    logger.info('retell: nightly reconciliation finished %s', result)
    return result


@shared_task
def pull_twilio_usage() -> dict:
    """The daily Twilio pull, on Celery beat. Same work as `manage.py pull_twilio_usage`.

    Reads the rolling window. A single client's failure is counted rather than raised, so one
    unreachable subaccount never costs the others their day; the count lands in the log line.
    """
    result = TwilioUsageService().pull()
    logger.info('twilio: daily usage pull finished %s', result)
    return result


@shared_task
def run_monthly_billing() -> dict:
    """The monthly close, on Celery beat: calculate and charge bills for the month that just
    ended. Same work as `manage.py calculate_bills` and `manage.py charge_bills`, chained.

    Scheduled 04:00 UTC on the 1st, an hour after the Retell audit, so the final night of the
    closing month is already reconciled before it is billed. Each step isolates a single client's
    failure from the rest of the run on its own
    (`calculate_bills`, `charge_bills`); this only chains the two for the period that just ended.
    """
    period = previous_billing_period()
    calculated = calculate_bills(period)
    charged = charge_bills(period)
    result = {'period': period, 'calculated': calculated, 'charged': charged}
    logger.info('billing: monthly run finished %s', result)
    return result


@shared_task
def retry_failed_charges() -> dict:
    """The automatic retries of declined charges, on Celery beat. Same work as
    `manage.py retry_failed_charges`.

    Only bills whose next retry is due are touched, so an hourly run is safe and never retries
    early. A declined retry is recorded on its bill rather than raised, so one client's card
    cannot stop the run for the others.
    """
    result = retry_due_bills()
    logger.info('billing: retry run finished %s', result)
    return result
