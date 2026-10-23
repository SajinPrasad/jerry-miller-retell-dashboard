"""Scheduled work for voice rebilling: the Retell audit and the Twilio pull."""

import logging

from celery import shared_task

from .services import RetellReconciliationService, TwilioUsageService

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
