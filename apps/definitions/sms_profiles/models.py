import uuid
from django.db import models
import requests
from django.conf import settings


class SmsProfiles(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50, verbose_name='Dil', blank=True, null=True)

    api_url = models.CharField(max_length=500, verbose_name='Api Url', blank=True, null=True)
    username = models.CharField(max_length=50, verbose_name='Kullanıcı Adı', blank=True, null=True)
    password = models.CharField(max_length=50, verbose_name='Şifre', blank=True, null=True)
    sms_header = models.CharField(max_length=50, verbose_name='SMS Başlığı', blank=True, null=True)
    sms_provider = models.CharField(max_length=50, verbose_name='SMS Sağlayıcısı', blank=True, null=True)
    description = models.CharField(max_length=500, verbose_name='Açıklama', blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'SmsProfiles'
        ordering = ['name']

