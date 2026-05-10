import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.definitions.locations.models import *
from apps.roles.models import *
from apps.stores.models import *
from apps.stores.models import Stores


class Users(AbstractUser):
    personal_type = models.CharField(max_length=120, blank=True, null=True)
    job_title = models.CharField(max_length=120, blank=True, null=True)
    mobile_phone = models.CharField(max_length=30, blank=True, null=True)
    avatar = models.ImageField(default="default/user.png", upload_to='accounts/', null=True, blank=True)
    identification_number = models.CharField(max_length=11, null=True, blank=True)
    role = models.ForeignKey(Roles, related_name='roles', on_delete=models.CASCADE, null=True, blank=True)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    is_email_verified = models.BooleanField(default=False, null=True, blank=True)
    is_phone_verified = models.BooleanField(default=False, null=True, blank=True)

    activate_2fa = models.BooleanField(default=False, null=True, blank=True)
    is_blocked = models.BooleanField(default=False, null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)
    is_protected = models.BooleanField(default=False, null=True, blank=True)
    failed_login_attempts = models.IntegerField(default=0, verbose_name="Hatalı Giriş Sayısı")
    blocked_until = models.DateTimeField(null=True, blank=True, verbose_name="Engel Bitiş Tarihi")

    def __str__(self):
        return self.username

    class Meta:
        db_table = 'Users'
        ordering = ['username']


class ContactConsent(models.Model):
    CHANNELS = (('phone', 'phone'), ('email', 'email'))
    OWNERS = (('user', 'user'), ('customer', 'customer'), ('store', 'store'))
    IYS_STATUSES = (('pending', 'pending'), ('synced', 'synced'), ('rejected', 'rejected'))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ⬇️ DEĞİŞTİ: user FK yerine generic sahiplik
    owner_type = models.CharField(max_length=12, choices=OWNERS)
    owner_id = models.CharField(max_length=64)  # Users.id (int) / Customers.id (uuid) / Stores.id (uuid) metin

    channel = models.CharField(max_length=10, choices=CHANNELS)
    is_consented = models.BooleanField(default=False)
    consented_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=50, default='otp_verify')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    iys_status = models.CharField(max_length=10, choices=IYS_STATUSES, default='pending')
    iys_ref = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        unique_together = ('owner_type', 'owner_id', 'channel')
        indexes = [
            models.Index(fields=['owner_type', 'owner_id', 'channel']),
        ]


# ============================================================================
# FAZ 45 — Multi-Branch: Kullanıcı-Şube Çoklu Erişim Tablosu
# ============================================================================
#
# Mevcut Users.store FK'si tek-şube bağlamında doğrudur ve kaldırılmaz —
# sistemdeki 200+ view bu alanı okumaktadır. Bu tablo onun ÜZERİNE bir
# katman eklenir: çoklu şube erişimi olan kullanıcılar (patron, genel
# müdür, denetçi) için ek erişim hakları tanımlar.
#
# DAVRANIŞ KURALI:
#   - is_primary=True olan kayıt, daima Users.store FK'si ile senkron
#     tutulur. (Veri migration'ı bunu kuracak — FAZ 45.3 backfill.)
#   - Sıradan personel için tek satır olur (kendi şubesi, is_primary=True).
#   - Patron için birden fazla satır olabilir; biri is_primary=True.
#   - get_active_store(request) helper'ı session'a bakar; çok şubeli
#     kullanıcılar için aktif şubeyi döner. Tek şubeliler için Users.store
#     fallback olarak kullanılır.
#
# Bu tablo FAZ 45'te DORMANT'tır — hiçbir view/middleware henüz okumaz.
# FAZ 48 (Konsolide Patron Dashboard) aktivasyonunda canlı kullanılacaktır.
# ============================================================================

class UserStoreAccess(models.Model):
    """Bir kullanıcının erişebileceği şubeleri ve her şubedeki rolünü tutar.

    Çoklu şube modelinin temelidir. Tek-şube davranışında her kullanıcı için
    is_primary=True olan TEK kayıt vardır ve Users.store ile senkron çalışır.
    Çok-şube aktivasyonunda (FAZ 48) ek satırlar oluşturulur.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        'accounts.Users',
        on_delete=models.CASCADE,
        related_name='store_accesses',
        verbose_name='Kullanıcı',
    )
    store = models.ForeignKey(
        Stores,
        on_delete=models.CASCADE,
        related_name='user_accesses',
        verbose_name='Şube',
    )
    role = models.ForeignKey(
        Roles,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='store_accesses',
        verbose_name='Bu Şubedeki Rol',
        help_text='Kullanıcının bu özel şubedeki rolü. NULL ise Users.role kullanılır.',
    )

    is_primary = models.BooleanField(
        default=True,
        verbose_name='Birincil Şube mi?',
        help_text='Kullanıcının varsayılan şubesi. Users.store FK ile senkron tutulur. Her kullanıcı için tam olarak 1 satır True olmalıdır.',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Erişim Aktif mi?',
        help_text='Geçici erişim kapatma için. Pasif erişimler get_active_store tarafından kabul edilmez.',
    )

    granted_at = models.DateTimeField(default=timezone.now, verbose_name='Erişim Verildi')
    granted_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
        verbose_name='Erişimi Veren',
        help_text='Bu çoklu erişimi atayan üst yönetici (audit için).',
    )
    notes = models.CharField(max_length=255, blank=True, default='', verbose_name='Not')

    class Meta:
        db_table = 'UserStoreAccesses'
        verbose_name = 'Kullanıcı-Şube Erişimi'
        verbose_name_plural = 'Kullanıcı-Şube Erişimleri'
        unique_together = (('user', 'store'),)
        indexes = [
            models.Index(fields=['user', 'is_active'], name='usa_user_active_idx'),
            models.Index(fields=['store', 'is_active'], name='usa_store_active_idx'),
            models.Index(fields=['user', 'is_primary'], name='usa_user_primary_idx'),
        ]

    def __str__(self):
        primary = ' (Birincil)' if self.is_primary else ''
        return f"{self.user.username} → {self.store}{primary}"


class OtpCode(models.Model):
    PURPOSE_CHOICES = (
        ('verify_email', 'verify_email'),
        ('verify_phone', 'verify_phone'),
        ('twofa_login', 'twofa_login'),
    )
    OWNER_CHOICES = (('user', 'user'), ('store', 'store'), ('customer', 'customer'))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_type = models.CharField(max_length=10, choices=OWNER_CHOICES)
    owner_id = models.CharField(max_length=64)  # user.id (int) veya store.id (uuid) string saklıyoruz
    channel = models.CharField(max_length=10, choices=(('email', 'email'), ('phone', 'phone')))
    code = models.CharField(max_length=10)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=['owner_type', 'owner_id', 'purpose', 'channel']),
        ]
