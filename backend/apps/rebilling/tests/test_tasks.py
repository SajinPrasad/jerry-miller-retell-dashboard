import httpx
import pytest
import respx
from celery.schedules import crontab
from django.conf import settings

from apps.rebilling.exceptions import RetellError
from apps.rebilling.models import RetellCall
from apps.rebilling.tasks import reconcile_retell_calls

from .test_reconciliation import LIST_URL, mock_list, page, recent_call
from .test_services import make_client

pytestmark = pytest.mark.django_db

SCHEDULE_NAME = 'reconcile-retell-calls'


@respx.mock
def test_the_daily_task_runs_the_audit():
    make_client()
    mock_list(page(recent_call()))

    result = reconcile_retell_calls()

    assert result == {'seen': 1, 'stored': 1, 'backfilled': 0}
    assert RetellCall.objects.filter(call_id='call1').exists()


@respx.mock
def test_the_daily_task_fails_when_retell_does():
    """Celery records the failure; the next night's window covers the same calls."""
    respx.post(LIST_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(RetellError):
        reconcile_retell_calls()


def test_beat_runs_the_audit_once_a_day():
    """The dotted path must be the task's own name, or beat only finds out at 03:00."""
    entry = settings.CELERY_BEAT_SCHEDULE[SCHEDULE_NAME]

    assert entry['task'] == reconcile_retell_calls.name
    assert entry['schedule'] == crontab(hour=3, minute=0)


def test_the_project_exposes_the_celery_app():
    """Without this import in `config/__init__.py`, `@shared_task` binds to no app."""
    import config

    assert config.celery_app.main == 'config'
