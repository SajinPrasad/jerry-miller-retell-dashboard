"""Scheduled work for Retell rebilling."""

import logging

from celery import shared_task

from .services import RetellReconciliationService

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
