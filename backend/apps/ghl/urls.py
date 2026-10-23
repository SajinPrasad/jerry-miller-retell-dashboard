from django.urls import path

from . import views

app_name = 'ghl'
urlpatterns = [
    path('onboarding/', views.OnboardingView.as_view(), name='onboarding'),
    path('onboarding/sync/', views.LocationSyncView.as_view(), name='onboarding-sync'),
    path('sso/session/', views.SSOSessionView.as_view(), name='sso-session'),
]
