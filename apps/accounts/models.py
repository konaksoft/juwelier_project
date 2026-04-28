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
