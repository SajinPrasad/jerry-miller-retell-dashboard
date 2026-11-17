from django.urls import path

from . import ghl_webhooks, views, webhooks

app_name = 'hcp'
urlpatterns = [
    path('webhooks/', webhooks.HCPWebhookView.as_view(), name='webhook'),
    path(
        'webhooks/ghl/',
        ghl_webhooks.GHLReverseSyncWebhookView.as_view(),
        name='ghl-webhook',
    ),
    path('accounts/', views.ConnectAccountView.as_view(), name='connect-account'),
    path(
        'accounts/<str:location_id>/status/',
        views.StatusView.as_view(),
        name='status',
    ),
    path(
        'accounts/<str:location_id>/employees/',
        views.EmployeesView.as_view(),
        name='employees',
    ),
    path(
        'accounts/<str:location_id>/employee-mappings/',
        views.EmployeeMappingsView.as_view(),
        name='employee-mappings',
    ),
    path(
        'accounts/<str:location_id>/calendars/',
        views.CalendarsView.as_view(),
        name='calendars',
    ),
    path(
        'accounts/<str:location_id>/calendar-settings/',
        views.CalendarSettingsView.as_view(),
        name='calendar-settings',
    ),
    path(
        'accounts/<str:location_id>/activate/',
        views.ActivateView.as_view(),
        name='activate',
    ),
]
