from django.urls import path

from . import views

app_name = 'hcp'
urlpatterns = [
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
]
