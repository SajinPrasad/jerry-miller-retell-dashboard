"""Celery app for the project's background work.

Scheduled jobs live in `CELERY_BEAT_SCHEDULE`; the workers that run them need both a worker and a
beat process pointed at `config` (see CLAUDE.md for the commands).
"""

import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@setup_logging.connect
def configure_logging(**kwargs):
    """Use Django's `LOGGING` for the worker and beat, instead of Celery's own handler.

    Without this Celery attaches a second handler of its own to the root logger, so every line
    is printed twice, once in each format, and the redaction filter only covers one of them.
    """
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)
