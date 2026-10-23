from django.urls import path

from . import views

app_name = 'rebilling'
urlpatterns = [
    path('webhooks/retell/', views.RetellWebhookView.as_view(), name='retell-webhook'),
    path('admin/clients/', views.AdminClientsView.as_view(), name='admin-clients'),
    path(
        'admin/clients/<str:location_id>/',
        views.AdminClientDetailView.as_view(),
        name='admin-client-detail',
    ),
]
