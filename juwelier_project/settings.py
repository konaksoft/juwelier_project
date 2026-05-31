import os
from pathlib import Path
from celery.schedules import crontab  # FAZ E — yedekleme zamanlaması için
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
    'apps.definitions.locations',
    'apps.definitions.sms_profiles', 'apps.definitions.email_profiles', 'apps.definitions.brands',
    'apps.stores', 'apps.definitions.currencies', 'apps.definitions.contracts', 'apps.definitions.rates',
    'apps.crm.packages',
    'apps.process', 'apps.repairs', 'apps.workshops', 'apps.counts', 'apps.custody',
    'apps.contact_forms', 'apps.whatsapp', "apps.settings.apps.SettingsConfig", "apps.bracelets", "django_celery_beat",
    'apps.crm.leads', 'rest_framework',
    'rest_framework.authtoken', 'apps.testimonials',
    'apps.orders', 'apps.crm.proposals', 'apps.crm.devices', 'apps.backups', 'apps.live_board',
    'apps.supports', 'apps.chambers',
    'apps.banking',
    'apps.stock_management.apps.StockManagementConfig',
    # FAZ 45 — Çoklu Şube Transfer Altyapısı (DORMANT, sadece şema)
    'apps.store_transfers.apps.StoreTransfersConfig',
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

# FAZ 30 — Pavo timeout ve durum eşikleri (frontend + backend ortak SSOT)
# Backend HTTP timeout (saniye): cihaza giden /CompleteSale, /JewellerySale gibi
# çağrılar için. 30 sn — kullanıcının kart sokması ve PIN girmesi için yeterli süre.
PAVO_LOCAL_TIMEOUT = int(os.getenv('PAVO_LOCAL_TIMEOUT', '30'))

# Frontend bekleme süresi (saniye): WebView bridge üzerinden POS yanıtı için.
# Hızlı İşlem ve Perakende artık aynı değer; kart okuma + onay süresi 120sn.
PAVO_FRONTEND_TIMEOUT_SECONDS = int(os.getenv('PAVO_FRONTEND_TIMEOUT_SECONDS', '120'))

# Pair (eşleştirme) timeout: cihaz hazırsa 5 sn yeterli; yavaş ağda 15 sn'e kadar
# çıkılabilir. Bu değer sayfa açılışındaki ilk pair için kullanılır.
PAVO_PAIR_TIMEOUT_SECONDS = int(os.getenv('PAVO_PAIR_TIMEOUT_SECONDS', '15'))

# Heartbeat (bağlantı durumu sorgulama) aralığı (saniye). Sayfada en az bir
# işlem yapıldığında periyodik kontrol; idle sayfalarda batarya yormaması
# için yüksek tutuldu.
PAVO_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv('PAVO_HEARTBEAT_INTERVAL_SECONDS', '45'))

# Pavo SaleStatus — başarı sayılan StatusId set'i.
# 4 = DocumentPending (mali fiş hazır, basılmayı bekliyor) — ana başarı kodu.
# 5 = DocumentPrinted, 6 = SaleCompleted, 22 = SaleFinalized — başarı sayılır.
# Diğer kodlar (Cancelled/Suspended/Aborted) başarısız sayılır.
# PAVO_DOCUMENT.TXT:835-870 referansından.
PAVO_SUCCESS_STATUS_IDS = [4, 5, 6, 22]

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
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.settings.middleware.StoreLanguageMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
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
        "apps.supports.context_processors.support_notifications"
        # BURAYI EKLE

    ]},
}]

WSGI_APPLICATION = 'juwelier_project.wsgi.application'

SESSION_COOKIE_AGE = 86400
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_SAVE_EVERY_REQUEST = True
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"

# ─────────────────────────────────────────────────────────────────────────────
# CELERY — Tek Kaynak (Single Source of Truth)
# ─────────────────────────────────────────────────────────────────────────────
# Beat schedule SADECE burada tanımlanır. celery.py içinde override YOKTUR.
# Broker/result backend .env REDIS_URL'den okunur; varsayılan db1 (esurec db0
# ile karışmaması için). Tüm Juwelier task'ları 'juwelier_default' kuyruğunda.
# ─────────────────────────────────────────────────────────────────────────────

