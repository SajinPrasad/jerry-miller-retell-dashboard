"""What can go wrong talking to Housecall Pro.

Split by what the caller can *do* about it, not by status code: a bad key needs a human to paste
a new one, a rate limit needs waiting, and everything else is a retry-or-report. The onboarding
views map each of these to a different response, which is the whole point of separating them.
"""


class HCPError(Exception):
    """An HCP request failed, or HCP returned something we can't use."""


class HCPAuthError(HCPError):
    """HCP rejected the API key (401/403).

    The key is admin-generated in Housecall Pro and shown once, so this is never recoverable in
    code - someone has to generate a new one and paste it in again.
    """


class HCPConfigurationError(HCPError):
    """The onboarding step was refused by a rule on our side, before HCP or GHL was asked.

    Carries `details` so the view can hand the page something it can render field by field -
    which users are missing from which calendar, say - rather than one string to display whole.
    """

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class HCPRateLimitError(HCPError):
    """HCP answered 429.

    `retry_after` is the seconds HCP asked us to wait, when it said; `None` when it did not.
    Phase 2 only reports this (the caller is a human clicking a button, who can click again);
    the backfill/sync phases are where an automatic backoff belongs.
    """

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
