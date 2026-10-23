from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('DJANGO_SECRET_KEY')
DEBUG = False
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=[])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'django_filters',
    # Local apps
    'apps.common',
    'apps.ghl',
    'apps.dashboards',
    'apps.rebilling',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

DATABASES = {
    'default': env.db('DATABASE_URL', default=f'sqlite:///{BASE_DIR / "db.sqlite3"}'),
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend'],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

CORS_ALLOWED_ORIGINS = env.list('CORS_ALLOWED_ORIGINS', default=[])

# GoHighLevel marketplace app (Task 1: onboarding)
GHL_CLIENT_ID = env('GHL_CLIENT_ID', default='')
GHL_CLIENT_SECRET = env('GHL_CLIENT_SECRET', default='')
GHL_APP_ID = env('GHL_APP_ID', default='')
# Jerry's agency location id; identifies the agency row and is skipped when syncing clients.
AGENCY_LOCATION_ID = env('AGENCY_LOCATION_ID', default='')
GHL_API_BASE_URL = env('GHL_API_BASE_URL', default='https://services.leadconnectorhq.com')
GHL_API_VERSION = env('GHL_API_VERSION', default='2021-07-28')
GHL_REQUEST_TIMEOUT = env.int('GHL_REQUEST_TIMEOUT', default=30)
# Host of the app's Installation URL (Advanced Settings > Auth); white-label hosts differ.
GHL_INSTALL_URL_BASE = env('GHL_INSTALL_URL_BASE', default='https://marketplace.gohighlevel.com')
GHL_REDIRECT_URI = env('GHL_REDIRECT_URI', default='http://localhost:8000/api/oauth/callback/')
# Must match the scopes configured on the marketplace app.
GHL_SCOPES = env(
    'GHL_SCOPES',
    default=' '.join(
        [
            'oauth.readonly',
            'oauth.write',
            'locations.readonly',
            'contacts.readonly',
            'opportunities.readonly',
            'calendars.readonly',
            'calendars/events.readonly',
            'conversations.readonly',
            'conversations/message.readonly',
            'saas/company.read',
            'saas/location.read',
        ]
    ),
)
# Fernet key that encrypts OAuth tokens at rest (see .env.example to generate one).
FIELD_ENCRYPTION_KEY = env('FIELD_ENCRYPTION_KEY', default='')

# Where the OAuth callback sends the browser afterwards (the React app).
FRONTEND_URL = env('FRONTEND_URL', default='http://localhost:5173')

# Retell voice rebilling (Task 4). The same key verifies webhook signatures, so it must be the
# API key carrying the webhook badge in the Retell dashboard.
RETELL_API_KEY = env('RETELL_API_KEY', default='')
# How a call's minutes are billed: 'per_second' or 'round_up_minute' (round each call up).
RETELL_ROUNDING = env('RETELL_ROUNDING', default='per_second')
RETELL_API_BASE_URL = env('RETELL_API_BASE_URL', default='https://api.retellai.com')
RETELL_REQUEST_TIMEOUT = env.int('RETELL_REQUEST_TIMEOUT', default=30)

# Background jobs
CELERY_BROKER_URL = env('REDIS_URL', default='redis://localhost:6379/0')

# Scheduled jobs. Times are UTC (TIME_ZONE above); 03:00 is off-peak for client calls, and the
# audit's 48-hour window means a missed run is covered by the next one.
CELERY_BEAT_SCHEDULE = {
    'reconcile-retell-calls': {
        'task': 'apps.rebilling.tasks.reconcile_retell_calls',
        'schedule': crontab(hour=3, minute=0),
    },
}
