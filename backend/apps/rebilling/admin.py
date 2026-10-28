from django.contrib import admin

from .models import ClientMapping


@admin.register(ClientMapping)
class ClientMappingAdmin(admin.ModelAdmin):
    """A fallback for the one thing the admin page's mapping endpoint does: set up a client.

    Everything a client is billed from - rates, calls, Twilio days - stays read-only here too;
    this is the mapping model, not a way to hand-edit billing history.
    """

    list_display = (
        'client_name',
        'location_id',
        'active',
        'billing_mode',
        'twilio_subaccount_sid',
    )
    list_filter = ('active', 'billing_mode')
    search_fields = ('client_name', 'location_id')
