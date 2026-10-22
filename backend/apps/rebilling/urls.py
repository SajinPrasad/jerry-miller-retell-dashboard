from django.urls import path

from . import views

app_name = 'rebilling'
urlpatterns = [
    path('webhooks/retell/', views.RetellWebhookView.as_view(), name='retell-webhook'),
]
