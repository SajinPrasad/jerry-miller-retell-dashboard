from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.common.encryption import EncryptedTextField

# Refresh a token once it is within this many seconds of expiring.
REFRESH_BUFFER_SECONDS = 300


class GHLOAuthToken(models.Model):
    """OAuth credentials for Jerry's agency (one row, `is_agency=True`) or one client sub-account.

    The agency token comes from the marketplace install and is refreshed with `refresh_token`.
    A sub-account token is minted from the agency token, so it has no refresh token of its own.
    """

    company_id = models.CharField(max_length=255)
    location_id = models.CharField(max_length=255, blank=True, default='')
    name = models.CharField(max_length=255, blank=True, default='')
    is_agency = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    access_token = EncryptedTextField()
    refresh_token = EncryptedTextField(blank=True, default='')
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'ghl_oauth_token'
        ordering = ['name']

    def __str__(self):
        return f'GHLOAuthToken({self.location_id or self.company_id}, {self.name!r})'

    @property
    def needs_refresh(self) -> bool:
        return self.expires_at <= timezone.now() + timedelta(seconds=REFRESH_BUFFER_SECONDS)
