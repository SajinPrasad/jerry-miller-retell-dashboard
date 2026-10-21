class GHLError(Exception):
    """A GHL request failed, or GHL returned something we can't use."""


class GHLNotConnectedError(GHLError):
    """The agency has not installed the app yet (or uninstalled it)."""
