"""
==============================================================================
 FAZ E — Yedekleme Celery Görevleri
==============================================================================

Tarih: 2026-05-05
Amaç:
    - Otomatik günlük yedek (DB-only) — gece 03:00
    - Haftalık tam yedek (DB + media) — Pazar gecesi 04:00
    - Retention politikası — eski yedekleri otomatik temizle

Konfigürasyon (jewelery_project/settings.py CELERY_BEAT_SCHEDULE):
    'backups.daily_db_backup_all_companies' → günlük 03:00
    'backups.weekly_full_backup_all_companies' → Pazar 04:00
    'backups.cleanup_old_backups' → günlük 02:00 (yedek alma öncesi)

Retention Default Değerler:
    BACKUP_DAILY_RETENTION_DAYS = 7  (.env override edilebilir)
    BACKUP_WEEKLY_RETENTION_DAYS = 30
    BACKUP_MIN_KEEP = 3 (her firmada en az 3 yedek tutulur, retention'dan
                        bağımsız — silinmesin diye güvenlik)

Karar 2 (Onaylı):
    Otomatik yedeklerde include_media=False (DB-only). Diskleri ve I/O'yu
    şişirmemek için. Haftada 1 kez full (media dahil) alınır.
==============================================================================
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


# .env'den okunabilir varsayılanlar
def _retention_days(name, default):
    val = getattr(settings, name, None)
    if val is None:
        return default
    try:
        return int(val)
    except Exception:
        return default


# ==============================================================================
#  GÜNLÜK DB YEDEĞİ — Tüm Firmalar
# ==============================================================================
@shared_task(
    name='backups.daily_db_backup_all_companies',
    ignore_result=True,
    soft_time_limit=1800,   # 30 dk
    time_limit=2100,
)
def daily_db_backup_all_companies():
    """
    Aktif tüm firmalar için DB-only ZIP yedeği alır.
    Karar 2: include_media=False (gece taraması diski şişirmesin).
    """
    from apps.stores.models import Company
    from apps.backups.services import BackupService

    companies = Company.objects.filter(is_deleted=False) if hasattr(Company, 'is_deleted') else Company.objects.all()

    total_ok = 0
    total_err = 0
    for c in companies:
        try:
            svc = BackupService(c.id)
            svc.create_backup_zip(
                note=f"Otomatik günlük yedek — {timezone.now():%d.%m.%Y %H:%M}",
                user=None,
                include_media=False,
            )
            total_ok += 1
            logger.info("[backups] Günlük yedek alındı: company_id=%s", c.id)
        except Exception as e:
            total_err += 1
            logger.exception("[backups] Günlük yedek hatası: company_id=%s err=%s", c.id, e)

    logger.info("[backups] Günlük yedek özet — başarılı: %d, hata: %d", total_ok, total_err)
    return {'ok': total_ok, 'err': total_err}


# ==============================================================================
#  HAFTALIK TAM YEDEK (DB + MEDIA) — Tüm Firmalar
# ==============================================================================
@shared_task(
    name='backups.weekly_full_backup_all_companies',
    ignore_result=True,
    soft_time_limit=7200,   # 2 saat
    time_limit=7800,
)
def weekly_full_backup_all_companies():
    """
    Aktif tüm firmalar için DB + media ZIP yedeği alır.
    Pazar gecesi 04:00 çalışır (en az trafik).
    """
    from apps.stores.models import Company
    from apps.backups.services import BackupService

    companies = Company.objects.filter(is_deleted=False) if hasattr(Company, 'is_deleted') else Company.objects.all()

    total_ok = 0
    total_err = 0
    for c in companies:
        try:
            svc = BackupService(c.id)
            svc.create_backup_zip(
                note=f"Otomatik haftalık tam yedek (media dahil) — {timezone.now():%d.%m.%Y}",
                user=None,
                include_media=True,
            )
            total_ok += 1
            logger.info("[backups] Haftalık tam yedek alındı: company_id=%s", c.id)
        except Exception as e:
            total_err += 1
            logger.exception(
                "[backups] Haftalık tam yedek hatası: company_id=%s err=%s", c.id, e
            )

    logger.info("[backups] Haftalık özet — başarılı: %d, hata: %d", total_ok, total_err)
    return {'ok': total_ok, 'err': total_err}


# ==============================================================================
#  RETENTION POLİTİKASI — Eski Yedekleri Sil
# ==============================================================================
@shared_task(
    name='backups.cleanup_old_backups',
    ignore_result=True,
    soft_time_limit=600,
    time_limit=900,
)
def cleanup_old_backups():
    """
    Retention politikası:
      - daily yedekler (.zip / _db_): BACKUP_DAILY_RETENTION_DAYS gün sonra silinir
      - weekly yedekler (_full_): BACKUP_WEEKLY_RETENTION_DAYS gün sonra silinir
      - manuel JSON yedekler: dokunulmaz (kullanıcı bilerek almış)
      - her firmada EN AZ BACKUP_MIN_KEEP yedek korunur (güvenlik)

    RestoreAuditLog'da PROTECT FK olduğu için audit'e bağlı yedekler
    silinmek istense bile ProtectedError yer; bu durumda log'a yazılır,
    yedek silinmez.
    """
    from apps.backups.models import CompanyBackup
    from apps.stores.models import Company

    daily_days = _retention_days('BACKUP_DAILY_RETENTION_DAYS', 7)
    weekly_days = _retention_days('BACKUP_WEEKLY_RETENTION_DAYS', 30)
    min_keep = _retention_days('BACKUP_MIN_KEEP', 3)

    now = timezone.now()
    daily_cutoff = now - timedelta(days=daily_days)
    weekly_cutoff = now - timedelta(days=weekly_days)

    deleted = 0
    protected = 0
    skipped_min_keep = 0

    companies = Company.objects.all()
    for c in companies:
        all_backups = list(
            CompanyBackup.objects
                .filter(company=c, status='COMPLETED')
                .order_by('-created_at')
        )
        if len(all_backups) <= min_keep:
            continue  # min_keep'i koru

        # min_keep dışındaki adaylar
        candidates = all_backups[min_keep:]

        for b in candidates:
            file_name = (b.backup_file.name if b.backup_file else '').lower()

            # JSON manuel yedeklere dokunma
            if file_name.endswith('.json'):
                continue

            # _full_ — weekly retention
            if '_full_' in file_name:
                if b.created_at >= weekly_cutoff:
                    continue
            # _db_ veya .zip — daily retention
            else:
                if b.created_at >= daily_cutoff:
                    continue

            # Sil (RestoreAuditLog PROTECT olduğu için audit varsa hata yer)
            try:
                # Önce fiziksel dosyayı sil
                if b.backup_file:
                    try:
                        b.backup_file.delete(save=False)
                    except Exception:
                        pass
                b.delete()
                deleted += 1
            except Exception as e:
                # PROTECT FK (RestoreAuditLog) → silme reddedildi
                protected += 1
                logger.warning(
                    "[backups] Eski yedek silinemedi (audit PROTECT?): id=%s err=%s",
                    b.id, e,
                )

    logger.info(
        "[backups] Cleanup özet — silindi: %d, korundu(audit): %d, atlandı(min_keep): %d",
        deleted, protected, skipped_min_keep,
    )
    return {'deleted': deleted, 'protected': protected}


# ==============================================================================
#  FAZ 60.2 — Chunked Upload Cleanup (günlük)
# ==============================================================================

@shared_task(name='backups.cleanup_chunked_uploads')
def cleanup_chunked_uploads():
    """
    Süresi dolmuş veya yarım kalan parçalı yükleme oturumlarını temizler.
    Beat schedule (önerilen): her gece 02:30.
    """
    try:
        from apps.backups.chunked_upload import ChunkedUploadService
        result = ChunkedUploadService.cleanup_expired()
        logger.info(
            "[backups] Chunked cleanup özet — expired: %d, removed_files: %d, kept: %d",
            result['expired'], result['removed_files'], result['kept_completed'],
        )
        return result
    except Exception as e:
        logger.exception("[backups] Chunked cleanup hatası: %s", e)
        return {'error': str(e)}
