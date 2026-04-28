from django.db import models
from django.utils import timezone
import uuid

from apps.accounts.models import *
from apps.crm.packages.models import Packages, SaaSModule

# Not: Decimal ve Validators importlarına artık bu dosyada ihtiyaç kalmadı
# çünkü price_margin_percent alanını settings uygulamasına taşıdık.

class Company(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, null=True, blank=True, verbose_name='Ticari Unvan / Ad Soyad')

    email = models.CharField(max_length=150, blank=True, null=True)
    phone = models.CharField(max_length=30, blank=True, null=True)
    address = models.TextField(verbose_name='Adres', null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True, verbose_name='İl')
    district = models.CharField(max_length=100, null=True, blank=True, verbose_name='İlçe')
    postal_code = models.CharField(max_length=10, null=True, blank=True, verbose_name='Posta Kodu')
    country = models.CharField(max_length=50, default='Türkiye')

    tax_office = models.CharField(max_length=100, null=True, blank=True, verbose_name='Vergi Dairesi')
    tax_office_code = models.CharField(max_length=10, null=True, blank=True, verbose_name='Vergi Dairesi Kodu')
    tax_number = models.CharField(max_length=45, null=True, blank=True, unique=True, verbose_name='VKN / TCKN')
    mersis_no = models.CharField(max_length=20, null=True, blank=True, verbose_name='MERSİS No')
    trade_registry_no = models.CharField(max_length=30, null=True, blank=True, verbose_name='Ticaret Sicil No')

    iban = models.CharField(max_length=34, null=True, blank=True, verbose_name='IBAN')
    e_invoice_type = models.CharField(
        max_length=10,
        choices=[('TEMEL', 'Temel Fatura'), ('TICARI', 'Ticari Fatura')],
        default='TEMEL',
        verbose_name='E-Fatura Senaryo Tipi'
    )

    avatar = models.ImageField(upload_to='Companies/', null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Companies'
        indexes = [
            models.Index(fields=['is_active']),
            models.Index(fields=['tax_number']),
            models.Index(fields=['tax_office_code']),
        ]

    def __str__(self):
        return self.title or "Firma"


class Stores(models.Model):
    # ------------------------------------------------------------------
    # FAZ 19 — Mağaza Yaşam Döngüsü (Hızlı Onboarding / Fast-Track)
    #   Mevcut is_active boolean'ı KORUNUR; status alanı onun üstüne
    #   anlamsal bir katman ekler. İki alan birlikte okunur.
    #
    #   DEMO            → Hızlı Kayıt ile açılmış, süreli demo mağaza
    #   PENDING_PAYMENT → Teklif onaylandı, ödeme/sözleşme bekleniyor
    #   ACTIVE          → Gerçek müşteri, aktif abonelik
    #   EXPIRED         → Demo süresi doldu veya abonelik bitti
    #   SUSPENDED       → Konasoft tarafından manuel askıya alındı
    # ------------------------------------------------------------------
    STATUS_CHOICES = [
        ('DEMO',            'Demo'),
        ('PENDING_PAYMENT', 'Ödeme Bekleniyor'),
        ('ACTIVE',          'Aktif'),
        ('EXPIRED',         'Süresi Doldu'),
        ('SUSPENDED',       'Askıya Alındı'),
    ]

    ONBOARDING_SOURCE_CHOICES = [
        ('corporate',  'Kurumsal (Teklif)'),
        ('fast_track', 'Hızlı Kayıt (Demo)'),
        ('manual',     'Manuel (Admin)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store_id = models.CharField(max_length=25, unique=True, editable=False, blank=True, null=True,
                                verbose_name='Mağaza Kimlik No')
    masak_public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='stores', null=True, blank=True)
    package = models.ForeignKey(Packages, on_delete=models.SET_NULL, null=True, blank=True)

    # İletişim Doğrulama Durumları (Statü olduğu için burada kalabilir)
    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)

    # --- SİLİNEN ALANLAR (Settings Uygulamasına Taşındı) ---
    # use_average_labor -> apps/settings/models.py
    # apply_masak_rules -> apps/settings/models.py
    # price_margin_percent -> apps/settings/models.py
    # -------------------------------------------------------

    email = models.CharField(max_length=150, blank=True, null=True)
    phone = models.CharField(max_length=30, blank=True, null=True)
    address = models.TextField(verbose_name='Adres', null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True, verbose_name='İl')
    district = models.CharField(max_length=100, null=True, blank=True, verbose_name='İlçe')
    postal_code = models.CharField(max_length=10, null=True, blank=True, verbose_name='Posta Kodu')
    country = models.CharField(max_length=50, default='Türkiye')

    title = models.CharField(max_length=255, null=True, blank=True, verbose_name='Mağaza Adı / Ekran Adı')
    barcode_title = models.CharField(max_length=255, null=True, blank=True, verbose_name='Barkod Adı')

    avatar = models.ImageField(default='default/store.png', upload_to='Stores/', null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    subscription_start = models.DateField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    # --- FAZ 19: Mağaza Yaşam Döngüsü Alanları ---
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='ACTIVE',
        db_index=True,
        verbose_name='Mağaza Statüsü',
        help_text='Mağazanın yaşam döngüsündeki güncel durumu. is_active boolean\'ı ile birlikte okunur.'
    )
    demo_expires_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Demo Bitiş Zamanı',
        help_text='Yalnızca status=DEMO için doludur. Süre dolduğunda otomatik EXPIRED olur.'
    )
    demo_converted_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Demo→Aktif Dönüşüm Zamanı',
        help_text='Demo mağazanın ücretli pakete geçtiği an damgalanır.'
    )
    onboarding_source = models.CharField(
        max_length=20,
        choices=ONBOARDING_SOURCE_CHOICES,
        default='manual',
        verbose_name='Kayıt Kaynağı',
        help_text='Bu mağaza hangi akışla sisteme dahil oldu?'
    )

    class Meta:
        db_table = 'Stores'
        indexes = [
            models.Index(fields=['company', 'is_active']),
            models.Index(fields=['status']),
            models.Index(fields=['status', 'demo_expires_at']),
        ]

    def __str__(self):
        return self.title or self.store_id or "Mağaza"

    # ------------------------------------------------------------------
    # FAZ 19 — Yardımcı Property'ler
    # ------------------------------------------------------------------
    @property
    def is_demo(self) -> bool:
        """Mağaza demo modunda mı?"""
        return self.status == 'DEMO'

    @property
    def demo_days_remaining(self):
        """Demo bitişine kalan gün sayısı (negatifse süre dolmuş)."""
        if not self.demo_expires_at:
            return None
        delta = self.demo_expires_at - timezone.now()
        return delta.days

    @property
    def is_demo_expired(self) -> bool:
        """Demo süresi dolmuş mu? (Cron henüz EXPIRED'e çevirmemiş olabilir.)"""
        if self.status != 'DEMO' or not self.demo_expires_at:
            return False
        return self.demo_expires_at < timezone.now()


