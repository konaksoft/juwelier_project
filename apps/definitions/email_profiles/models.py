import uuid
from django.db import models


class EmailProfiles(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, verbose_name='Dil', blank=True, null=True)
    username = models.CharField(max_length=150, verbose_name='Kullanıcı Adı', blank=True, null=True)
    password = models.CharField(max_length=150, verbose_name='Şifre', blank=True, null=True)
    server = models.CharField(max_length=150, verbose_name='E-posta Sunucusu', blank=True, null=True)
    port = models.CharField(max_length=150, verbose_name='Port', blank=True, null=True)
    sender = models.CharField(max_length=150, verbose_name='Gönderen', blank=True, null=True)
    ssl = models.BooleanField(default=False, verbose_name='SSL', blank=True, null=True)
    tls = models.BooleanField(default=False, verbose_name='TSL', blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'EmailProfiles'
        ordering = ['name']