# Redis broker/result — esurec (db0) ile ayrı tutmak için Juwelier db1 kullanır.
CELERY_BROKER_URL = env_str('REDIS_URL', 'redis://127.0.0.1:6379/1')
CELERY_RESULT_BACKEND = env_str('REDIS_URL', 'redis://127.0.0.1:6379/1')

# Kuyruk ayrımı — worker '-Q juwelier_default' ile çalıştırılmalı.
CELERY_TASK_DEFAULT_QUEUE = 'juwelier_default'

CELERY_BEAT_SCHEDULE = {
    # ==== Kitco Uluslararası Spot Fiyat Fetch ====
    # KitcoPriceCache tablosuna YAZAR; Rates tablosunu OKUR. Başka tabloya dokunmaz.
    'live-board-fetch-kitco-every-60s': {
        'task': 'live_board.fetch_kitco_live_rates',
        'schedule': 60,  # Her 60 saniyede bir
        'options': {'queue': 'juwelier_default'},
    },

    # ==== ECB FX Kur Senkronizasyonu ====
    # Avrupa Merkez Bankası günlük XML feed'inden USD bazlı kurları Rates'e yazar.
    # Kitco task'ı EUR/GBP/CHF/CAD/AUD/JPY türetimi için bu kayıtları okur.
    'rates-sync-fx-from-ecb-hourly': {
        'task': 'definitions.rates.sync_fx_rates_from_ecb',
        'schedule': crontab(minute=10),  # Her saatin 10'unda
        'options': {'queue': 'juwelier_default'},
    },

    # ==== FAZ E — Yedekleme Otomasyonu ====
    # Karar 2 (Onaylı): Otomatik yedeklerde include_media=False (DB-only).
    # Haftada 1 kez full (media dahil).
    'backups_cleanup_old': {
        'task': 'backups.cleanup_old_backups',
        # Her gün 02:00 — yedek alma öncesi temizlik
        'schedule': crontab(hour=2, minute=0),
        'options': {'queue': 'juwelier_default'},
    },
    'backups_daily_db': {
        'task': 'backups.daily_db_backup_all_companies',
        # Her gün 03:00 — DB-only ZIP
        'schedule': crontab(hour=3, minute=0),
        'options': {'queue': 'juwelier_default'},
    },
    'backups_weekly_full': {
        'task': 'backups.weekly_full_backup_all_companies',
        # Her Pazar 04:00 — DB + media full ZIP
        'schedule': crontab(hour=4, minute=0, day_of_week=0),
        'options': {'queue': 'juwelier_default'},
    },
}

# FAZ E — Yedekleme Retention Politikası (.env override edilebilir)
BACKUP_DAILY_RETENTION_DAYS = env_int('BACKUP_DAILY_RETENTION_DAYS', 7)
BACKUP_WEEKLY_RETENTION_DAYS = env_int('BACKUP_WEEKLY_RETENTION_DAYS', 30)
BACKUP_MIN_KEEP = env_int('BACKUP_MIN_KEEP', 3)

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

# ─────────────────────────────────────────────────────────────────────────────
# YEREL SAAT & YERELLEŞTİRME (FAZ 1 — Almanya/Avrupa Pazarı Hazırlığı)
# ─────────────────────────────────────────────────────────────────────────────
# Almanya (DE) merkezli kuyumculuk operasyonu için saat dilimi ve dil
# ayarları juwelier_plus'tan port edilmiştir. TIME_ZONE artık 'Europe/Berlin'
# (UTC+1 / yaz UTC+2). USE_TZ=True ile Django timezone-aware datetime moduna
# geçirildi → tüm yeni kayıtlar UTC olarak DB'ye yazılır, görüntülenirken
# Berlin saatine çevrilir. Bu sayede yaz saati (DST) geçişlerinde veri
# kaybı / kayma yaşanmaz.
#
# DATE_FORMAT / DATETIME_FORMAT / TIME_FORMAT ayarları Alman locale stiline
# (d.m.Y H:i) sabitlenmiştir. Şablonlardaki hardcoded format string'ler ile
# uyumlu. FIRST_DAY_OF_WEEK=1 → AB standartı (Pazartesi).
# ─────────────────────────────────────────────────────────────────────────────
LANGUAGE_CODE = 'de'
LANGUAGES = [
    ('de', 'Deutsch'),
    ('en', 'English'),
    ('tr', 'Türkçe'),
]
TIME_ZONE = 'Europe/Berlin'
USE_I18N = True
USE_L10N = True
USE_TZ = True

