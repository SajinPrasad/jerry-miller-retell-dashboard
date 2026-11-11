"""Receivers that write the audit trail and the Celery task record without call-site help.

Model changes are caught at `pre_save` / `post_save` rather than at each call site, so a state
change added later is audited too and one cannot be forgotten. `pre_save` reads the stored row
(one indexed query) for the *before* state: a snapshot taken when the object was loaded would be
wrong after `refresh_from_db()`, which the charge code uses.
"""

import json
import logging
import traceback as traceback_module

from celery.signals import task_failure, task_postrun, task_prerun
from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models.signals import post_save, pre_save
from django.utils import timezone

from . import context
from .models import TaskRun
from .recording import record
from .redaction import redact, scrub

logger = logging.getLogger(__name__)

# What is audited. Credentials are not here: the GHL token rows are deliberately absent.
AUDITED = {
    'rebilling.Bill': 'bill',
    'rebilling.BillLine': 'bill_line',
    'rebilling.RateHistory': 'rate',
    'rebilling.ClientMapping': 'client_mapping',
}


def _json(value):
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
    except TypeError:
        return json.loads(json.dumps(value, default=str))


def _fields(model) -> list[str]:
    return [f.attname for f in model._meta.concrete_fields if not f.primary_key]


def _scope(instance) -> tuple[str, str]:
    """The client's location id and the billing period an audited row belongs to."""
    label = instance._meta.label
    if label == 'rebilling.ClientMapping':
        return instance.location_id, ''
    if label == 'rebilling.Bill':
        return instance.client.location_id, instance.billing_period
    if label == 'rebilling.BillLine':
        return instance.bill.client.location_id, instance.bill.billing_period
    return instance.client.location_id, ''


def _before_save(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    names = _fields(sender)
    if update_fields is not None:
        names = [n for n in names if n in update_fields or n.removesuffix('_id') in update_fields]
    instance._audit_fields = names
    instance._audit_before = (
        None
        if instance._state.adding or instance.pk is None
        else sender._base_manager.filter(pk=instance.pk).values(*names).first()
    )


def _after_save(sender, instance, created, raw=False, **kwargs):
    if raw or not hasattr(instance, '_audit_fields'):
        return
    label = AUDITED[sender._meta.label]
    names = instance._audit_fields
    after = {n: getattr(instance, n) for n in names}
    location_id, period = _scope(instance)
    common = dict(
        model=sender._meta.label,
        object_id=instance.pk,
        location_id=location_id,
        billing_period=period,
    )

    if created:
        record(f'{label}.created', after=_json(after), **common)
        return

    before = instance._audit_before
    if before is None:
        return
    changed = [n for n in names if before[n] != after[n]]
    if not changed:
        return
    action = f'{label}.updated'
    if label == 'bill' and 'status' in changed:
        action = 'bill.status_changed'
    record(
        action,
        before=_json({n: before[n] for n in changed}),
        after=_json({n: after[n] for n in changed}),
        **common,
    )


def connect_audited_models() -> None:
    for model_label in AUDITED:
        model = apps.get_model(model_label)
        pre_save.connect(_before_save, sender=model, dispatch_uid=f'audit-pre-{model_label}')
        post_save.connect(_after_save, sender=model, dispatch_uid=f'audit-post-{model_label}')


connect_audited_models()


# --- Celery ----------------------------------------------------------------------------------
# Each handler swallows its own errors: recording a run must never fail the task it describes.


def _safe(handler):
    def wrapper(*args, **kwargs):
        try:
            handler(*args, **kwargs)
        except Exception:
            logger.exception('audit: could not record a Celery task event')

    return wrapper


def _open_run(task_id) -> TaskRun | None:
    return TaskRun.objects.filter(task_id=task_id, finished_at__isnull=True).order_by('-pk').first()


@task_prerun.connect(weak=False)
@_safe
def _task_started(task_id=None, task=None, args=None, kwargs=None, **extra):
    context.begin(request_id=task_id or '')
    context.set_actor(f'celery:{task.name}')
    TaskRun.objects.create(
        task_id=task_id,
        task_name=task.name,
        args=scrub(_json(list(args or []))),
        kwargs=scrub(_json(dict(kwargs or {}))),
        worker=getattr(task.request, 'hostname', '') or '',
    )
    logger.info('task started', extra={'context': {'task': task.name, 'task_id': task_id}})


@task_failure.connect(weak=False)
@_safe
def _task_failed(task_id=None, exception=None, traceback=None, **extra):
    run = _open_run(task_id)
    if run is not None:
        run.status = TaskRun.FAILURE
        run.error = redact(
            ''.join(traceback_module.format_exception(type(exception), exception, traceback))
        )
        run.save(update_fields=['status', 'error'])


@task_postrun.connect(weak=False)
@_safe
def _task_finished(task_id=None, task=None, retval=None, state=None, **extra):
    run = _open_run(task_id)
    seconds = None
    if run is not None:
        run.finished_at = timezone.now()
        if run.status != TaskRun.FAILURE:
            run.status = {'SUCCESS': TaskRun.SUCCESS, 'RETRY': TaskRun.RETRY}.get(
                state, TaskRun.FAILURE
            )
        if run.status == TaskRun.SUCCESS:
            run.result = scrub(_json(retval))
        run.save(update_fields=['status', 'finished_at', 'result'])
        seconds = (run.finished_at - run.started_at).total_seconds()
    logger.info(
        'task finished',
        extra={
            'context': {
                'task': task.name,
                'task_id': task_id,
                'state': state,
                'seconds': seconds,
                'result': scrub(_json(retval)) if state == 'SUCCESS' else None,
            }
        },
    )
    context.clear()
