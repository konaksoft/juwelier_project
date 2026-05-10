"""UI / Playwright test ortamı için Django ayarları (FAZ 17).

GÜVENLİK:
  - Üretim .env dosyası OKUNMAZ.
  - Veritabanı kimlik bilgileri YALNIZCA .env.test dosyasından yüklenir.
  - SECRET_KEY ve tüm dış servis değerleri settings_test.py'den gelir (sahte).

Kurulum:
  cp .env.test.example .env.test
  # .env.test içindeki TEST_DB_* değerlerini test veritabanı bilgileriyle doldur.

Çalıştırma:
  pytest -c pytest-ui.ini                         # tüm UI testleri
  pytest -c pytest-ui.ini --browser chromium      # yalnız Chrome
  pytest -c pytest-ui.ini --headed --slowmo 800   # tarayıcıyı görsel izle
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent

# Sadece .env.test yükle — üretim .env'ini asla okuma.
# override=True: shell'den gelen olası stray değerlerin üzerine yaz.
_env_test_path = _BASE_DIR / ".env.test"
if not _env_test_path.exists():
    raise FileNotFoundError(
        f"\n\n⛔  UI testleri için .env.test bulunamadı: {_env_test_path}\n"
        f"   Kurulum:\n"
        f"     cp {_BASE_DIR}/.env.test.example {_env_test_path}\n"
        f"   Ardından .env.test içindeki TEST_DB_* değerlerini doldur.\n"
    )
load_dotenv(_env_test_path, override=True)

# Güvenli temel ayarları settings_test.py'den al (load_dotenv() çağırmaz).
from juwelier_project.settings_test import *  # noqa: F401,F403

# ─── Veritabanı — .env.test'ten gelen gerçek test PostgreSQL DB ──────────────
# SECRET_KEY bu blokta KASITLI OLARAK OVERRIDE EDİLMEZ; settings_test.py'den
# gelen test değeri korunur. Yalnız DB bağlantı bilgileri .env.test'ten alınır.
_test_db_name = os.environ.get("TEST_DB_NAME", "")
if not _test_db_name:
    raise ValueError(
        "\n⛔  .env.test içinde TEST_DB_NAME tanımlı değil!\n"
        "   .env.test.example dosyasını incele.\n"
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql_psycopg2",
        "NAME": _test_db_name,
        "USER": os.environ.get("TEST_DB_USER", ""),
        "PASSWORD": os.environ.get("TEST_DB_PASSWORD", ""),
        "HOST": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("TEST_DB_PORT", "5432"),
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        # Pytest-django varsayılan olarak 'test_' prefix'i ekler.
        # TEST.NAME = gerçek DB adı → pytest doğrudan mevcut DB'ye bağlanır.
        "TEST": {
            "NAME": _test_db_name,
        },
    }
}
