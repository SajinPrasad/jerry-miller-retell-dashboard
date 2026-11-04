import httpx
import pytest
import respx
from celery.schedules import crontab
from django.conf import settings

from apps.rebilling.models import RetellCall
from apps.rebilling.tasks import reconcile_retell_calls

from .test_reconciliation import LIST_URL, mock_list, page, recent_call
from .test_services import make_workspace

pytestmark = pytest.mark.django_db

SCHEDULE_NAME = 'reconcile-retell-calls'


@respx.mock
def test_the_daily_task_runs_the_audit():
    make_workspace()
    mock_list(page(recent_call()))

    result = reconcile_retell_calls()

    assert result == {'workspaces': 1, 'failed': 0, 'seen': 1, 'stored': 1, 'backfilled': 0}
    assert RetellCall.objects.filter(call_id='call1').exists()


@respx.mock
def test_one_unreadable_workspace_is_counted_rather_than_raised():
    """A bad key is one client's problem; raising would cost everyone else the night."""
    make_workspace()
    respx.post(LIST_URL).mock(return_value=httpx.Response(500))

    result = reconcile_retell_calls()

    assert result['failed'] == 1 and result['stored'] == 0


def test_beat_runs_the_audit_once_a_day():
    """The dotted path must be the task's own name, or beat only finds out at 03:00."""
    entry = settings.CELERY_BEAT_SCHEDULE[SCHEDULE_NAME]

    assert entry['task'] == reconcile_retell_calls.name
    assert entry['schedule'] == crontab(hour=3, minute=0)


def test_the_project_exposes_the_celery_app():
    """Without this import in `config/__init__.py`, `@shared_task` binds to no app."""
    import config

    assert config.celery_app.main == 'config'
