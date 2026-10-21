from django.urls import path

from . import views

app_name = 'ghl'
urlpatterns = [
    path('onboarding/', views.OnboardingView.as_view(), name='onboarding'),
    path('onboarding/sync/', views.LocationSyncView.as_view(), name='onboarding-sync'),
    path('oauth/callback/', views.OAuthCallbackView.as_view(), name='oauth-callback'),
]
