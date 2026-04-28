# apps/definitions/contracts/models.py
from django.urls import reverse
from django.db import models
import uuid
from apps.stores.models import Company, Stores
from apps.crm.packages.models import Packages


# Contracts modeliniz aynı kalabilir...
class Contracts(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name="Sözleşme Adı", null=True, blank=True)
    description = models.TextField(verbose_name="Sözleşme İçeriği/Açıklama", null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Contracts'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class ContractProcess(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Onay Bekliyor'),
        ('SIGNED', 'İmzalandı'),
        ('EXPIRED', 'Süresi Doldu'),
        ('REJECTED', 'Reddedildi'),
    ]
    PROCESS_TYPE_CHOICES = [('SALES', 'Satış'), ('DEMO', 'Demo')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    package = models.ForeignKey(Packages, on_delete=models.SET_NULL, null=True, blank=True)
    contract = models.ForeignKey(Contracts, on_delete=models.SET_NULL, null=True, blank=True,
                                 verbose_name="İlgili Sözleşme")
    content_snapshot = models.TextField(verbose_name="Sözleşme Metni (Snapshot)", blank=True, null=True)

    # Sadece Görsel İmza (Yeterli)
    signature_image = models.ImageField(upload_to='contract_signatures/%Y/%m/', null=True, blank=True,
                                        verbose_name="İmza Görseli")
    document_hash = models.CharField(max_length=64, null=True, blank=True, verbose_name="Dosya Hash (SHA256)")

    proposal = models.ForeignKey(
        'proposals.Proposals',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='contract_processes',
        verbose_name="İlgili Teklif"
    )

    process_type = models.CharField(max_length=10, choices=PROCESS_TYPE_CHOICES, default='SALES')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')

    # --- GÜVENLİK VE DELİL ALANLARI ---
    signer_phone = models.CharField(max_length=20, null=True, blank=True, verbose_name="İmzalayan Telefon")
    verification_code = models.CharField(max_length=6, null=True, blank=True, verbose_name="SMS Kodu")

    # SMS Sağlayıcı Kanıtı
    sms_uuid = models.CharField(max_length=100, null=True, blank=True, verbose_name="Netgsm JobID")

    # İmzalama Anı Verileri
    signed_at = models.DateTimeField(null=True, blank=True, verbose_name="İmzalama Tarihi")
    signer_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="İmzalayan IP")
    signer_user_agent = models.TextField(null=True, blank=True, verbose_name="Tarayıcı/Cihaz Bilgisi")

    # İmzalanan Metnin Birebir Kopyası (Değiştirilemez Kanıt)
    signed_content_snapshot = models.TextField(null=True, blank=True, verbose_name="İmzalanan Metin Kopyası")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ContractProcesses'

    @property
    def url(self):
        # 'contracts:public_view' sizin urls.py'daki isminiz olmalı
        return reverse('contracts:public_view', args=[str(self.token)])
