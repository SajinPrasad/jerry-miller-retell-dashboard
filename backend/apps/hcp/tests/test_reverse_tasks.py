"""The Celery tasks behind the GHL reverse-sync webhook: account resolution and the activation
gate, mirroring `test_tasks.py` for the forward direction.
"""

import pytest

from apps.audit import recording
from apps.audit.models import WebhookEvent
from apps.hcp import reverse_tasks as tasks
from apps.hcp.models import HCPAccount, IntegratedContact
from apps.hcp.services.reverse_sync import GHLToHCPSync
from apps.hcp.tests import ghl_fixtures
from apps.hcp.tests.test_reverse_sync import FakeGHLObjects, FakeHCPClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def active(account):
    HCPAccount.objects.filter(pk=account.pk).update(is_active=True)
    account.refresh_from_db()
    return account


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    """Give every task a fake GHL and HCP, without changing how tasks build their sync."""
    ghl = FakeGHLObjects()
    hcp = FakeHCPClient()
    real_init = GHLToHCPSync.__init__

    def init(self, account, **kwargs):
        kwargs.setdefault('ghl', ghl)
        kwargs.setdefault('hcp', hcp)
        real_init(self, account, **kwargs)

    monkeypatch.setattr(GHLToHCPSync, '__init__', init)
    return hcp


def record(payload):
    return recording.webhook_received(WebhookEvent.GHL, payload, event_type=payload.get('type', ''))


def test_an_event_for_an_unconnected_location_is_ignored():
    assert tasks.sync_ghl_contact_create(ghl_fixtures.contact_event()) == 'ignored'
    assert not IntegratedContact.objects.exists()


def test_an_event_for_a_connected_but_inactive_account_is_ignored(account):
    assert account.is_active is False

    assert tasks.sync_ghl_contact_create(ghl_fixtures.contact_event()) == 'ignored'
    assert not IntegratedContact.objects.exists()


def test_an_event_for_an_active_account_is_handled(active, wired):
    outcome = tasks.sync_ghl_contact_create(ghl_fixtures.contact_event())

    assert outcome == 'synced'
    assert wired.customers_created


def test_the_stored_event_is_settled_ignored_when_the_handler_skips(active):
    event = record(ghl_fixtures.contact_event(firstName='', lastName='', email='', phone=''))

    outcome = tasks.sync_ghl_contact_create(
        ghl_fixtures.contact_event(firstName='', lastName='', email='', phone=''), event.pk
    )

    event.refresh_from_db()
    assert outcome == 'skipped'
    assert event.status == WebhookEvent.IGNORED


def test_the_stored_event_for_an_unconnected_location_is_settled_ignored():
    event = record(ghl_fixtures.contact_event())

    tasks.sync_ghl_contact_create(ghl_fixtures.contact_event(), event.pk)

    event.refresh_from_db()
    assert event.status == WebhookEvent.IGNORED


def test_a_successful_sync_leaves_the_stored_event_processed(active):
    event = record(ghl_fixtures.contact_event())

    tasks.sync_ghl_contact_create(ghl_fixtures.contact_event(), event.pk)

    event.refresh_from_db()
    assert event.status == WebhookEvent.PROCESSED
