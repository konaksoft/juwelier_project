from django.conf import settings as dj_settings
from django.core.cache import cache

# Django settings.py içinde override edilebilecek varsayılanlar
# apps/settings/conf.py
DEFAULTS = {
    "AUTO_IMAGE_PROCESSING_ENABLED": True,
    "AUTO_IMAGE_PROCESSING_SKIP_MODELS": [],
    "IMAGE_MAX_WIDTH": 1200,
    "IMAGE_MAX_HEIGHT": 1200,
    "IMAGE_QUALITY": 85,
    "IMAGE_MAX_KB": 150,  # ← yeni
    "IMAGE_PREFER_WEBP": True,  # ← yeni
    "IMAGE_KEEP_EXIF": False,  # ← yeni
}

# (İsteğe bağlı) DB tablosundan runtime ayarı okumak için küçük yardımcı.
# Yoksa sadece Django settings + DEFAULTS çalışır.
CACHE_TTL = 60  # sn


def _from_django(name):
    return getattr(dj_settings, name, DEFAULTS.get(name))


def get(name):
    # Öncelik: Django settings.py  → sonra DEFAULTS
    return _from_django(name)