class StoreModule(models.Model):
    """
    Store ↔ SaaSModule köprü tablosu.

    Bir mağazaya paket dışı ek modüller atanabilir. Mağazanın efektif
    yetki havuzu şu şekilde hesaplanır:

        Efektif Yetkiler = Paket Yetkileri (PackagePermissionMatrix)
                           ∪ Store Modül Yetkileri (StoreModule → permission'lar)

    Bu model Faz 12.3 kapsamında oluşturulmuştur.

    Kullanım örneği:
        - Mağaza "Silver" paketindedir ama ek olarak "Perakende" modülünü
          satın almıştır → StoreModule(store=mağaza, module=perakende)
        - get_store_effective_permission_ids(store) çağrıldığında hem paketin
          hem de ek modüllerin yetkileri birleşik döner.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Stores, on_delete=models.CASCADE, related_name='store_modules',
        verbose_name="Mağaza"
    )
    module = models.ForeignKey(
        SaaSModule, on_delete=models.CASCADE, related_name='in_stores',
        verbose_name="Modül"
    )
    activated_at = models.DateTimeField(auto_now_add=True, verbose_name="Aktivasyon Tarihi")
    note = models.CharField(max_length=200, blank=True, verbose_name="Not",
                            help_text="Ek modül atamasına dair açıklama.")

    class Meta:
        db_table = 'StoreModules'
        unique_together = ('store', 'module')
        verbose_name = 'Mağaza Modülü'
        verbose_name_plural = 'Mağaza Modülleri'

    def __str__(self):
        store_label = self.store.title or self.store.store_id or str(self.store.id)
        return f"{store_label} → {self.module.name}"


# Fiyat Cache (Değişmedi)
from decimal import Decimal  # Sadece burası için gerekiyorsa tekrar eklenebilir veya yukarıda tutulabilir
class StorePriceCache(models.Model):
    store = models.OneToOneField('Stores', on_delete=models.CASCADE, related_name='price_cache')
    has_buy_tl = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    has_sale_tl = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'StorePriceCache'
