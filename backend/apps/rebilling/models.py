"""Retell voice rebilling: who a call belongs to, what it cost, and what we charge for it.

Retell bills Jerry for every client's voice AI as one bill. These models split that usage per GHL
sub-account so each client can be billed at the rate Jerry set for them.
"""

import math
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import models

MS_PER_MINUTE = 60_000

AUTO_CHARGE = 'auto_charge'
SEND_INVOICE = 'send_invoice'
BILLING_MODES = [(AUTO_CHARGE, 'Auto charge'), (SEND_INVOICE, 'Send invoice')]

# Values for settings.RETELL_ROUNDING.
PER_SECOND = 'per_second'
ROUND_UP_MINUTE = 'round_up_minute'

# The only call_status that can be billed. Calls are stored whatever their status.
ENDED = 'ended'

# Values for `billing_status` on a stored call.
BILLABLE = 'billable'
UNBILLABLE = 'unbillable'
BILLING_STATUSES = [(BILLABLE, 'Billable'), (UNBILLABLE, 'Unbillable')]

# The one Twilio usage category we bill. It excludes SIP and trunking calls by Twilio's own
# definition, which is what keeps Retell's minutes (they run over a SIP trunk) out of it.
CALLS = 'calls'


def billable_minutes(duration_ms: int | None) -> Decimal:
    """Minutes we bill for a call, under the configured rounding rule."""
    if not duration_ms:
        return Decimal(0)
    if settings.RETELL_ROUNDING == ROUND_UP_MINUTE:
        return Decimal(math.ceil(duration_ms / MS_PER_MINUTE))
    return Decimal(duration_ms) / MS_PER_MINUTE


def billing_state(
    duration_ms: int | None, started_at: datetime | None, call_status: str
) -> tuple[str, str]:
    """Whether a call can be billed, and in plain words why not.

    A call missing any of this is marked rather than dropped: the nightly audit re-reads it while
    it is still inside the window, so most of these resolve themselves within a day.
    """
    if call_status != ENDED:
        return UNBILLABLE, f'Call status is {call_status or "missing"}, not {ENDED}.'
    if duration_ms is None:
        return UNBILLABLE, 'Retell has not reported a duration for this call.'
    if started_at is None:
        return UNBILLABLE, 'Retell has not reported a start time, so no rate period applies.'
    return BILLABLE, ''


class ClientMapping(models.Model):
    """One client: their GHL sub-account, their Retell agents and how they get billed.

    Retell knows nothing about GHL sub-accounts, so this row is what links a call to a client.
    """

    location_id = models.CharField(max_length=255, unique=True)
    client_name = models.CharField(max_length=255)
    # Primary match for a call. Each sub-account gets its own agent.
    retell_agent_ids = models.JSONField(default=list, blank=True)
    # Backup match, compared digits-only because these are typed in by hand.
    phone_numbers = models.JSONField(default=list, blank=True)
    # The client's own Twilio subaccount. Blank = no Twilio voice billing for this client.
    twilio_subaccount_sid = models.CharField(max_length=64, blank=True, default='')
    billing_mode = models.CharField(max_length=20, choices=BILLING_MODES, default=AUTO_CHARGE)
    # Where the usage bill goes. Also the key an existing Stripe customer is looked up by, so
    # a client who already pays Jerry through Stripe is reused rather than duplicated.
    billing_email = models.EmailField(blank=True, default='')
    stripe_customer_id = models.CharField(max_length=255, blank=True, default='')
    billing_contact_id = models.CharField(max_length=255, blank=True, default='')
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'rebilling_client_mapping'
        ordering = ['client_name']

    def __str__(self):
        return f'ClientMapping({self.location_id}, {self.client_name!r})'


class RateHistory(models.Model):
    """What Jerry charges one client per minute, from `start_date` onwards.

    A rate change adds a row instead of editing one, so usage before the change still bills at the
    rate that was in effect then. One timeline carries both rates, so a mid-month change splits
    Retell and Twilio usage at the same point.
    """

    client = models.ForeignKey(ClientMapping, on_delete=models.CASCADE, related_name='rates')
    # The Retell AI minute. Named before there was a second kind of voice minute; kept as-is
    # because a rename costs a migration and a code sweep and buys no behaviour.
    voice_rate_cents = models.PositiveIntegerField(default=25)
    # The Twilio regular-call minute.
    call_rate_cents = models.PositiveIntegerField(default=5)
    start_date = models.DateField()

    class Meta:
        db_table = 'rebilling_rate_history'
        ordering = ['-start_date']
        unique_together = [('client', 'start_date')]

    def __str__(self):
        return (
            f'RateHistory({self.client_id}, ai={self.voice_rate_cents}c, '
            f'call={self.call_rate_cents}c from {self.start_date})'
        )


