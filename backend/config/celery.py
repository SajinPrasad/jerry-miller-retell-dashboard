"""Celery app for the project's background work.

Scheduled jobs live in `CELERY_BEAT_SCHEDULE`; the workers that run them need both a worker and a
beat process pointed at `config` (see CLAUDE.md for the commands).
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
