"""Logging: one JSON line per record on stdout, credentials redacted, errors kept in the database.

systemd's journal collects stdout from Gunicorn, the Celery worker and Celery beat, so the same
formatter serves all three. Anything at WARNING or above from this app, and anything at ERROR or
above from anywhere, is also written to `ErrorLog`: the journal is bounded by disk and age, and
an error from weeks ago is exactly the one somebody asks about.

Structured context goes in `extra={'context': {...}}` on a log call - ids and amounts, never
credentials - and lands in the JSON line and in `ErrorLog.context`.
"""

import json
import logging
import threading
import traceback
from datetime import UTC, datetime

from . import context
from .redaction import redact, scrub

# Noise a public endpoint attracts. In the journal, never in the table.
_NOT_PERSISTED = ('django.security.DisallowedHost', 'django.server')


class RedactingFilter(logging.Filter):
    """Redacts the message and the traceback in place, before any formatter sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, '_redacted', False):
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)))
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
            record._redacted = True
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        actor, _ = context.get_actor()
        data = {
            'ts': datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec='milliseconds'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'request_id': context.request_id(),
            'actor': actor,
        }
        extra = getattr(record, 'context', None)
        if extra:
            data['context'] = scrub(extra)
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)))
        if record.exc_text:
            data['exception'] = record.exc_text
        return json.dumps(data, default=str)


class DatabaseHandler(logging.Handler):
    """Writes errors and warnings to `ErrorLog`. Never raises and never recurses."""

    _guard = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._guard, 'busy', False) or not self._wanted(record):
            return
        self._guard.busy = True
        try:
            from django.db import connection, transaction

            # A transaction already failed: any insert would raise, and on Postgres would also
            # poison what is left of the caller's work.
            if connection.in_atomic_block and connection.needs_rollback:
                return

            from .models import ErrorLog

            message = redact(record.getMessage())
            text = record.exc_text or (
                redact(''.join(traceback.format_exception(*record.exc_info)))
                if record.exc_info
                else ''
            )
            actor, _ = context.get_actor()
            # A savepoint, so a failed insert cannot abort the transaction it was logged from.
            with transaction.atomic():
                ErrorLog.objects.create(
                    level=record.levelname,
                    logger=record.name[:255],
                    message=message,
                    exception_type=(record.exc_info[0].__name__ if record.exc_info else '')[:255],
                    traceback=text,
                    context=scrub(getattr(record, 'context', None) or {}),
                    request_id=context.request_id(),
                    actor=actor[:255],
                )
        except Exception:  # noqa: BLE001 - a log handler must never take the caller down
            pass
        finally:
            self._guard.busy = False

    @staticmethod
    def _wanted(record: logging.LogRecord) -> bool:
        if record.name in _NOT_PERSISTED:
            return False
        if record.levelno >= logging.ERROR:
            return True
        return record.levelno >= logging.WARNING and record.name.startswith('apps.')
