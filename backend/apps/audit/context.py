"""Who is doing what, for the logs and the audit trail.

Three values ride along with the current request or Celery task and are read by the log
formatter, the error handler and every audit row: a **request id** that ties one request's log
lines, webhook record and audit rows together; the **actor**, who triggered the work; and the
**webhook event** being processed, so a bill that moved because of a Stripe event points at the
payload that moved it.

Context variables, not thread-locals: they are per thread under Gunicorn and per task under
Celery, and `begin()` / `clear()` are called at the edges so a worker thread never carries one
request's actor into the next.
"""

import getpass
import sys
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar('request_id', default='')
_actor: ContextVar[tuple[str, str]] = ContextVar('actor', default=('', ''))
_webhook_event_id: ContextVar[int | None] = ContextVar('webhook_event_id', default=None)


def begin(request_id: str = '') -> str:
    """Start a fresh context (request or task). Returns the request id in use."""
    rid = request_id or uuid.uuid4().hex[:12]
    _request_id.set(rid)
    _actor.set(('', ''))
    _webhook_event_id.set(None)
    return rid


def clear() -> None:
    _request_id.set('')
    _actor.set(('', ''))
    _webhook_event_id.set(None)


def request_id() -> str:
    return _request_id.get()


def set_actor(actor: str, label: str = '') -> None:
    """`actor` is `user:<ghl user id>`, `webhook:<source>` or `celery:<task name>`."""
    _actor.set((actor, label))


def get_actor() -> tuple[str, str]:
    """The actor, or - when nothing set one - the shell user for a management command."""
    actor = _actor.get()
    if actor[0]:
        return actor
    if len(sys.argv) > 1 and sys.argv[0].endswith('manage.py'):
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001 - no passwd entry, odd container: still say it was a CLI
            user = 'unknown'
        return f'cli:{user}', sys.argv[1]
    return 'system', ''


def set_webhook_event(pk: int | None) -> None:
    _webhook_event_id.set(pk)


def webhook_event_id() -> int | None:
    return _webhook_event_id.get()
