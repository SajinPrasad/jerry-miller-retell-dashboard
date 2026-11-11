"""The logging, webhook record, audit trail and Celery task record."""

import hashlib
import hmac
import json
import logging
import time
from types import SimpleNamespace

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.audit import context, recording
from apps.audit.logs import DatabaseHandler, JsonFormatter, RedactingFilter
from apps.audit.models import AuditEvent, ErrorLog, TaskRun, WebhookEvent
from apps.audit.redaction import REDACTED, redact, scrub, strip_retell_pii
from apps.audit.replay import ReplayRefused, replay
from apps.rebilling.models import Bill, ClientMapping, RateHistory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_context():
    context.clear()
    yield
    context.clear()


@pytest.fixture
def client_row():
    return ClientMapping.objects.create(location_id='loc1', client_name='Cool Breeze')


# --- redaction -------------------------------------------------------------------------------


class TestRedaction:
    def test_known_setting_values_are_replaced_wherever_they_appear(self):
        text = 'upstream said: bad key retell-test-key for twilio-test-token'
        out = redact(text)
        assert 'retell-test-key' not in out
        assert 'twilio-test-token' not in out

    def test_credential_shapes_are_replaced(self):
        for secret in (
            'sk_live_abcdef123456',
            'whsec_abcdef123456',
            'Bearer abc.def-ghi',
            'eyJhbGciOi.eyJzdWIiOi.sig_nature',
            'U2FsdGVkX1+abcDEF/123==',
        ):
            assert secret.split()[-1] not in redact(f'error near {secret} end')

    def test_key_value_pairs_naming_a_credential_are_replaced(self):
        out = redact('{"access_token": "abc123", "refresh_token": "xyz789", "name": "Bob"}')
        assert 'abc123' not in out and 'xyz789' not in out
        assert 'Bob' in out

    def test_oauth_callback_code_is_replaced_but_a_decline_code_is_not(self):
        assert 'abc' not in redact('GET /api/oauth/callback/?code=abc&state=1')
        assert 'card_declined' in redact("{'code': 'card_declined'}")

    def test_scrub_blanks_credential_keys_in_structured_data(self):
        out = scrub({'a': {'client_secret': 'cs_1', 'ok': 1}, 'list': [{'password': 'p'}]})
        assert out == {'a': {'client_secret': REDACTED, 'ok': 1}, 'list': [{'password': REDACTED}]}

    def test_retell_payload_loses_transcript_and_recording_but_keeps_billing_fields(self):
        call = {
            'call_id': 'c1',
            'duration_ms': 61000,
            'transcript': 'hello',
            'transcript_object': [{'content': 'hi'}],
            'recording_url': 'https://x/y.wav',
            'public_log_url': 'https://x/log',
            'call_cost': {'combined_cost': 12.5},
        }
        out = strip_retell_pii({'event': 'call_ended', 'call': call})['call']
        assert out == {'call_id': 'c1', 'duration_ms': 61000, 'call_cost': {'combined_cost': 12.5}}


# --- logging ---------------------------------------------------------------------------------


