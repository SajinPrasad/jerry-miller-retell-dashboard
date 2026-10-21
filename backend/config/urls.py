from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/ghl/', include('apps.ghl.urls')),
    path('api/dashboards/', include('apps.dashboards.urls')),
    path('api/rebilling/', include('apps.rebilling.urls')),
]
