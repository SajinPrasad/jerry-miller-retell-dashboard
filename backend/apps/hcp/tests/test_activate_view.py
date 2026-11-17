"""Step 4: `POST /api/hcp/accounts/<location_id>/activate/`.

The backfill itself is `test_backfill.py`'s; this file is about who may start it and when.
"""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.ghl.tests.test_sso import VECTOR_AGENCY, make_agency
from apps.hcp import tasks
from apps.hcp.models import (
    BACKFILL_FAILED,
    BACKFILL_RUNNING,
    HCPAccount,
    HCPCalendarSettings,
)
from apps.hcp.services import onboarding
from apps.hcp.tests.conftest import LOCATION_ID
from apps.hcp.tests.test_onboarding import make_mapping
from apps.hcp.tests.test_views import OTHER_LOCATION

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture(autouse=True)
def agency():
    return make_agency(company_id='comp_demo')


@pytest.fixture
def started(monkeypatch):
    """The backfills queued, instead of a broker being needed."""
    queued: list[str] = []
    monkeypatch.setattr(tasks, 'start_backfill', queued.append)
    return queued


@pytest.fixture
def ready(account):
    """The account with every earlier onboarding step done."""
    make_mapping()
    HCPCalendarSettings.objects.create(
        location_id=LOCATION_ID, estimate_calendar_id='cal-est', job_calendar_id='cal-job'
    )
    return account


def activate(api, blob=VECTOR_AGENCY):
    return api.post(
        reverse('hcp:activate', args=[LOCATION_ID]), format='json', HTTP_X_GHL_SESSION=blob
    )


def test_a_ready_account_starts_the_backfill_and_answers_202(api, ready, started):
    response = activate(api)

    assert response.status_code == 202
    assert started == [LOCATION_ID]
    # Not active yet - it turns active when the backfill finishes - but no longer idle either.
    assert response.json()['state'] == onboarding.BACKFILLING
    assert response.json()['is_active'] is False
    assert response.json()['backfill'] == {'status': 'running', 'stage': '', 'progress': {}}
    account = HCPAccount.objects.get(location_id=LOCATION_ID)
    assert account.is_active is False
    assert account.backfill_status == BACKFILL_RUNNING


def test_a_second_activate_while_running_is_a_409(api, ready, started):
    assert activate(api).status_code == 202

    response = activate(api)

    assert response.status_code == 409
    assert response.json()['code'] == 'backfill_running'
    assert started == [LOCATION_ID]


def test_a_failed_backfill_can_be_started_again_and_resets_its_progress(api, ready, started):
    HCPAccount.objects.filter(pk=ready.pk).update(
        backfill_status=BACKFILL_FAILED,
        backfill_stage='leads',
        backfill_progress={'customers': {'synced': 3}},
    )

    response = activate(api)

    assert response.status_code == 202
    assert response.json()['backfill'] == {'status': 'running', 'stage': '', 'progress': {}}
    assert started == [LOCATION_ID]


def test_status_reports_stage_and_progress_while_running(api, ready):
    HCPAccount.objects.filter(pk=ready.pk).update(
        backfill_status=BACKFILL_RUNNING,
        backfill_stage='leads',
        backfill_progress={'customers': {'synced': 40, 'skipped': 2}},
    )

    body = api.get(
        reverse('hcp:status', args=[LOCATION_ID]), HTTP_X_GHL_SESSION=VECTOR_AGENCY
    ).json()

    assert body['state'] == onboarding.BACKFILLING
    assert body['backfill'] == {
        'status': 'running',
        'stage': 'leads',
        'progress': {'customers': {'synced': 40, 'skipped': 2}},
    }


def test_a_failed_backfill_reads_ready_to_activate_with_the_failure_shown(api, ready):
    HCPAccount.objects.filter(pk=ready.pk).update(
        backfill_status=BACKFILL_FAILED, backfill_stage='jobs'
    )

    body = api.get(
        reverse('hcp:status', args=[LOCATION_ID]), HTTP_X_GHL_SESSION=VECTOR_AGENCY
    ).json()

    assert body['state'] == onboarding.READY_TO_ACTIVATE
    assert body['backfill']['status'] == 'failed'
    assert body['backfill']['stage'] == 'jobs'


@pytest.mark.parametrize(
    ('state', 'message'),
    [
        ('mappings', 'Map the Housecall Pro employees first.'),
        ('calendars', 'Configure the calendars first.'),
    ],
)
def test_an_account_missing_an_earlier_step_is_refused(api, account, started, state, message):
    if state == 'calendars':
        make_mapping()

    response = activate(api)

    assert response.status_code == 400
    assert response.json()['detail'] == message
    assert started == []


def test_an_active_account_is_refused(api, ready, started):
    HCPAccount.objects.filter(pk=ready.pk).update(is_active=True)

    response = activate(api)

    assert response.status_code == 400
    assert response.json()['detail'] == 'This account is already active.'
    assert started == []


def test_an_unconnected_sub_account_is_a_404(api, started):
    assert activate(api).status_code == 404
    assert started == []


def test_activation_needs_a_session(api, ready, started):
    response = api.post(reverse('hcp:activate', args=[LOCATION_ID]), format='json')

    assert response.status_code in (401, 403)
    assert started == []


def test_a_sub_account_cannot_activate_another(api, ready, started):
    response = activate(api, blob=OTHER_LOCATION)

    assert response.status_code == 403
    assert started == []