def _record(msg, *args, level=logging.INFO, name='apps.test', exc_info=None, **extra):
    record = logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestLogging:
    def test_json_line_carries_request_actor_and_context_and_no_secret(self):
        context.begin('req123')
        context.set_actor('user:u1')
        record = _record(
            'charge failed with key retell-test-key',
            level=logging.ERROR,
            context={'location_id': 'loc1', 'api_key': 'shh'},
        )
        RedactingFilter().filter(record)
        data = json.loads(JsonFormatter().format(record))

        assert data['request_id'] == 'req123'
        assert data['actor'] == 'user:u1'
        assert data['level'] == 'ERROR'
        assert data['context'] == {'location_id': 'loc1', 'api_key': REDACTED}
        assert 'retell-test-key' not in json.dumps(data)

    def test_traceback_is_redacted(self):
        try:
            raise ValueError('boom sk_test_abcdef123456')
        except ValueError:
            import sys

            record = _record('failed', level=logging.ERROR, exc_info=sys.exc_info())
        RedactingFilter().filter(record)
        text = JsonFormatter().format(record)
        assert 'sk_test_abcdef123456' not in text
        assert 'ValueError' in text

    def test_errors_and_app_warnings_are_kept_in_the_database(self):
        handler = DatabaseHandler()
        handler.emit(
            _record(
                'no rate for loc1',
                level=logging.WARNING,
                name='apps.rebilling.services',
                context={'location_id': 'loc1'},
            )
        )
        handler.emit(_record('library error', level=logging.ERROR, name='celery.app.trace'))
        handler.emit(_record('library chatter', level=logging.WARNING, name='django.request'))
        handler.emit(_record('routine', level=logging.INFO, name='apps.rebilling.services'))

        rows = {row.logger: row for row in ErrorLog.objects.all()}
        assert set(rows) == {'apps.rebilling.services', 'celery.app.trace'}
        assert rows['apps.rebilling.services'].context == {'location_id': 'loc1'}

    def test_a_failing_insert_never_raises_into_the_caller(self, monkeypatch):
        monkeypatch.setattr(ErrorLog.objects, 'create', lambda **kw: 1 / 0)
        DatabaseHandler().emit(_record('x', level=logging.ERROR))  # must not raise

    def test_stored_message_is_redacted(self):
        record = _record('bad key retell-test-key', level=logging.ERROR)
        DatabaseHandler().emit(record)
        assert 'retell-test-key' not in ErrorLog.objects.get().message


# --- audit trail -----------------------------------------------------------------------------


class TestAuditTrail:
    def test_bill_status_transition_records_who_before_and_after(self, client_row):
        bill = Bill.objects.create(client=client_row, billing_period='2026-10', total_cents=900)
        context.set_actor('user:u1', 'Jerry <j@x.com>')

        bill.status = 'failed'
        bill.failure_reason = 'card_declined'
        bill.save(update_fields=['status', 'failure_reason'])

        event = AuditEvent.objects.get(action='bill.status_changed')
        assert event.actor == 'user:u1'
        assert event.actor_label == 'Jerry <j@x.com>'
        assert event.location_id == 'loc1' and event.billing_period == '2026-10'
        assert event.before == {'status': 'uncharged', 'failure_reason': ''}
        assert event.after == {'status': 'failed', 'failure_reason': 'card_declined'}

    def test_creation_records_the_full_row(self, client_row):
        Bill.objects.create(client=client_row, billing_period='2026-10', total_cents=900)
        event = AuditEvent.objects.get(action='bill.created')
        assert event.before is None
        assert event.after['total_cents'] == 900

    def test_a_save_that_changes_nothing_records_nothing(self, client_row):
        bill = Bill.objects.create(client=client_row, billing_period='2026-10')
        before = AuditEvent.objects.count()
        bill.save()
        assert AuditEvent.objects.count() == before

    def test_rate_and_mapping_changes_are_recorded(self, client_row):
        # Mapping creation seeds the default rate, and both are recorded.
        assert AuditEvent.objects.filter(action='client_mapping.created').exists()
        assert AuditEvent.objects.filter(action='rate.created').exists()

        client_row.phone_numbers = ['+15551234567']
        client_row.save(update_fields=['phone_numbers'])
        event = AuditEvent.objects.get(action='client_mapping.updated')
        assert event.before == {'phone_numbers': []}
        assert event.after == {'phone_numbers': ['+15551234567']}

        rate = RateHistory.objects.get(client=client_row)
        rate.voice_rate_cents = 30
        rate.save()
        assert AuditEvent.objects.get(action='rate.updated').after == {'voice_rate_cents': 30}

    def test_rows_are_append_only(self, client_row):
        event = AuditEvent.objects.first()
        with pytest.raises(RuntimeError):
            event.save()
        with pytest.raises(RuntimeError):
            event.delete()

    def test_a_failed_audit_write_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(AuditEvent.objects, 'create', lambda **kw: 1 / 0)
        assert recording.record('x') is None

    def test_detail_is_scrubbed(self):
        recording.record('x', detail={'api_key': 'shh', 'ok': 1})
        assert AuditEvent.objects.get(action='x').detail == {'api_key': REDACTED, 'ok': 1}

    def test_the_actor_of_an_admin_request_is_the_ghl_user(self, client_row):
        from django.urls import reverse

        from apps.ghl.tests.test_sso import VECTOR_AGENCY, make_agency
        from apps.rebilling.tests.test_admin_views import make_location

        make_agency()
        make_location('loc1', 'Cool Breeze')
        response = APIClient().put(
            reverse('rebilling:admin-client-rates', args=['loc1']),
            data={'voice_rate_cents': 31, 'call_rate_cents': 6},
            format='json',
            HTTP_X_GHL_SESSION=VECTOR_AGENCY,
        )
        assert response.status_code == 200

        event = AuditEvent.objects.filter(action__startswith='rate.').latest('id')
        assert event.actor == 'user:user-1'
        assert 'Jerry Miller' in event.actor_label and 'jerry@example.test' in event.actor_label
        assert event.location_id == 'loc1'
        assert event.after['voice_rate_cents'] == 31


