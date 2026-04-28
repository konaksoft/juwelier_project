import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_str(name, default=""):
    return os.getenv(name, default)


def env_bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = env_str("SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY .env içinde tanımlı olmalı!")

META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_WABA_ID = os.getenv("META_WABA_ID", "")
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "")

META_DEFAULT_TEST_TO = os.getenv("META_DEFAULT_TEST_TO", "+905511196049")  # + ile E.164
META_DEFAULT_TEMPLATE_NAME = os.getenv("META_DEFAULT_TEMPLATE_NAME", "hello_world")
META_DEFAULT_TEMPLATE_LANG = os.getenv("META_DEFAULT_TEMPLATE_LANG", "tr_TR")  # örn: en_US ya da tr_TR
ALLOWED_HOSTS = [h.strip() for h in env_str("ALLOWED_HOSTS", "").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env_str("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

AUTH_USER_MODEL = "accounts.Users"

INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'apps.dashboard', 'apps.accounts', 'apps.customers', 'apps.roles', 'apps.activity_logs',
    'apps.suppliers', 'apps.products', 'apps.gold_purchases', 'apps.scraps',
    'apps.inventories', 'apps.definitions.categories', 'apps.transactions_board',
    'apps.definitions.locations', 'apps.masak',
    'apps.definitions.sms_profiles', 'apps.definitions.email_profiles', 'apps.definitions.brands',
    'apps.stores', 'apps.definitions.currencies', 'apps.definitions.contracts', 'apps.definitions.rates',
    'apps.crm.packages',
    'apps.process', 'apps.repairs', 'apps.workshops', 'apps.counts', 'apps.custody',
    'apps.contact_forms', 'apps.whatsapp', "apps.settings.apps.SettingsConfig", "apps.bracelets", "django_celery_beat",
    'apps.crm.leads', 'apps.invoices', 'rest_framework',
    'rest_framework.authtoken', 'apps.testimonials',
    'apps.orders', 'apps.pavo', 'apps.crm.proposals', 'apps.crm.devices', 'apps.backups', 'apps.live_board',
    'apps.supports', 'apps.chambers',
    'apps.banking',
    'apps.stock_management.apps.StockManagementConfig',
]

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

PAVO_BASE_URL = os.getenv('PAVO_BASE_URL', 'https://overpayws.overtech.com.tr')
PAVO_MERCHANT_ID = os.getenv('PAVO_MERCHANT_ID', '')
PAVO_API_KEY = os.getenv('PAVO_API_KEY', '')
PAVO_API_SECRET = os.getenv('PAVO_API_SECRET', '')
PAVO_WEBHOOK_SECRET = os.getenv('PAVO_WEBHOOK_SECRET', '')
PAVO_SUCCESS_URL = os.getenv('PAVO_SUCCESS_URL', 'https://example.com/payment/success')
PAVO_FAIL_URL = os.getenv('PAVO_FAIL_URL', 'https://example.com/payment/fail')

PAVO_USE_DEMO = str(os.getenv('PAVO_USE_DEMO', 'false')).strip().lower() in ('1', 'true', 'yes', 'on')
PAVO_BASE_URL_DEMO = os.getenv('PAVO_BASE_URL_DEMO', 'https://overpaywsdemo.overtech.com.tr')
PAVO_APP_TOKEN = os.getenv('PAVO_APP_TOKEN', '')
PAVO_APPLICATION_NAME = os.getenv('PAVO_APPLICATION_NAME', 'Kuyum Plus')
PAVO_SOURCE_FINGERPRINT = os.getenv('PAVO_SOURCE_FINGERPRINT', 'KP-FP')
PAVO_DISPLAY_LAYOUT = os.getenv('PAVO_DISPLAY_LAYOUT', 'KuyumPlus')
PAVO_VERIFY_SSL = str(os.getenv('PAVO_VERIFY_SSL', 'true')).strip().lower() in ('1', 'true', 'yes', 'on')

NETGSM_USERCODE = env_str('NETGSM_USERCODE', '')
NETGSM_PASSWORD = env_str('NETGSM_PASSWORD', '')
NETGSM_HEADER = env_str('NETGSM_HEADER', '')

# settings.py dosyasının uygun bir yerine ekleyin (örneğin NETGSM tanımlarının altına)

RAPIDAPI_KEY = env_str('RAPIDAPI_KEY', '')
RAPIDAPI_HOST = env_str('RAPIDAPI_HOST', 'harem-altin-anlik-altin-fiyatlari-live-rates-gold.p.rapidapi.com')

# Eğer .env dosyasında tanımlı değilse varsayılan olarak local adresi alır.
APP_DOMAIN = env_str('APP_DOMAIN', 'http://127.0.0.1:8000')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'juwelier_project.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug', 'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages',
        'apps.roles.context_processors.get_user_permissions',
        "apps.pavo.context_processors.pavo_terminal", "apps.supports.context_processors.support_notifications"
        # BURAYI EKLE

    ]},
}]

