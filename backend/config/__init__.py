"""Expose the Celery app so `@shared_task` binds to it when Django starts."""

from .celery import app as celery_app

__all__ = ('celery_app',)