# --- webhooks --------------------------------------------------------------------------------


def _retell_signed(body: bytes) -> str:
    """Retell's `v=<ms>,d=<hmac sha256 of body+ms>` header."""
    ts = str(int(time.time() * 1000))
    digest = hmac.new(b'retell-test-key', body + ts.encode(), hashlib.sha256).hexdigest()
    return f'v={ts},d={digest}'


@pytest.fixture
def api():
    return APIClient()


class TestWebhookRecord:
    def _post_retell(self, api, payload):
        body = json.dumps(payload).encode()
        return api.post(
            '/api/rebilling/webhooks/retell/',
            data=body,
            content_type='application/json',
            HTTP_X_RETELL_SIGNATURE=_retell_signed(body),
        )

    def test_retell_delivery_is_stored_without_transcript_and_processed(self, api, client_row):
        client_row.retell_agent_ids = ['agent1']
        client_row.save()
        payload = {
            'event': 'call_ended',
            'call': {
                'call_id': 'c1',
                'agent_id': 'agent1',
                'call_status': 'ended',
                'start_timestamp': 1_700_000_000_000,
                'duration_ms': 61000,
                'transcript': 'private words',
                'recording_url': 'https://x/y.wav',
            },
        }
        assert self._post_retell(api, payload).status_code == 204

        event = WebhookEvent.objects.get()
        assert event.source == 'retell' and event.event_id == 'c1'
        assert event.status == 'processed' and event.signature_valid
        assert 'transcript' not in event.payload['call']
        assert 'recording_url' not in event.payload['call']
        assert event.payload['call']['duration_ms'] == 61000

    def test_other_retell_events_are_kept_and_marked_ignored(self, api):
        self._post_retell(api, {'event': 'call_started', 'call': {'call_id': 'c9'}})
        assert WebhookEvent.objects.get().status == 'ignored'

    def test_bad_signature_is_recorded_without_a_body(self, api):
        response = api.post(
            '/api/rebilling/webhooks/retell/',
            data=b'{"event": "call_ended"}',
            content_type='application/json',
            HTTP_X_RETELL_SIGNATURE='nope',
        )
        assert response.status_code == 401
        event = WebhookEvent.objects.get()
        assert event.status == 'rejected' and event.payload is None
        assert not event.signature_valid

    def test_a_handler_crash_marks_the_delivery_failed_with_the_traceback(self, api, monkeypatch):
        def boom(call):
            raise RuntimeError('database exploded')

        monkeypatch.setattr('apps.rebilling.views.store_call', boom)
        body = json.dumps({'event': 'call_ended', 'call': {'call_id': 'c1'}}).encode()
        with pytest.raises(RuntimeError):
            api.post(
                '/api/rebilling/webhooks/retell/',
                data=body,
                content_type='application/json',
                HTTP_X_RETELL_SIGNATURE=_retell_signed(body),
            )
        event = WebhookEvent.objects.get()
        assert event.status == 'failed'
        assert 'database exploded' in event.error and 'RuntimeError' in event.error
        assert event.payload['call']['call_id'] == 'c1'  # still there to replay

    def test_replaying_a_failed_delivery_applies_it_and_is_audited(self, client_row):
        client_row.retell_agent_ids = ['agent1']
        client_row.save()
        event = WebhookEvent.objects.create(
            source='retell',
            event_type='call_ended',
            signature_valid=True,
            status='failed',
            error='boom',
            payload={
                'event': 'call_ended',
                'call': {
                    'call_id': 'c7',
                    'agent_id': 'agent1',
                    'call_status': 'ended',
                    'start_timestamp': 1_700_000_000_000,
                    'duration_ms': 1000,
                },
            },
        )
        assert replay(event) == 'stored'

        event.refresh_from_db()
        assert event.status == 'processed'
        audit = AuditEvent.objects.get(action='webhook.replayed')
        assert audit.object_id == str(event.pk)
        assert audit.detail['previous']['status'] == 'failed'

    def test_a_rejected_delivery_cannot_be_replayed(self):
        event = WebhookEvent.objects.create(source='stripe', status='rejected')
        with pytest.raises(ReplayRefused):
            replay(event)

    @override_settings(STRIPE_WEBHOOK_SECRET='whsec_test_secret')
    def test_stripe_delivery_changes_carry_the_webhook_actor_and_event(self, api, client_row):
        import stripe

        bill = Bill.objects.create(
            client=client_row,
            billing_period='2026-10',
            total_cents=900,
            status='charging',
            stripe_invoice_id='in_1',
        )
        payload = json.dumps(
            {
                'id': 'evt_1',
                'object': 'event',
                'type': 'invoice.paid',
                'created': 1_700_000_000,
                'data': {'object': {'id': 'in_1', 'object': 'invoice'}},
            }
        )
        ts = int(time.time())
        signed = f'{ts}.{payload}'.encode()
        sig = hmac.new(b'whsec_test_secret', signed, hashlib.sha256).hexdigest()
        response = api.post(
            '/api/rebilling/webhooks/stripe/',
            data=payload,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE=f't={ts},v1={sig}',
        )
        assert response.status_code == 204
        assert stripe  # SDK verified the signature above

        event = WebhookEvent.objects.get(source='stripe')
        assert event.event_id == 'evt_1' and event.status == 'processed'
        audit = AuditEvent.objects.get(action='bill.status_changed')
        assert audit.actor == 'webhook:stripe'
        assert audit.webhook_event_id == event.pk
        assert audit.before['status'] == 'charging' and audit.after['status'] == 'paid'
        bill.refresh_from_db()
        assert bill.status == 'paid'


