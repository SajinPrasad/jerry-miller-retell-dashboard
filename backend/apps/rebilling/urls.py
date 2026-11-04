from django.urls import path

from . import views

app_name = 'rebilling'
urlpatterns = [
    # One client's own Retell workspace. The id in the path is what picks the key the
    # signature is checked with, so it has to be in the URL and cannot be in the body.
    path(
        'webhooks/retell/<str:location_id>/',
        views.RetellClientWebhookView.as_view(),
        name='retell-client-webhook',
    ),
    path('webhooks/stripe/', views.StripeWebhookView.as_view(), name='stripe-webhook'),
    path('webhooks/ghl-card/', views.GHLCardWebhookView.as_view(), name='ghl-card-webhook'),
    path('admin/clients/', views.AdminClientsView.as_view(), name='admin-clients'),
    path('admin/clients/sync/', views.AdminClientsSyncView.as_view(), name='admin-clients-sync'),
    path(
        'admin/twilio-subaccounts/',
        views.AdminTwilioSubaccountsView.as_view(),
        name='admin-twilio-subaccounts',
    ),
    path(
        'admin/clients/<str:location_id>/',
        views.AdminClientDetailView.as_view(),
        name='admin-client-detail',
    ),
    path(
        'admin/clients/<str:location_id>/rates/',
        views.AdminClientRatesView.as_view(),
        name='admin-client-rates',
    ),
    path(
        'admin/clients/<str:location_id>/card-request/',
        views.AdminClientCardRequestView.as_view(),
        name='admin-client-card-request',
    ),
    path(
        'admin/clients/<str:location_id>/retell-key/',
        views.AdminClientRetellKeyView.as_view(),
        name='admin-client-retell-key',
    ),
    path(
        'admin/clients/<str:location_id>/mapping/',
        views.AdminClientMappingView.as_view(),
        name='admin-client-mapping',
    ),
]
