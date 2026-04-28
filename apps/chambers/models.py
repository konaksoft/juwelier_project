import uuid
from django.db import models
from apps.stores.models import Company
from apps.definitions.locations.models import City, District, TaxOffice


class Chambers(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name='Oda/Dernek Adı')

    # İletişim Bilgileri
    email = models.CharField(max_length=150, blank=True, null=True)
    phone = models.CharField(max_length=30, blank=True, null=True)
    website = models.URLField(max_length=200, blank=True, null=True, verbose_name='Web Sitesi')

    # Adres Bilgileri (Lokasyon modellerine bağlandı)
    address = models.TextField(verbose_name='Adres', null=True, blank=True)
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True, related_name='chambers',
                             verbose_name='İl')
    district = models.ForeignKey(District, on_delete=models.SET_NULL, null=True, blank=True, related_name='chambers',
                                 verbose_name='İlçe')

    # Resmi & Finansal Bilgiler
    tax_office = models.ForeignKey(TaxOffice, on_delete=models.SET_NULL, null=True, blank=True, related_name='chambers',
                                   verbose_name='Vergi Dairesi')
    tax_number = models.CharField(max_length=50, blank=True, null=True, verbose_name='Vergi Numarası')
    registry_number = models.CharField(max_length=100, blank=True, null=True,
                                       verbose_name='Dernek Kütük / Oda Sicil No')

    # Yetkili Bilgisi (metin)
    president_name = models.CharField(max_length=150, blank=True, null=True, verbose_name='Oda/Dernek Başkanı')

    # Yetkili Kullanıcı Bağlantısı (login ve yetkilendirme için)
    president_user = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='presided_chambers',
        verbose_name='Başkan Kullanıcı Hesabı',
    )

    # FİRMA BAĞLANTISI
    companies = models.ManyToManyField(Company, related_name='chambers', blank=True, verbose_name='Üye Firmalar')

    description = models.TextField(null=True, blank=True, verbose_name='Açıklama')

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Chambers'
        indexes = [
            models.Index(fields=['is_active']),
            models.Index(fields=['name']),
            models.Index(fields=['president_user'], name='chambers_president_idx'),
        ]

    def __str__(self):
        return self.name


class ChamberProductPrice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chamber = models.ForeignKey(Chambers, on_delete=models.CASCADE, related_name='product_prices')
    product = models.ForeignKey('products.Products', on_delete=models.CASCADE, related_name='chamber_prices')

    buy_price_hs = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True, verbose_name="Dernek Alış Has"
    )
    sale_price_hs = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True, verbose_name="Dernek Satış Has"
    )
    fixed_labor_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Dernek Sabit İşçilik"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ChamberProductPrices'
        unique_together = ('chamber', 'product')

    def __str__(self):
        return f"{self.chamber.name} - {self.product.name} Fiyatı"