# Alman tarih/saat formatı — Django'nun varsayılan locale formatlarını
# override eder. Şablonlardaki {{ x|date:"d.m.Y" }} kullanımları korunur.
DATE_FORMAT = 'd.m.Y'
DATETIME_FORMAT = 'd.m.Y H:i'
TIME_FORMAT = 'H:i'
SHORT_DATE_FORMAT = 'd.m.Y'
SHORT_DATETIME_FORMAT = 'd.m.Y H:i'

# AB standardı: hafta Pazartesi başlar (0=Pazar, 1=Pazartesi)
FIRST_DAY_OF_WEEK = 1

# Celery beat schedule'ları Berlin saatine göre tetiklensin. CELERY_ENABLE_UTC
# True kalır (broker UTC kullanır), beat sadece görüntülenirken Berlin saatine
# çevirir → DST geçişleri otomatik yönetilir.
CELERY_TIMEZONE = 'Europe/Berlin'
CELERY_ENABLE_UTC = True

# LOCALE_PATHS — proje-spesifik Almanca/İngilizce/Türkçe çevirileri.
# Bu dizinler henüz oluşturulmadıysa Django sessizce atlar; ileride
# `django-admin makemessages -l de` ile doldurulacaktır.
LOCALE_PATHS = [
    os.path.join(BASE_DIR, 'locale'),
]

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

# ----------------------------------------------------------------------------
# FAZ 60.2 — Chunked Upload Ayarları
# ----------------------------------------------------------------------------
# Smart Restore büyük paketleri (özellikle ZIP+media) Cloudflare 100 MB body
# limitini aşmadan parça parça yükler. Aşağıdaki ayarlar sunucu tarafı
# güvenlik kapakları (override edilmek istenirse env üzerinden değiştirilir).
#
# BACKUP_CHUNKED_TEMP_ROOT — geçici parça dosyalarının kök dizini.
#   Default: BASE_DIR/_chunked_uploads
# BACKUP_CHUNK_MAX_SIZE_MB — tek bir chunk için server-side hard limit.
#   Default: 10 MB (client tarafı 5 MB gönderir; bol marj).
# BACKUP_TOTAL_MAX_SIZE_MB — toplam paket boyutu üst limit.
#   Default: 5120 MB (5 GB).
BACKUP_CHUNKED_TEMP_ROOT = env_str('BACKUP_CHUNKED_TEMP_ROOT', '') or os.path.join(BASE_DIR, '_chunked_uploads')
BACKUP_CHUNK_MAX_SIZE_MB = env_int('BACKUP_CHUNK_MAX_SIZE_MB', 10)
BACKUP_TOTAL_MAX_SIZE_MB = env_int('BACKUP_TOTAL_MAX_SIZE_MB', 5120)

# Django default upload handler'ları zaten 2.5 MB üstünü diske yazıyor;
# chunked endpoint'lerinde bu sınır aktif (her chunk <10 MB → bellek-içi OK).
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int(
    'DATA_UPLOAD_MAX_MEMORY_SIZE',
    BACKUP_CHUNK_MAX_SIZE_MB * 1024 * 1024 + 1024 * 1024,  # chunk + form alanları
)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int(
    'FILE_UPLOAD_MAX_MEMORY_SIZE',
    BACKUP_CHUNK_MAX_SIZE_MB * 1024 * 1024,
)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
