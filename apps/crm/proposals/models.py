import uuid
from decimal import Decimal
from functools import cached_property
from django.db import models
from django.utils import timezone
from django.conf import settings

from apps.stores.models import Company

from apps.crm.leads.models import *


class Proposals(models.Model):
    STATUS_CHOICES = (
        ('draft', 'Taslak'),
        ('sent', 'Gönderildi'),
        ('accepted', 'Onaylandı'),
        ('rejected', 'Reddedildi'),
        ('revised', 'Revize Edildi'),
    )
    CURRENCY_CHOICES = (
        ('TRY', 'TRY'),
        ('USD', 'USD'),
        ('EUR', 'EUR'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal_no = models.CharField(max_length=30, unique=True, editable=False, null=True, blank=True)
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name='proposals')
    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='proposals')
    created_by = models.ForeignKey(Users, on_delete=models.PROTECT, related_name='created_proposals', null=True,
                                   blank=True)

    title = models.CharField(max_length=200, null=True, blank=True)
    date = models.DateField(default=timezone.now, null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD')

    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), null=True,
                                          blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('20.00'), null=True, blank=True)

    notes = models.TextField(null=True, blank=True)
    private_notes = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)

    class Meta:
        db_table = 'Proposals'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['proposal_no']),
            models.Index(fields=['status']),
            models.Index(fields=['lead']),
            models.Index(fields=['company']),
        ]

    def __str__(self):
        return f"{self.proposal_no} - {self.title}"

    def save(self, *args, **kwargs):
        if not self.proposal_no:
            date_str = timezone.now().strftime('%Y%m%d')
            uid_chunk = str(uuid.uuid4())[:4].upper()
            self.proposal_no = f"T-{date_str}-{uid_chunk}"
        super().save(*args, **kwargs)

    @cached_property
    def subtotal(self):
        """Ara Toplam: Tüm kalemlerin indirimsiz toplamı."""
        return Decimal(sum(item.total_price for item in self.items.all()))

    @property
    def discount_value(self):
        """Uygulanan iskonto tutarı (negatif olmaz)."""
        return max(self.discount_amount or Decimal('0.00'), Decimal('0.00'))

    @property
    def discounted_subtotal(self):
        """İskontolu Toplam: Ara Toplam – İskonto."""
        return max(self.subtotal - self.discount_value, Decimal('0.00'))

    @property
    def tax_amount(self):
        """KDV Tutarı: İskontolu toplam üzerinden hesaplanır."""
        rate = self.tax_rate or Decimal('0.00')
        tax = self.discounted_subtotal * (rate / Decimal('100.00'))
        return tax.quantize(Decimal('0.01'))

    @property
    def grand_total(self):
        """Genel Toplam: İskontolu Toplam + KDV."""
        return self.discounted_subtotal + self.tax_amount


class ProposalItems(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(Proposals, on_delete=models.CASCADE, related_name='items')

    package = models.ForeignKey('packages.Packages', on_delete=models.SET_NULL, null=True, blank=True)
    module = models.ForeignKey(
        'packages.SaaSModule', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='proposal_items', verbose_name='İlgili Modül'
    )
    device = models.ForeignKey('devices.Device', on_delete=models.SET_NULL, null=True, blank=True)

    # YENİ EKLENEN ALAN: Teklif satırının hangi mağaza için olduğu (manuel metin)
    store_name = models.CharField(max_length=100, null=True, blank=True, verbose_name='Mağaza/Şube Adı')

    description = models.CharField(max_length=255, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), null=True, blank=True)

    maintenance_included = models.BooleanField(default=False, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ProposalItems'
        ordering = ['created_at']

    @property
    def total_price(self):
        qty = self.quantity or 0
        price = self.unit_price or Decimal('0.00')
        return qty * price

    def save(self, *args, **kwargs):
        # Eğer açıklama boşsa ve Paket, Modül veya Cihaz seçildiyse otomatik doldur
        if not self.description:
            if self.module:
                self.description = f"{self.module.name} — Lisans Bedeli"
                if not self.unit_price:
                    self.unit_price = self.module.license_price or Decimal('0.00')
            elif self.package:
                self.description = f"{self.package.name} Lisansı"
                if not self.unit_price:
                    self.unit_price = self.package.price_license or Decimal('0.00')
            elif self.device:
                self.description = f"{self.device.name} ({self.device.code})"
                if not self.unit_price:
                    self.unit_price = self.device.price or Decimal('0.00')

        super().save(*args, **kwargs)



class ProposalLogs(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(Proposals, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İşlemi Yapan")

    action = models.CharField(max_length=50, verbose_name="İşlem Tipi")  # Örn: Oluşturma, Güncelleme, Durum Değişimi
    description = models.TextField(verbose_name="Açıklama", null=True,
                                   blank=True)  # Örn: Durum Taslak -> Onaylandı olarak değişti.

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="İşlem Tarihi")

    class Meta:
        db_table = 'ProposalLogs'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.proposal.proposal_no} - {self.action}"