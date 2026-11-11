from django.contrib import admin

from .models import AuditEvent, ErrorLog, TaskRun, WebhookEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    """These tables are records, not settings: look, filter, search, never edit."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = ('id', 'received_at', 'source', 'event_type', 'event_id', 'status')
    list_filter = ('source', 'status', 'signature_valid')
    search_fields = ('event_id', 'event_type', 'request_id')


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ('id', 'created_at', 'action', 'location_id', 'billing_period', 'actor')
    list_filter = ('action', 'model')
    search_fields = ('location_id', 'actor', 'actor_label', 'object_id', 'request_id')


@admin.register(TaskRun)
class TaskRunAdmin(ReadOnlyAdmin):
    list_display = ('id', 'started_at', 'task_name', 'status', 'finished_at')
    list_filter = ('task_name', 'status')
    search_fields = ('task_id',)


@admin.register(ErrorLog)
class ErrorLogAdmin(ReadOnlyAdmin):
    list_display = ('id', 'created_at', 'level', 'logger', 'message')
    list_filter = ('level', 'logger')
    search_fields = ('message', 'request_id', 'actor')
