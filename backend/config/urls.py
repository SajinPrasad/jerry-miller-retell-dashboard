from django.contrib import admin
from django.urls import include, path

from apps.ghl.views import OAuthCallbackView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/oauth/callback/', OAuthCallbackView.as_view(), name='oauth-callback'),
    path('api/ghl/', include('apps.ghl.urls')),
    path('api/dashboards/', include('apps.dashboards.urls')),
    path('api/rebilling/', include('apps.rebilling.urls')),
]
