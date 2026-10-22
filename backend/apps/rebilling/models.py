"""Retell voice rebilling: who a call belongs to, what it cost, and what we charge for it.

Retell bills Jerry for every client's voice AI as one bill. These models split that usage per GHL
sub-account so each client can be billed at the rate Jerry set for them.
"""

import math
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


def billable_minutes(duration_ms: int | None) -> Decimal:
    """Minutes we bill for a call, under the configured rounding rule."""
    if not duration_ms:
        return Decimal(0)
    if settings.RETELL_ROUNDING == ROUND_UP_MINUTE:
        return Decimal(math.ceil(duration_ms / MS_PER_MINUTE))
    return Decimal(duration_ms) / MS_PER_MINUTE


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
    billing_mode = models.CharField(max_length=20, choices=BILLING_MODES, default=AUTO_CHARGE)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default='')
    billing_contact_id = models.CharField(max_length=255, blank=True, default='')
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'rebilling_client_mapping'
        ordering = ['client_name']

    def __str__(self):
        return f'ClientMapping({self.location_id}, {self.client_name!r})'


class RateHistory(models.Model):
    """What Jerry charges one client per AI minute, from `start_date` onwards.

    A rate change adds a row instead of editing one, so usage before the change still bills at the
    rate that was in effect then.
    """

    client = models.ForeignKey(ClientMapping, on_delete=models.CASCADE, related_name='rates')
    voice_rate_cents = models.PositiveIntegerField(default=25)
    start_date = models.DateField()

    class Meta:
        db_table = 'rebilling_rate_history'
        ordering = ['-start_date']
        unique_together = [('client', 'start_date')]

    def __str__(self):
        return f'RateHistory({self.client_id}, {self.voice_rate_cents}c from {self.start_date})'


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

    class Meta:
        db_table = 'rebilling_unassigned_call'
        ordering = ['-started_at']

    def __str__(self):
        return f'UnassignedCall({self.call_id}, agent={self.agent_id!r})'
