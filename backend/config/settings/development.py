from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])  # noqa: F405
CORS_ALLOWED_ORIGINS = ['http://localhost:5173']
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
