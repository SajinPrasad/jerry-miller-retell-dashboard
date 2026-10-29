class RetellError(Exception):
    """A Retell request failed, or Retell returned something we can't use."""


class TwilioError(Exception):
    """A Twilio request failed, or Twilio returned something we can't use."""


class StripeBillingError(Exception):
    """A bill cannot be charged for a reason on our side, before Stripe is ever asked."""


class GHLInvoiceError(Exception):
    """A GHL invoice could not be raised, sent or voided."""
