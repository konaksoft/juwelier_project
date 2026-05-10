import os
from celery import Celery
from celery.schedules import crontab
from celery.signals import task_prerun, task_postrun

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'juwelier_project.settings')

app = Celery('juwelier_project')

app.conf.broker_url = 'redis://127.0.0.1:6379/0'
app.conf.result_backend = 'redis://127.0.0.1:6379/0'
app.conf.beat_schedule_filename = '/var/run/celery/celerybeat-schedule'
app.config_from_object('django.conf:settings', namespace='CELERY')

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

app.autodiscover_tasks(['apps.invoices', 'apps.products', 'apps.dashboard', 'apps.banking', 'apps.stock_management', 'apps.gold_purchases', 'apps.backups'])


# ── Celery Beat Schedule ───────────────────────────────────────────────────
app.conf.beat_schedule = {
    'banking-delta-fetch-every-30min': {
        'task': 'banking.fetch_latest_bank_transactions',
        'schedule': 30 * 60,  # Her 30 dakikada bir (saniye cinsinden)
        'options': {'queue': 'default'},
    },
    # ── Stok Yonetimi Tasklari ──
    'stock-fetch-prices-every-60s': {
        'task': 'stock_management.fetch_prices_from_providers',
        'schedule': 60,  # Her 60 saniyede bir
        'options': {'queue': 'default'},
    },
    'stock-integrity-check-daily': {
        'task': 'stock_management.daily_stock_integrity_check',
        'schedule': crontab(hour=0, minute=5),  # Her gun 00:05
        'options': {'queue': 'default'},
    },
    'stock-cleanup-old-quotes-weekly': {
        'task': 'stock_management.cleanup_old_price_quotes',
        'schedule': crontab(hour=3, minute=0, day_of_week=0),  # Pazar 03:00
        'options': {'queue': 'default'},
    },
    # ── FAZ R-3: Dashboard Rollup Task'ları ──
    'dashboard-nightly-rollup': {
        'task': 'dashboard.compute_daily_rollups',
        'schedule': crontab(hour=2, minute=5),  # Her gece 02:05
        'options': {'queue': 'default'},
    },
    'dashboard-today-rollup-15min': {
        'task': 'dashboard.compute_today_rollup',
        'schedule': 15 * 60,  # Her 15 dakikada bir (saniye cinsinden)
        'options': {'queue': 'default'},
    },
    # ── FAZ 60.2: Yarım kalan parçalı yüklemeleri günlük temizle ──
    'backups-cleanup-chunked-uploads-daily': {
        'task': 'backups.cleanup_chunked_uploads',
        'schedule': crontab(hour=2, minute=30),  # Her gece 02:30
        'options': {'queue': 'default'},
    },
}