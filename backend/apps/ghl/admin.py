from django.contrib import admin

from .models import GHLOAuthToken


@admin.register(GHLOAuthToken)
class GHLOAuthTokenAdmin(admin.ModelAdmin):
    list_display = ('name', 'company_id', 'location_id', 'is_agency', 'is_active', 'expires_at')
    list_filter = ('is_agency', 'is_active')
    exclude = ('access_token', 'refresh_token')
