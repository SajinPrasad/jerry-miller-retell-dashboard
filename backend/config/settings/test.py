from .base import *  # noqa: F403
from .base import MIDDLEWARE

SECRET_KEY = 'test-secret-key'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
MIDDLEWARE = [m for m in MIDDLEWARE if 'whitenoise' not in m]

FIELD_ENCRYPTION_KEY = 'EtcfOijPUSsIbUZiNn5_-4X7P4Jf2lwWGAbpGZR15VU='
GHL_CLIENT_ID = 'app123-abc'
GHL_CLIENT_SECRET = 'secret'
GHL_APP_ID = 'app123'
GHL_APP_SSO_KEY = 'test-sso-secret'
AGENCY_LOCATION_ID = 'agency-loc'
GHL_REDIRECT_URI = 'http://testserver/api/oauth/callback/'
GHL_INSTALL_URL_BASE = 'https://marketplace.example.com'
FRONTEND_URL = 'http://frontend.test'
RETELL_API_KEY = 'retell-test-key'
TWILIO_ACCOUNT_SID = 'ACparent'
TWILIO_AUTH_TOKEN = 'twilio-test-token'
STRIPE_SECRET_KEY = 'sk_test_key'
STRIPE_WEBHOOK_SECRET = 'whsec_test_secret'
# Pinned rather than read from the environment: the tests assert the paused behaviour by hand,
# and the ones that cover the Twilio calculation turn it back on with `override_settings`. A
# developer's own `.env` must not decide which of the two the suite runs.
TWILIO_BILLING_ENABLED = False
# Pinned for the same reason: the status and card tests assert what is written to the contact
# in GoHighLevel and what the webhook accepts, neither of which may depend on a developer's
# own `.env`.
GHL_RETELL_STATUS_CUSTOM_FIELD_ID = 'cf_retell_status'
GHL_CARD_WEBHOOK_TOKEN = 'card-webhook-token'
GHL_RETELL_AGENT_CUSTOM_VALUE_NAME = 'retell_agent_id'
