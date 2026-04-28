# apps/devices/models.py
import uuid
from decimal import Decimal
from django.db import models

from apps.accounts.models import Users


class Device(models.Model):
    CURRENCY_CHOICES = (
        ('TRY', 'TRY'),
        ('USD', 'USD'),
        ('EUR', 'EUR'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200, verbose_name="Cihaz Adı")
    code = models.CharField(max_length=50, unique=True, verbose_name="Stok Kodu / Model")
    description = models.TextField(blank=True, null=True, verbose_name="Açıklama")

    # Fiyatlandırma
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Fiyat")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD', verbose_name="Para Birimi")

    # Görsel ve Durum
    image = models.ImageField(upload_to='devices/', null=True, blank=True, verbose_name="Cihaz Resmi")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'Devices'
        ordering = ['name']
        verbose_name = 'Cihaz'
        verbose_name_plural = 'Cihazlar'

    def __str__(self):
        return f"{self.name} ({self.price} {self.currency})"

    @property
    def currency_symbol(self):
        return {'TRY': '₺', 'USD': '$', 'EUR': '€'}.get(self.currency, self.currency)
