from datetime import timedelta

from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.utils import timezone
from apps.stores.models import Company
import os, uuid


class CompanyBackup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='backups')
    backup_file = models.FileField(upload_to='CompanyBackups/')
    file_size = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by_user = models.CharField(max_length=150, blank=True, null=True)  # Yedek alan kişi
    note = models.TextField(blank=True, null=True)
    STATUS_CHOICES = [
        ('PENDING', 'Hazırlanıyor'),
        ('COMPLETED', 'Tamamlandı'),
        ('FAILED', 'Hata'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='COMPLETED')

    class Meta:
        db_table = 'CompanyBackup'
        ordering = ['-created_at']
        verbose_name = "Firma Yedeği"
        verbose_name_plural = "Firma Yedekleri"

    def __str__(self):
        return f"{self.company.title} - {self.created_at.strftime('%d.%m.%Y %H:%M')}"

    def save(self, *args, **kwargs):
        # Dosya boyutu hesaplama
        if self.backup_file and hasattr(self.backup_file, 'size'):
            mb_size = self.backup_file.size / (1024 * 1024)
            self.file_size = f"{mb_size:.2f} MB"
        super().save(*args, **kwargs)


class RestoreAuditLog(models.Model):
    """
    FAZ A.4 — Yedekten Geri Yükleme İz Kaydı (Append-Only Audit Log).

    Amaç:
        Append-only ledger katmanına (CustomerLedger, CashboxLedger vb.)
        ek alan eklemeden, hangi kaydın hangi yedekten / ne zaman / kim tarafından
        geri yüklendiğini merkezi olarak takip eder.

    Kullanım Şekli:
      - Full Restore: tek bir toplu giriş (target boş, restore_notes'da özet).
        Yüksek hacimde her satır için ayrı log yazmak performans kaybı yapar.
      - Smart Restore: her item için ayrı bir giriş.
        idempotency_key ile çift yazımı önlemek için kullanılır.
        target alanı (Generic FK) etkilenen kayda işaret eder.

    Audit Bütünlüğü:
      - backup → CompanyBackup (PROTECT): Audit kaybı yaşanmasın.
      - original_created_at / original_created_by: Yedekteki orijinal değerler
        (denetim için dondurulmuş kopya).
    """

    RESTORE_TYPES = [
        ('FULL', 'Tam Firma Geri Yüklemesi'),
        ('SMART', 'Akıllı Geri Yükleme (Merge)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup = models.ForeignKey(
        CompanyBackup,
        on_delete=models.PROTECT,
        related_name='restore_logs',
        help_text='Bu kaydın geri yüklendiği yedek dosyası.'
    )
    restore_type = models.CharField(
        max_length=10,
        choices=RESTORE_TYPES,
        db_index=True,
        help_text='FULL = wipe & load, SMART = merge / upsert.'
    )

    # --- Hangi kayıt etkilendi (Generic FK) ---
    # Full Restore'da boş kalır (toplu işlem, satır bazlı log yazmıyoruz).
    # Smart Restore'da her item için ayrı kayıt → content_type + object_id dolu.
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        help_text='Smart restore: hangi modele ait kayıt yüklendi.'
    )
    object_id = models.UUIDField(
        null=True, blank=True,
        db_index=True,
        help_text='Smart restore: ilgili kaydın UUID\'si.'
    )
    target = GenericForeignKey('content_type', 'object_id')

    # --- Smart Restore Idempotency ---
    # sha256(barcode + supplier_tax_no + sale_date) gibi belirleyici alanlardan üretilir.
    # Aynı yedek tekrar yüklenirse bu key ile çift yazımı engelleriz.
    idempotency_key = models.CharField(
        max_length=128,
        blank=True, null=True,
        db_index=True,
        help_text='Smart restore çift yazım koruması için belirleyici anahtar.'
    )

    # --- Audit (yedekteki orijinal alanlar dondurulmuş kopya) ---
    original_created_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Yedekteki orijinal created_at — denetim için dondurulmuş.'
    )
    original_created_by = models.CharField(
        max_length=150, blank=True, null=True,
        help_text='Yedekteki orijinal created_by — denetim için dondurulmuş.'
    )

    # --- Restore Meta ---
    restored_at = models.DateTimeField(auto_now_add=True, db_index=True)
    restored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='restore_actions',
        help_text='Geri yüklemeyi tetikleyen kullanıcı.'
    )
    restore_notes = models.TextField(
        blank=True, null=True,
        help_text='Restore sonrası özet notlar (kayıt sayısı, atlanan vs.).'
    )

    # --- Smart Restore Şüpheli Eşleşmeler (Strateji 1 — Konservatif) ---
    # match_or_create sırasında yeni kayıt açıldı ama mevcut DB'de benzer
    # kayıtlar var. Kullanıcı manuel inceleme listesinde bunları görür.
    similarity_warnings = models.JSONField(
        default=dict, blank=True,
        help_text='Şüpheli eşleşmeler için potential_matches listesi.'
    )

    class Meta:
        db_table = 'RestoreAuditLog'
        ordering = ['-restored_at']
        verbose_name = 'Geri Yükleme Audit Kaydı'
        verbose_name_plural = 'Geri Yükleme Audit Kayıtları'
        indexes = [
            models.Index(fields=['content_type', 'object_id'], name='ral_target_idx'),
            models.Index(fields=['backup', 'restore_type'], name='ral_backup_type_idx'),
            models.Index(fields=['idempotency_key'], name='ral_idempotency_idx'),
        ]

    def __str__(self):
        target_str = f" → {self.content_type.model}:{self.object_id}" if self.content_type_id else ""
        return f"[{self.restore_type}] {self.backup_id} @ {self.restored_at:%d.%m.%Y %H:%M}{target_str}"


