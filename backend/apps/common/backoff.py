"""Waiting out a rate limit: how long to wait before the next attempt, and the wait itself.

Shared by the two clients that retry a 429 (`apps/ghl/services.py`, `apps/hcp/services/
hcp_client.py`), so both back off the same way and a test patches one `pause`.
"""

import time

# Retries after the first attempt, for a caller that opted in to waiting.
MAX_RETRIES = 3
BASE_WAIT_SECONDS = 1
# A ceiling on one wait, whatever the server asked for: a worker sleeping for minutes on a
# single request is worse than failing it, and a failed delivery can be replayed.
MAX_WAIT_SECONDS = 30


def wait_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait before retry number `attempt + 1`.

    The server's own `Retry-After` wins when it sent one; otherwise 1s, 2s, 4s, ... Either way
    never more than `MAX_WAIT_SECONDS`.
    """
    wait = retry_after if retry_after is not None else BASE_WAIT_SECONDS * 2**attempt
    return min(wait, MAX_WAIT_SECONDS)


def pause(seconds: float) -> None:
    time.sleep(seconds)
