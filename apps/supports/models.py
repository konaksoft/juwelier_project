from django.db import models
from apps.accounts.models import *
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
import uuid
import random


class SupportStatus(models.TextChoices):
    PENDING = 'PENDING', _('Beklemede')
    ASSIGNED = 'ASSIGNED', _('Personel Atandı')
    IN_PROGRESS = 'IN_PROGRESS', _('İşlemde')
    STOPPED = 'STOPPED', _('Durduruldu')  # YENİ: Durduruldu statüsü
    WAITING_CUSTOMER = 'WAITING', _('Müşteri Yanıtı Bekleniyor')
    RESOLVED = 'RESOLVED', _('Çözüldü')
    CANCELLED = 'CANCELLED', _('İptal Edildi')


class SupportCloseReason(models.TextChoices):
    RESOLVED = 'RESOLVED', _('Çözüldü (Başarılı)')
    SOFTWARE_UPDATE = 'SOFTWARE_UPDATE', _('Yazılım Güncellemesi Yapıldı')
    USER_ERROR = 'USER_ERROR', _('Kullanıcı Hatası / Eğitim Verildi')
    UNRESOLVABLE = 'UNRESOLVABLE', _('Teknik Olarak İmkansız / Reddedildi')
    DUPLICATE = 'DUPLICATE', _('Mükerrer Talep')
    OTHER = 'OTHER', _('Diğer')


class SupportPriority(models.TextChoices):
    LOW = 'LOW', _('Düşük')
    MEDIUM = 'MEDIUM', _('Orta')
    HIGH = 'HIGH', _('Yüksek')
    CRITICAL = 'CRITICAL', _('Kritik')


class PersonelSupport(models.Model):
    CATEGORY_TYPES = (
        ('error', 'Hata'),
        ('developer', 'Geliştirme'),
        ('support', 'Teknik Destek')
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_no = models.CharField(
        max_length=6,
        unique=True,
        editable=False,
        verbose_name=_('Talep No'),
        null=True,  # Mevcut veriler hata vermesin diye geçici olarak null=True
        blank=True
    )
    category = models.CharField(max_length=20, choices=CATEGORY_TYPES, default='error')
    personel_request = models.ForeignKey(
        Users,
        on_delete=models.CASCADE,
        related_name='personel_request',
        verbose_name=_('Talep Oluşturan')
    )

    assigned_staff = models.ForeignKey(
        Users,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_requests',
        verbose_name=_('Atanan Personel')
    )

    title = models.CharField(max_length=255, verbose_name=_('Konu'))

    status = models.CharField(
        max_length=20,
        choices=SupportStatus.choices,
        default=SupportStatus.PENDING,
        verbose_name=_('Durum')
    )

    priority = models.CharField(
        max_length=20,
        choices=SupportPriority.choices,
        default=SupportPriority.MEDIUM,
        verbose_name=_('Öncelik')
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Oluşturulma Tarihi'))
    created_at_start = models.DateTimeField(null=True, blank=True)
    created_at_end = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Kapanış Tarihi'))
    is_deleted = models.BooleanField(default=False)
    closing_reason = models.CharField(
        max_length=50,
        choices=SupportCloseReason.choices,
        null=True,
        blank=True,
        verbose_name=_('Kapanış Nedeni')
    )
    closing_description = models.TextField(
        null=True,
        blank=True,
        verbose_name=_('Kapanış Açıklaması')
    )

    class Meta:
        db_table = "PersonelSupports"
        ordering = ['-created_at']
        verbose_name = _('Müşteri Talebi')
        verbose_name_plural = _('Müşteri Talepleri')

    def save(self, *args, **kwargs):
        if not self.ticket_no:
            while True:
                new_ticket = str(random.randint(100000, 999999))
                if not PersonelSupport.objects.filter(ticket_no=new_ticket).exists():
                    self.ticket_no = new_ticket
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        display_no = self.ticket_no if self.ticket_no else str(self.id)[:8]
        return f"#{display_no} - {self.title}"


class SupportWorkSession(models.Model):
    request = models.ForeignKey(
        PersonelSupport,
        on_delete=models.CASCADE,
        related_name='work_sessions'
    )
    start_time = models.DateTimeField(auto_now_add=True, verbose_name=_('Başlangıç'))
    end_time = models.DateTimeField(null=True, blank=True, verbose_name=_('Bitiş'))

    class Meta:
        db_table = "SupportWorkSessions"
        ordering = ['start_time']

    @property
    def duration(self):
        end = self.end_time if self.end_time else timezone.now()
        diff = end - self.start_time
        return diff.total_seconds() / 60


class RequestMessage(models.Model):
    request = models.ForeignKey(
        PersonelSupport,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    sender = models.ForeignKey(
        Users,
        on_delete=models.CASCADE,
        verbose_name=_('Gönderen')
    )

    message = models.TextField(verbose_name=_('Mesaj'))
    attachment = models.FileField(upload_to='supports/replies/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_new_message = models.BooleanField(default=True)
    is_internal_log = models.BooleanField(default=False, verbose_name=_('Sistem Logu mu?'))

    class Meta:
        ordering = ['created_at']
        db_table = "RequestMessages"
        verbose_name = ('Talep Mesajı')
        verbose_name_plural = _('Talep Mesajları')


class TrainingVideo(models.Model):
    module_name = models.CharField(max_length=100, verbose_name=_('Modül Adı'))
    title = models.CharField(max_length=255, verbose_name=_('Video Başlığı'))
    youtube_id = models.CharField(
        max_length=50,
        verbose_name=_('YouTube Video ID'),
        help_text=_('YouTube video ID\'si (Örn: dQw4w9WgXcQ)')
    )
    order = models.PositiveIntegerField(default=0, verbose_name=_('Sıralama'))
    is_active = models.BooleanField(default=True, verbose_name=_('Aktif'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "TrainingVideos"
        ordering = ['module_name', 'order']
        verbose_name = _('Eğitim Videosu')
        verbose_name_plural = _('Eğitim Videoları')

    def __str__(self):
        return f"{self.module_name} - {self.title}"
