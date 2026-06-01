import os
from celery import Celery
from celery.signals import task_prerun, task_postrun

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'juwelier_project.settings')

app = Celery('juwelier_project')

# Broker/result backend — .env REDIS_URL'den okunur. Esurec (db0) ile
# karışmaması için Juwelier varsayılan olarak db1 kullanır. Bu değerler
# settings.py'deki CELERY_BROKER_URL / CELERY_RESULT_BACKEND ile birebir
# aynıdır; config_from_object zaten yükler, burada defansif olarak da set edilir.
_redis_url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/1')
app.conf.broker_url = _redis_url
app.conf.result_backend = _redis_url
app.conf.beat_schedule_filename = os.environ.get(
    'CELERY_BEAT_SCHEDULE_FILE',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'celerybeat-schedule'),
)
app.config_from_object('django.conf:settings', namespace='CELERY')

# ─────────────────────────────────────────────────────────────────────────────
# YEREL SAAT (FAZ 1 — Almanya/Avrupa Pazarı Hazırlığı)
# ─────────────────────────────────────────────────────────────────────────────
# settings.CELERY_TIMEZONE='Europe/Berlin' burada da garanti altına alınır
# (config_from_object'in kapsam dışı kaldığı durumlar için defansif). Tüm
# crontab() schedule'ları Berlin saatine göre tetiklenir; broker'da timestamp
# UTC olarak saklanır → DST geçişlerinde otomatik kayma yapılır.
# ─────────────────────────────────────────────────────────────────────────────
app.conf.timezone = 'Europe/Berlin'
app.conf.enable_utc = True

# --- DB Baglanti Havuzu Korumasi (HATA 6 Fix) ---
# CONN_MAX_AGE=60 ile Celery worker'lari eski baglantilar biriktirir.
# Her task oncesi/sonrasi kapat → "too many clients" hatasini engeller.
@task_prerun.connect
def task_prerun_close_db(sender=None, **kwargs):
    from django.db import close_old_connections
    close_old_connections()

@task_postrun.connect
def task_postrun_close_db(sender=None, **kwargs):
    from django.db import close_old_connections
    close_old_connections()

# Worker her 100 task'tan sonra yeniden baslatilir.
app.conf.worker_max_tasks_per_child = 100
# Ayni anda sadece 1 task on-bellege alinir; ani yuk altinda birikmesi engellenir.
app.conf.worker_prefetch_multiplier = 1

app.autodiscover_tasks(['apps.invoices', 'apps.dashboard', 'apps.banking', 'apps.stock_management', 'apps.gold_purchases', 'apps.backups', 'apps.live_board', 'apps.definitions.rates'])

# ── Celery Beat Schedule ───────────────────────────────────────────────────
# Beat schedule TEK KAYNAKTAN okunur: settings.py → CELERY_BEAT_SCHEDULE.
# config_from_object('django.conf:settings', namespace='CELERY') bunu yükler.
# Burada override YOKTUR — aksi halde settings.py'deki tanım ezilirdi.