class RetellCall(models.Model):
    """A Retell call matched to a client. Only `ended` calls are billed."""

    call_id = models.CharField(max_length=255, unique=True)
    client = models.ForeignKey(ClientMapping, on_delete=models.PROTECT, related_name='calls')
    agent_id = models.CharField(max_length=255, blank=True, default='')
    from_number = models.CharField(max_length=32, blank=True, default='')
    to_number = models.CharField(max_length=32, blank=True, default='')
    direction = models.CharField(max_length=16, blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    # call_cost.combined_cost: what Retell charges Jerry, kept so we can show his margin.
    retell_cost_cents = models.PositiveIntegerField(null=True, blank=True)
    call_status = models.CharField(max_length=32, blank=True, default='')
    # Set from `billing_state` whenever the call is stored or re-read. Defaults to unbillable so a
    # row created by hand is never billed until something has checked it.
    billing_status = models.CharField(max_length=20, choices=BILLING_STATUSES, default=UNBILLABLE)
    # Why it can't be billed, for the admin page. Empty when it can.
    unbillable_reason = models.CharField(max_length=255, blank=True, default='')
    # The bill this call was billed on, if any. Deleting a bill never deletes the call itself —
    # only unlinks it.
    bill = models.ForeignKey(
        'Bill', on_delete=models.SET_NULL, null=True, blank=True, related_name='retell_calls'
    )

    class Meta:
        db_table = 'rebilling_retell_call'
        ordering = ['-started_at']

    def __str__(self):
        return f'RetellCall({self.call_id}, {self.client_id})'

    @property
    def billable_minutes(self) -> Decimal:
        return billable_minutes(self.duration_ms)


class UnassignedCall(models.Model):
    """A call that matched no agent and no phone number.

    Kept rather than dropped: once someone maps the agent or number, it gets billed in that period.
    """

    call_id = models.CharField(max_length=255, unique=True)
    agent_id = models.CharField(max_length=255, blank=True, default='')
    from_number = models.CharField(max_length=32, blank=True, default='')
    to_number = models.CharField(max_length=32, blank=True, default='')
    direction = models.CharField(max_length=16, blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    retell_cost_cents = models.PositiveIntegerField(null=True, blank=True)
    call_status = models.CharField(max_length=32, blank=True, default='')
    # Set from `billing_state` whenever the call is stored or re-read. Defaults to unbillable so a
    # row created by hand is never billed until something has checked it.
    billing_status = models.CharField(max_length=20, choices=BILLING_STATUSES, default=UNBILLABLE)
    # Why it can't be billed, for the admin page. Empty when it can.
    unbillable_reason = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'rebilling_unassigned_call'
        ordering = ['-started_at']

    def __str__(self):
        return f'UnassignedCall({self.call_id}, agent={self.agent_id!r})'


class TwilioUsageDay(models.Model):
    """One Twilio usage category, for one client, for one GMT day.

    Twilio reports usage as a daily aggregate per category rather than per call, and keeps
    revising a day after it ends, so a re-pull **updates** this row. That is the opposite of
    `RetellCall`, which is written once on the first webhook and never touched again.
    """

    client = models.ForeignKey(ClientMapping, on_delete=models.PROTECT, related_name='twilio_usage')
    subaccount_sid = models.CharField(max_length=64)
    # Twilio's `start_date`: a GMT calendar day, not Jerry's local one.
    usage_date = models.DateField()
    # `calls` today. This column is what lets SMS arrive later as data rather than as schema.
    category = models.CharField(max_length=64, default=CALLS)
    call_count = models.PositiveIntegerField(default=0)
    # Twilio has already rounded each call up to its billed increment before summing the day, so
    # `RETELL_ROUNDING` must never be applied on top of this number.
    usage_minutes = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal(0))
    # Twilio's `price` in cents: what Twilio charges Jerry, kept so we can show his margin.
    twilio_price_cents = models.PositiveIntegerField(default=0)
    # Twilio's own freshness stamp — the moment through which this day's data is current.
    as_of = models.DateTimeField(null=True, blank=True)
    # The bill this day was billed on, if any. Deleting a bill never deletes the usage day itself —
    # only unlinks it.
    bill = models.ForeignKey(
        'Bill', on_delete=models.SET_NULL, null=True, blank=True, related_name='twilio_lines'
    )

    class Meta:
        db_table = 'rebilling_twilio_usage_day'
        ordering = ['-usage_date']
        # The idempotency key: Twilio's answer to `call_id`.
        unique_together = [('client', 'usage_date', 'category')]

    def __str__(self):
        return f'TwilioUsageDay({self.client_id}, {self.usage_date}, {self.category})'


# Values for `BillLine.line_type`.
RETELL_LINE = 'retell'
TWILIO_LINE = 'twilio'
LINE_TYPES = [(RETELL_LINE, 'Retell AI minutes'), (TWILIO_LINE, 'Twilio call minutes')]

# Values for `Bill.status`. A bill is calculated long before it is charged, and the charge is
# answered by a webhook rather than by the call that started it, so these are four real states
# and not a progress bar: `charging` means Stripe has the invoice and we are waiting to hear.
UNCHARGED = 'uncharged'
CHARGING = 'charging'
PAID = 'paid'
FAILED = 'failed'
BILL_STATUSES = [
    (UNCHARGED, 'Uncharged'),
    (CHARGING, 'Charging'),
    (PAID, 'Paid'),
    (FAILED, 'Failed'),
]


class Bill(models.Model):
    """One bill for one client for one month.

    Covers both Retell AI minutes and Twilio regular call minutes. Lines are added per source
    only when there is usage of that source, so a client billed for only one source produces one
    line.
    """

    client = models.ForeignKey(ClientMapping, on_delete=models.PROTECT, related_name='bills')
    # The billing period as YYYY-MM.
    billing_period = models.CharField(max_length=7)
    # Total amount to bill, in cents. Sum of all lines.
    total_cents = models.PositiveIntegerField(default=0)
    # Where this bill is in the charging run. Calculation leaves it `uncharged`.
    status = models.CharField(max_length=20, choices=BILL_STATUSES, default=UNCHARGED)
    # The Stripe invoice this bill was charged on. Set once the invoice exists, which is also
    # what the webhook looks the bill up by.
    stripe_invoice_id = models.CharField(max_length=255, blank=True, default='')
    # When the Stripe invoice was finalised, which is the moment the card was first attempted.
    # The webhook is the only thing that moves a bill off `charging`, so this is the clock that
    # makes a missing webhook visible: a bill still `charging` long after this was set was
    # either never answered or never collected.
    charged_at = models.DateTimeField(null=True, blank=True)
    # When Stripe told us it was paid. Empty until then, including while `charging`.
    paid_at = models.DateTimeField(null=True, blank=True)
    # Why the last attempt failed, as Stripe's own error code: `card_declined`,
    # `authentication_required` (the card needs the client there to re-authorise it), and so on.
    # The code is what separates "send them a new card" from "ask them to confirm this one",
    # which a bare `failed` cannot. Cleared when a bill is paid, because Stripe's own retries
    # can collect one that has already failed once.
    failure_reason = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'rebilling_bill'
        ordering = ['-billing_period']
        # One bill per client per period, never charged twice.
        unique_together = [('client', 'billing_period')]

    def __str__(self):
        return f'Bill({self.client_id}, {self.billing_period}, {self.total_cents}¢)'


class BillLine(models.Model):
    """One line item on a bill: either Retell or Twilio."""

    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name='lines')
    line_type = models.CharField(max_length=20, choices=LINE_TYPES)
    # Quantity in minutes, for the human to verify.
    quantity_minutes = models.DecimalField(max_digits=12, decimal_places=2)
    # Jerry's cost for this line (what the provider charged him), in cents.
    cost_cents = models.PositiveIntegerField()
    # What we charge the client, in cents. Calculated from quantity × rate at time of billing.
    bill_cents = models.PositiveIntegerField()

    class Meta:
        db_table = 'rebilling_bill_line'
        ordering = ['line_type']

    def __str__(self):
        return f'BillLine({self.bill_id}, {self.line_type}, {self.bill_cents}¢)'
