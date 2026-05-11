"""Test ortamı için izole Django ayarları.

.env dosyası OKUNMAZ — load_dotenv() bu dosyada hiç çağrılmaz.
Tüm değerler sabit ve açıkça belirtilmiştir. Gerçek kimlik bilgisi yoktur.

Veritabanı : SQLite :memory: (dış bağlantı açılmaz)
Cache       : django.core.cache.backends.locmem.LocMemCache (Redis gerekmez)
E-posta     : django.core.mail.backends.console.EmailBackend (gerçek posta gönderilmez)
Dış servisler: Sahte/dummy değerler (WhatsApp, Pavo, ESUREC, NETGSM, RapidAPI)

Kural: Bu dosyaya hiçbir zaman gerçek kimlik bilgisi ekleme.
       Üretim ayarları için settings.py kullan.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Temel Güvenlik ───────────────────────────────────────────────────────────
SECRET_KEY = "test-secret-key-do-not-use-in-production-kuyumplus-1234"
DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = []

# ─── Kullanıcı Modeli ─────────────────────────────────────────────────────────
AUTH_USER_MODEL = "accounts.Users"

# ─── Uygulamalar ──────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.dashboard",
    "apps.accounts",
    "apps.customers",
    "apps.roles",
    "apps.activity_logs",
    "apps.suppliers",
    "apps.products",
    "apps.gold_purchases",
    "apps.scraps",
    "apps.inventories",
    "apps.definitions.categories",
    "apps.transactions_board",
    "apps.definitions.locations",
    "apps.masak",
    "apps.definitions.sms_profiles",
    "apps.definitions.email_profiles",
    "apps.definitions.brands",
    "apps.stores",
    "apps.definitions.currencies",
    "apps.definitions.contracts",
    "apps.definitions.rates",
    "apps.crm.packages",
    "apps.process",
    "apps.repairs",
    "apps.workshops",
    "apps.counts",
    "apps.custody",
    "apps.contact_forms",
    "apps.whatsapp",
    "apps.settings.apps.SettingsConfig",
    "apps.bracelets",
    "django_celery_beat",
    "apps.crm.leads",
    "apps.invoices",
    "rest_framework",
    "rest_framework.authtoken",
    "apps.testimonials",
    "apps.orders",
    "apps.pavo",
    "apps.crm.proposals",
    "apps.crm.devices",
    "apps.backups",
    "apps.live_board",
    "apps.supports",
    "apps.chambers",
    "apps.banking",
    "apps.stock_management.apps.StockManagementConfig",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# ─── Middleware ───────────────────────────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "juwelier_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.roles.context_processors.get_user_permissions",
                "apps.pavo.context_processors.pavo_terminal",
                "apps.supports.context_processors.support_notifications",
            ]
        },
    }
]

WSGI_APPLICATION = "juwelier_project.wsgi.application"

SESSION_COOKIE_AGE = 86400
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = True
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

# ─── Veritabanı — SQLite :memory: (dış sunucu bağlantısı yok) ────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# ─── Cache — LocMem (Redis gerektirmez) ───────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# ─── Şifre Doğrulayıcıları ───────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─────────────────────────────────────────────────────────────────────────────
# Test ortamı da Almanya/Berlin saat dilimine sabitlendi → CI'da timezone
# bağımlı bug'lar (DST, naive/aware karışımı, format) production ile aynı
# koşullarda yakalanır. USE_TZ=True production ile simetrik.
# ─────────────────────────────────────────────────────────────────────────────
LANGUAGE_CODE = "de"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_L10N = True
USE_TZ = True

# Production ile aynı format ayarları → şablon testleri tutarlı sonuç verir.
DATE_FORMAT = 'd.m.Y'
DATETIME_FORMAT = 'd.m.Y H:i'
TIME_FORMAT = 'H:i'
SHORT_DATE_FORMAT = 'd.m.Y'
SHORT_DATETIME_FORMAT = 'd.m.Y H:i'
FIRST_DAY_OF_WEEK = 1

CELERY_TIMEZONE = 'Europe/Berlin'
CELERY_ENABLE_UTC = True

STATIC_URL = "/static/"
STATICFILES_DIRS = (str(BASE_DIR / "static"),)

AUTO_IMAGE_PROCESSING_ENABLED = False
IMAGE_MAX_KB = 150
IMAGE_PREFER_WEBP = True
IMAGE_KEEP_EXIF = False

# ─── E-posta — console backend (gerçek posta gönderilmez) ────────────────────
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
EMAIL_FROM_ADDRESS = "test@test.local"
EMAIL_HOST = "localhost"
EMAIL_PORT = 25
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
EMAIL_USE_TLS = False
EMAIL_USE_SSL = False

SUPPORT_EMAILS = []

MEDIA_URL = "/media/"
MEDIA_ROOT = str(BASE_DIR / "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Celery Beat — testlerde görev zamanlama kapalı ──────────────────────────
CELERY_BEAT_SCHEDULE = {}

# ─── Uygulama Adresi ─────────────────────────────────────────────────────────
APP_DOMAIN = "http://testserver"

# ─── Meta / WhatsApp — sahte değerler ────────────────────────────────────────
META_VERIFY_TOKEN = "test-dummy-meta-verify-token"
META_PHONE_NUMBER_ID = "test-dummy-phone-id"
META_ACCESS_TOKEN = "test-dummy-access-token"
META_WABA_ID = "test-dummy-waba-id"
META_GRAPH_VERSION = "v17.0"
META_DEFAULT_TEST_TO = "+900000000000"
META_DEFAULT_TEMPLATE_NAME = "hello_world"
META_DEFAULT_TEMPLATE_LANG = "tr_TR"

# ─── Pavo Ödeme — sahte değerler (gerçek ödeme çağrısı yapılmaz) ─────────────
PAVO_BASE_URL = "https://test-dummy.example.com"
PAVO_MERCHANT_ID = "test-dummy-merchant"
PAVO_API_KEY = "test-dummy-pavo-api-key"
PAVO_API_SECRET = "test-dummy-pavo-api-secret"
PAVO_WEBHOOK_SECRET = "test-dummy-webhook-secret"
PAVO_SUCCESS_URL = "http://testserver/payment/success"
PAVO_FAIL_URL = "http://testserver/payment/fail"
PAVO_USE_DEMO = True
PAVO_BASE_URL_DEMO = "https://test-dummy-demo.example.com"
PAVO_APP_TOKEN = "test-dummy-app-token"
PAVO_APPLICATION_NAME = "Test Kuyum Plus"
PAVO_SOURCE_FINGERPRINT = "TEST-FP"
PAVO_DISPLAY_LAYOUT = "Test"
PAVO_VERIFY_SSL = False

# ─── NetGSM SMS — sahte değerler ─────────────────────────────────────────────
NETGSM_USERCODE = "test-dummy-netgsm-user"
NETGSM_PASSWORD = "test-dummy-netgsm-pass"
NETGSM_HEADER = "TEST"

# ─── RapidAPI — sahte değerler ───────────────────────────────────────────────
RAPIDAPI_KEY = "test-dummy-rapidapi-key"
RAPIDAPI_HOST = "test-dummy.rapidapi.com"

# ─── e-Süreç — sahte değerler ────────────────────────────────────────────────
ESUREC_BASE_URL = ""
ESUREC_API_KEY = "test-dummy-esurec-key"
ESUREC_API_SECRET = "test-dummy-esurec-secret"
ESUREC_TIMEOUT = 5
# Geçerli formatlı Fernet anahtarı (32 null byte) — üretim anahtarı DEĞİL
ESUREC_CREDENTIAL_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
