from django.urls import path

from . import views, webhooks

app_name = 'ghl'
urlpatterns = [
    path('onboarding/', views.OnboardingView.as_view(), name='onboarding'),
    path('onboarding/sync/', views.LocationSyncView.as_view(), name='onboarding-sync'),
    path('sso/session/', views.SSOSessionView.as_view(), name='sso-session'),
    path('saas-client-count/', views.SaasClientCountView.as_view(), name='saas-client-count'),
    # The one GHL marketplace webhook URL. The two older URLs stay wired to the same view, so any
    # registration made against them keeps working.
    path('webhooks/', webhooks.GHLWebhookView.as_view(), name='webhooks'),
    path(
        'webhooks/location-created/',
        webhooks.GHLWebhookView.as_view(),
        name='location-created-webhook',
    ),
]
