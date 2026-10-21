from rest_framework import serializers

from .models import GHLOAuthToken


class GHLOAuthTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = GHLOAuthToken
        fields = ['company_id', 'location_id', 'name', 'is_active', 'expires_at']
