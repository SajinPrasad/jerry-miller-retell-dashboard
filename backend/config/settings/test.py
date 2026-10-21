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
AGENCY_LOCATION_ID = 'agency-loc'
GHL_REDIRECT_URI = 'http://testserver/api/oauth/callback/'
GHL_INSTALL_URL_BASE = 'https://marketplace.example.com'
FRONTEND_URL = 'http://frontend.test'