# --- request context -------------------------------------------------------------------------


class TestMiddleware:
    def test_response_carries_a_request_id_and_the_context_is_cleared(self, api):
        response = api.get('/api/ghl/onboarding/', HTTP_X_REQUEST_ID='abc123')
        assert response['X-Request-ID'] == 'abc123'
        assert context.request_id() == ''


# --- celery ----------------------------------------------------------------------------------


class TestTaskRuns:
    def test_a_successful_task_leaves_a_run_with_its_result(self):
        from apps.audit import signals

        task = SimpleNamespace(
            name='apps.rebilling.tasks.x', request=SimpleNamespace(hostname='w1')
        )
        signals._task_started(task_id='t1', task=task, args=[], kwargs={})
        assert context.get_actor()[0] == 'celery:apps.rebilling.tasks.x'
        signals._task_finished(task_id='t1', task=task, retval={'failed': 0}, state='SUCCESS')

        run = TaskRun.objects.get(task_id='t1')
        assert run.status == 'success' and run.result == {'failed': 0}
        assert run.finished_at is not None and run.worker == 'w1'

    def test_a_failed_task_keeps_its_traceback(self):
        from apps.audit import signals

        task = SimpleNamespace(
            name='apps.rebilling.tasks.x', request=SimpleNamespace(hostname='w1')
        )
        signals._task_started(task_id='t2', task=task, args=[], kwargs={})
        try:
            raise RuntimeError('retell down')
        except RuntimeError as exc:
            signals._task_failed(task_id='t2', exception=exc, traceback=exc.__traceback__)
        signals._task_finished(task_id='t2', task=task, retval=None, state='FAILURE')

        run = TaskRun.objects.get(task_id='t2')
        assert run.status == 'failure'
        assert 'retell down' in run.error and 'RuntimeError' in run.error

    def test_recording_a_run_never_breaks_the_task(self, monkeypatch):
        from apps.audit import signals

        monkeypatch.setattr(TaskRun.objects, 'create', lambda **kw: 1 / 0)
        task = SimpleNamespace(name='t', request=SimpleNamespace(hostname=''))
        signals._task_started(task_id='t3', task=task, args=[], kwargs={})  # must not raise


