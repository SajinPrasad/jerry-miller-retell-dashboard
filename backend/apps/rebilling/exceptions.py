class RetellError(Exception):
    """A Retell request failed, or Retell returned something we can't use."""


class TwilioError(Exception):
    """A Twilio request failed, or Twilio returned something we can't use."""