WSGI_APPLICATION = 'juwelier_project.wsgi.application'

SESSION_COOKIE_AGE = 86400
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = True
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

CELERY_BEAT_SCHEDULE = {
    'check_esurec_statuses': {
        'task': 'invoices.check_esurec_statuses',
        'schedule': timedelta(minutes=5),
    },
    'update_products_from_api': {
        'task': 'products.update_products_from_api',
        'schedule': timedelta(seconds=15),
    },
}

# --- e-Süreç Entegrasyon Ayarları ---
ESUREC_BASE_URL = env_str('ESUREC_BASE_URL', '')
ESUREC_API_KEY = env_str('ESUREC_API_KEY', '')
ESUREC_API_SECRET = env_str('ESUREC_API_SECRET', '')
ESUREC_TIMEOUT = env_int('ESUREC_TIMEOUT', 30)

# Fernet simetrik şifreleme anahtarı — EsurecTenantCredential.tenant_token_enc için
# Üretim:
#   from cryptography.fernet import Fernet
#   print(Fernet.generate_key().decode())  # → .env'e ESUREC_CREDENTIAL_KEY=<değer> olarak ekle
# UYARI: Bu anahtar kaybolursa mevcut şifreli tokenlar çözülemez. Güvenli sakla.
ESUREC_CREDENTIAL_KEY = env_str('ESUREC_CREDENTIAL_KEY', '')

# --- Database ---
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': env_str('DB_NAME', ''),
        'USER': env_str('DB_USER', ''),
        'PASSWORD': env_str('DB_PASSWORD', ''),
        'HOST': env_str('DB_HOST', ''),
        'PORT': env_str('DB_PORT', '5432'),
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': True,
    }
}

# --- Cache (Redis) ---
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",  # 0'ı Celery kullanıyor, 1'i Cache için ayırdık
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        }
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'tr'
TIME_ZONE = 'Europe/Istanbul'
USE_I18N = True
USE_L10N = True
USE_TZ = False

STATIC_URL = '/static/'
STATICFILES_DIRS = (os.path.join(BASE_DIR, 'static/'),)

AUTO_IMAGE_PROCESSING_ENABLED = True
AUTO_IMAGE_PROCESSING_SKIP_MODELS = [
    # "someapp.bigblobmodel",
]
IMAGE_MAX_KB = 150
IMAGE_PREFER_WEBP = True
IMAGE_KEEP_EXIF = False

# --- Email ---
EMAIL_FROM_ADDRESS = env_str('EMAIL_FROM_ADDRESS', 'Kuyumplus.com <destek@kuyumplus.com>')
EMAIL_HOST = env_str('EMAIL_HOST', 'mail.kurumsaleposta.com')
EMAIL_PORT = env_int('EMAIL_PORT', 587)
EMAIL_HOST_USER = env_str('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = env_str('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)

SUPPORT_EMAILS = ['konakyunus@hotmail.com']

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