# --- edge cases ------------------------------------------------------------------------------


class TestEdgeCases:
    def test_benign_payload_fields_are_not_blanked_but_credentials_are(self):
        payload = {
            'total_tokens': 120,
            'llm_token_usage': {'average': 3},
            'checkout_session': 'cs_1',
            'off_session': True,
            'access_token': 'abc',
            'client_secret': 'cs',
            'x-ghl-session': 'blob',
            'api_key': 'k',
        }
        out = scrub(payload)
        assert out['total_tokens'] == 120
        assert out['llm_token_usage'] == {'average': 3}
        assert out['checkout_session'] == 'cs_1' and out['off_session'] is True
        for key in ('access_token', 'client_secret', 'x-ghl-session', 'api_key'):
            assert out[key] == REDACTED

    def test_processing_restores_the_context_it_found(self):
        context.set_actor('user:u1')
        event = WebhookEvent.objects.create(source='retell', signature_valid=True, payload={})
        with recording.processing(event):
            assert context.get_actor()[0] == 'webhook:retell'
            assert context.webhook_event_id() == event.pk
        assert context.get_actor()[0] == 'user:u1'
        assert context.webhook_event_id() is None

    def test_replay_restores_the_context_even_when_the_handler_fails(self, monkeypatch):
        from apps.audit import replay as replay_module

        def boom(payload):
            raise RuntimeError('nope')

        monkeypatch.setitem(replay_module.HANDLERS, 'retell', boom)
        event = WebhookEvent.objects.create(source='retell', signature_valid=True, payload={})
        with pytest.raises(RuntimeError):
            replay(event)
        assert context.webhook_event_id() is None

    def test_a_replay_that_ignores_the_event_leaves_it_ignored(self):
        event = WebhookEvent.objects.create(
            source='retell',
            signature_valid=True,
            status='failed',
            payload={'event': 'call_started'},
        )
        assert replay(event) == 'ignored'
        event.refresh_from_db()
        assert event.status == 'ignored'

    @pytest.mark.parametrize('body', [b'[]', b'5', b'"text"', b'null'])
    def test_non_object_bodies_are_rejected_with_400(self, api, settings, body):
        retell = api.post(
            '/api/rebilling/webhooks/retell/',
            data=body,
            content_type='application/json',
            HTTP_X_RETELL_SIGNATURE=_retell_signed(body),
        )
        card = api.post(
            '/api/rebilling/webhooks/ghl-card/',
            data=body,
            content_type='application/json',
            HTTP_X_CARD_WEBHOOK_TOKEN='card-webhook-token',
        )
        assert retell.status_code == 400 and card.status_code == 400
        events = WebhookEvent.objects.all()
        assert {e.status for e in events} == {'rejected'} and events.count() == 2

    def test_non_object_body_on_the_ghl_location_webhook_is_rejected(self, api, monkeypatch):
        monkeypatch.setattr('apps.ghl.views._verify_webhook_signature', lambda body, sig: True)
        response = api.post(
            '/api/ghl/webhooks/location-created/', data=b'[]', content_type='application/json'
        )
        assert response.status_code == 400
        assert WebhookEvent.objects.get().status == 'rejected'

    def test_a_retell_call_that_is_not_an_object_is_tolerated(self, api):
        body = json.dumps({'event': 'call_ended', 'call': 'oops'}).encode()
        response = api.post(
            '/api/rebilling/webhooks/retell/',
            data=body,
            content_type='application/json',
            HTTP_X_RETELL_SIGNATURE=_retell_signed(body),
        )
        assert response.status_code == 204
