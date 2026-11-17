class GHLError(Exception):
    """A GHL request failed, or GHL returned something we can't use."""


class GHLNotConnectedError(GHLError):
    """The agency has not installed the app yet (or uninstalled it)."""


class GHLRateLimitError(GHLError):
    """GHL answered 429. `retry_after` is the seconds it asked us to wait, when it said."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