# ==============================================================================
#  FAZ 60.2 — Parçalı Yükleme Oturumu (Cloudflare 413 By-pass)
# ==============================================================================
class ChunkedUploadSession(models.Model):
    """
    Smart Restore büyük dosyalarını (özellikle ZIP+media içeren paketleri)
    Cloudflare'in 100 MB body limitini aşmadan yüklemek için parça parça
    upload mekanizmasının state'ini tutar.

    Yaşam Döngüsü:
        PENDING (init)
          → UPLOADING (ilk chunk geldi)
          → READY (tüm chunk'lar geldi, restore'a hazır)
          → COMPLETED (restore başarılı) | FAILED | ABORTED | EXPIRED

    Append-Only Davranış:
        - received_chunks artar, asla azalmaz.
        - Bir chunk_index iki kez gelirse ikinci yazma reddedilir (idempotent).
        - status sadece ileri yönde değişir (PENDING → UPLOADING → READY → terminal).

    Disk Yönetimi:
        - temp_file_path mutlak yol: settings.BASE_DIR / '_chunked_uploads' /
          {upload_id}.bin gibi.
        - cleanup_expired() command'ı expires_at < now olanları siler.

    Güvenlik:
        - user FK: yüklemeyi başlatan kişi (silinse bile audit kayıt kalır).
        - store_id: hangi mağazaya restore edileceği (cross-store engellemek için
          finalize sırasında permission tekrar doğrulanır).
        - filename: client-bildirimi (sanitize edilerek saklanır).
    """

    STATUS_CHOICES = [
        ('PENDING', 'Bekliyor'),
        ('UPLOADING', 'Yükleniyor'),
        ('READY', 'Tamamlandı (Restore Bekliyor)'),
        ('COMPLETED', 'Restore Edildi'),
        ('ABORTED', 'İptal Edildi'),
        ('FAILED', 'Hata'),
        ('EXPIRED', 'Süresi Doldu'),
    ]

    DEFAULT_TTL_HOURS = 24

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='chunked_uploads',
        help_text='Yüklemeyi başlatan kullanıcı.'
    )
    store_id = models.UUIDField(
        db_index=True,
        help_text='Hedef mağaza UUID — restore bu mağazaya yapılacak.'
    )
    backup_id = models.UUIDField(
        blank=True, null=True, db_index=True,
        help_text='Opsiyonel: paketin geldiği yedek (audit referans).'
    )
    filename = models.CharField(
        max_length=255,
        help_text='Yüklenen dosyanın orijinal adı.'
    )
    total_size = models.BigIntegerField(
        help_text='Toplam dosya boyutu (byte).'
    )
    total_chunks = models.IntegerField(
        help_text='Beklenen toplam parça sayısı.'
    )
    chunk_size = models.IntegerField(
        help_text='Sabit parça boyutu (byte) — son parça hariç.'
    )
    received_chunks = models.IntegerField(
        default=0,
        help_text='Şu ana kadar başarıyla alınan parça sayısı.'
    )
    received_bytes = models.BigIntegerField(
        default=0,
        help_text='Şu ana kadar yazılan byte sayısı.'
    )
    temp_file_path = models.CharField(
        max_length=500,
        help_text='Sunucuda parçaların append edildiği dosya yolu.'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING',
        db_index=True,
    )
    error_message = models.TextField(
        blank=True, null=True,
        help_text='FAILED ise hata detayı.'
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(
        db_index=True,
        help_text='Bu zamandan sonra cleanup task tarafından silinir.'
    )

    class Meta:
        db_table = 'ChunkedUploadSession'
        ordering = ['-created_at']
        verbose_name = 'Parçalı Yükleme Oturumu'
        verbose_name_plural = 'Parçalı Yükleme Oturumları'
        indexes = [
            models.Index(fields=['user', 'status'], name='cus_user_status_idx'),
            models.Index(fields=['status', 'expires_at'], name='cus_status_expiry_idx'),
        ]

    def __str__(self):
        return (
            f"[{self.status}] {self.filename} "
            f"({self.received_chunks}/{self.total_chunks} chunks)"
        )

    @classmethod
    def default_expiry(cls):
        return timezone.now() + timedelta(hours=cls.DEFAULT_TTL_HOURS)

    @property
    def progress_percent(self):
        if self.total_chunks <= 0:
            return 0
        return int(round(100.0 * self.received_chunks / self.total_chunks))

    def is_terminal(self):
        return self.status in ('COMPLETED', 'ABORTED', 'FAILED', 'EXPIRED')
