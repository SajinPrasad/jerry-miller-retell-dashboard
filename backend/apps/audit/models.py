from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class WebhookEvent(models.Model):
    """One inbound webhook delivery, kept as received so it can be inspected and replayed.

    The payload is stored **before** it is processed, so a bug in the handler never costs the
    event. Retell's transcript, recording links and analysis are stripped (personal content that
    billing never reads); credentials are blanked by key name. A delivery that fails its
    signature check is recorded without a body: the caller is unauthenticated, and the body is
    theirs to make up.
    """

    RETELL = 'retell'
    STRIPE = 'stripe'
    GHL = 'ghl'
    GHL_CARD = 'ghl_card'
    SOURCES = [
        (RETELL, 'Retell'),
        (STRIPE, 'Stripe'),
        (GHL, 'GoHighLevel'),
        (GHL_CARD, 'GoHighLevel card workflow'),
    ]

    RECEIVED = 'received'
    PROCESSED = 'processed'
    IGNORED = 'ignored'
    FAILED = 'failed'
    REJECTED = 'rejected'
    STATUSES = [
        (RECEIVED, 'Received'),
        (PROCESSED, 'Processed'),
        (IGNORED, 'Ignored'),
        (FAILED, 'Failed'),
        (REJECTED, 'Rejected'),
    ]

    source = models.CharField(max_length=20, choices=SOURCES)
    event_type = models.CharField(max_length=255, blank=True, default='')
    # The sender's own id: Stripe's `evt_...`, the Retell `call_id`, GHL's `webhookId`.
    event_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=RECEIVED)
    signature_valid = models.BooleanField(default=False)
    payload = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    # Why processing failed, or why the delivery was rejected. Traceback included, redacted.
    error = models.TextField(blank=True, default='')
    request_id = models.CharField(max_length=32, blank=True, default='')
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'audit_webhook_event'
        ordering = ['-id']

    def __str__(self):
        return f'WebhookEvent({self.pk}, {self.source} {self.event_type}, {self.status})'


class AuditEvent(models.Model):
    """One thing that happened to money, billing or a client's state, and who did it.

    Append-only: a row cannot be saved over or deleted through the model. Written for every
    change to a bill, bill line, rate or client mapping (with the fields that changed, before and
    after) and for each outbound action - an invoice raised, a card attempted, a pause written
    to GoHighLevel, a card requested.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # `user:<GHL user id>`, `webhook:<source>`, `celery:<task>`, `cli:<os user>` or `system`.
    actor = models.CharField(max_length=255, blank=True, default='')
    # The GHL user's name and email for a `user:` actor; the command name for a `cli:` one.
    actor_label = models.CharField(max_length=255, blank=True, default='')
    action = models.CharField(max_length=100, db_index=True)
    model = models.CharField(max_length=100, blank=True, default='')
    object_id = models.CharField(max_length=64, blank=True, default='')
    location_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    billing_period = models.CharField(max_length=7, blank=True, default='')
    # Only the fields that changed. `before` is null on creation, `after` null on deletion.
    before = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    detail = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    # The delivery that caused this, when a webhook did.
    webhook_event = models.ForeignKey(
        WebhookEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name='audit_events'
    )
    request_id = models.CharField(max_length=32, blank=True, default='')

    class Meta:
        db_table = 'audit_event'
        ordering = ['-id']

    def __str__(self):
        return f'AuditEvent({self.pk}, {self.action}, {self.location_id})'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError('Audit events are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError('Audit events are append-only.')


class TaskRun(models.Model):
    """One execution of a Celery task, scheduled by beat or not."""

    STARTED = 'started'
    SUCCESS = 'success'
    FAILURE = 'failure'
    RETRY = 'retry'
    STATUSES = [(STARTED, 'Started'), (SUCCESS, 'Success'), (FAILURE, 'Failure'), (RETRY, 'Retry')]

    task_id = models.CharField(max_length=64, db_index=True)
    task_name = models.CharField(max_length=255, db_index=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=STARTED)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    args = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    kwargs = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    # What the task returned: the counts each of ours reports (`failed: 2`).
    result = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    error = models.TextField(blank=True, default='')
    worker = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'audit_task_run'
        ordering = ['-id']

    def __str__(self):
        return f'TaskRun({self.task_name}, {self.status})'


class ErrorLog(models.Model):
    """A warning or error, with the structured context the log call carried."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10)
    logger = models.CharField(max_length=255)
    message = models.TextField()
    exception_type = models.CharField(max_length=255, blank=True, default='')
    traceback = models.TextField(blank=True, default='')
    # Ids and amounts passed as `extra={'context': {...}}`: location, bill, invoice, period.
    context = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    request_id = models.CharField(max_length=32, blank=True, default='')
    actor = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'audit_error_log'
        ordering = ['-id']

    def __str__(self):
        return f'ErrorLog({self.pk}, {self.level}, {self.logger})'
