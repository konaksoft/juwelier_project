import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Sum
from django.db.models.functions import Coalesce
from apps.accounts.models import Users
from apps.stores.models import Stores
from apps.products.models import Products

# Locations uygulamasından modelleri import ediyoruz
# Not: Uygulama ismin 'apps.definitions.locations' ise yolu ona göre düzenle
from apps.definitions.locations.models import *


# ============================================================================
# FAZ B: LEDGER PARA BIRIMI STANDARDIZASYON ENUM'U
# ============================================================================

class LedgerCurrencyChoices(models.TextChoices):
    """
    SupplierLedger ve CustomerLedger kayıtlarında kullanılan para birimlerinin
    kod standardizasyonu.

    ÖNEMLİ - Zero Migration Risk Prensibi:
        Bu enum ŞUAN SupplierLedger.currency alanına choices=... olarak
        BAĞLANMAMIŞTIR. SupplierLedger.currency veritabanında serbest bir
        CharField olarak kalmaya devam etmektedir.

        Veri bütünlüğü FAZ B servis katmanı (get_ledger_currency, StockService
        validasyonları) tarafından sağlanır. Böylece mevcut kayıtlar
        hiçbir migration/validation hatası yaşamaz.

    Metal Bazlı Birimler:
        HS : Has Altın (24 ayar gram). Ledger'da gram cinsinden tutulur.
        HG : Has Gümüş (999/925 gram). Ledger'da gram cinsinden tutulur.

    Fiat (Para) Birimleri:
        TRY, USD, EUR, GBP, CAD, QAR

    Kullanım:
        from apps.suppliers.services import get_ledger_currency
        currency = get_ledger_currency(product, fiat_currency='TRY')
    """
    HS  = 'HS',  'Has Altın'
    HG  = 'HG',  'Has Gümüş'
    TRY = 'TRY', 'Türk Lirası'
    USD = 'USD', 'Amerikan Doları'
    EUR = 'EUR', 'Euro'
    GBP = 'GBP', 'İngiliz Sterlini'
    CAD = 'CAD', 'Kanada Doları'
    QAR = 'QAR', 'Katar Riyali'


class Suppliers(models.Model):
    # Mükellefiyet Türü (Fatura keserken VKN mi TCKN mi kullanılacağını belirler)
    class TaxPayerType(models.TextChoices):
        INDIVIDUAL = 'INDIVIDUAL', 'Şahıs / Bireysel'
        CORPORATE = 'CORPORATE', 'Tüzel Kişi / Şirket'

    # Hesap Türü: Tedarikçi (klasik) veya Çantacı
    class AccountType(models.TextChoices):
        SUPPLIER = 'SUPPLIER', 'Tedarikçi'
        CANTACI = 'CANTACI', 'Çantacı'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Temel Bilgiler ---
    company_name = models.CharField(max_length=255, verbose_name="Firma Ünvanı")
    person_name = models.CharField(max_length=100, blank=True, null=True, verbose_name="Yetkili Adı")
    person_surname = models.CharField(max_length=100, blank=True, null=True, verbose_name="Yetkili Soyadı")

    # --- İletişim ---
    email = models.EmailField(max_length=100, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)

    # --- Vergi Bilgileri ---
    tax_payer_type = models.CharField(
        max_length=20,
        choices=TaxPayerType.choices,
        default=TaxPayerType.CORPORATE,
        verbose_name="Mükellefiyet Türü"
    )
    tax_office = models.ForeignKey(
        TaxOffice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Vergi Dairesi",
        related_name="suppliers"
    )
    tax_number = models.CharField(max_length=11, blank=True, null=True, verbose_name="VKN / TCKN")
    is_einvoice_user = models.BooleanField(default=False, verbose_name="E-Fatura Mükellefi")


    city = models.ForeignKey(
        City,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="İl"
    )
    district = models.ForeignKey(
        District,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="İlçe"
    )
    post_code = models.CharField(max_length=10, blank=True, null=True, verbose_name="Posta Kodu")
    company_address = models.TextField(blank=True, null=True, verbose_name="Açık Adres (Cadde, Sokak, Kapı No)")

    # --- Hesap Türü (Tedarikçi / Çantacı) ---
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        null=True,
        blank=True,
        verbose_name="Hesap Türü",
        help_text="NULL veya SUPPLIER = Tedarikçi, CANTACI = Çantacı"
    )

    # --- Sistem Bilgileri ---
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    def balance_summary(self) -> dict[str, dict[str, Decimal]]:
        """
        Aktif ledger satırlarını özetler.
        """
        rows = (self.ledgers
                .filter(is_active=True)
                .values('currency', 'transaction_type')
                .annotate(total=Sum('amount_value')))

        out: dict[str, dict[str, Decimal]] = {}
        for r in rows:
            cur = (r['currency'] or 'HS').upper()
            bkt = out.setdefault(cur, {
                'receivable': Decimal('0'),
                'payable': Decimal('0'),
                'net': Decimal('0'),
            })
            if r['transaction_type'] == 'EXIT':  # bizim ALACAĞIMIZ
                bkt['receivable'] += r['total']
            else:  # bizim BORCUMUZ
                bkt['payable'] += r['total']
            bkt['net'] = bkt['receivable'] - bkt['payable']
        return out

    def get_balance(self):
        hs = self.balance_summary().get('HS', {'net': Decimal('0')})['net']
        return hs

    @property
    def full_location_string(self):
        """Örn: İstanbul / Kadıköy"""
        parts = []
        if self.city: parts.append(self.city.name)
        if self.district: parts.append(self.district.name)
        return " / ".join(parts) if parts else "-"

    def __str__(self):
        return f'{self.company_name}'

    class Meta:
        db_table = 'Suppliers'
        ordering = ['company_name']


class SupplierLedger(models.Model):
    ENTRY = 'ENTRY'
    EXIT = 'EXIT'
    TX_CHOICES = [(ENTRY, 'Giriş (Borç)'), (EXIT, 'Çıkış (Alacak)')]

    # Çantacı İşlem Tipleri (sadece account_type=CANTACI olan tedarikçilerde anlamlıdır)
    class CantaciTxType(models.TextChoices):
        HURDA_VERILDI = 'HURDA_VERILDI', 'Hurda Verildi'
        URUN_ALINDI = 'URUN_ALINDI', 'Ürün Alındı'
        ODEME = 'ODEME', 'Ödeme'
        TAHSILAT = 'TAHSILAT', 'Tahsilat'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supplier = models.ForeignKey(Suppliers, on_delete=models.CASCADE,
                                 related_name='ledgers', null=True, blank=True)
    product = models.ForeignKey(Products, on_delete=models.SET_NULL,
                                null=True, blank=True)
    transaction_type = models.CharField(max_length=5, choices=TX_CHOICES)
    cantaci_tx_type = models.CharField(
        max_length=20,
        choices=CantaciTxType.choices,
        null=True,
        blank=True,
        verbose_name="Çantacı İşlem Tipi",
        help_text="Çantacı hesaplarında işlem alt tipini belirtir"
    )
    quantity_piece = models.IntegerField(default=0)
    quantity_gram = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    description = models.TextField(null=True, blank=True)
    amount_value = models.DecimalField(max_digits=14, decimal_places=3)
    currency = models.CharField(max_length=50)
    # ------------------------------------------------------------------
    # FAZ B2 / ONARIM FAZI 2: Dondurulmus Kur (Frozen Exchange Rate)
    # ------------------------------------------------------------------
    # Islem aninda donduruldugu kur. Ornek: 1 USD = 45.50 TL.
    # Iptal/geri sarma akisinda bu deger korunarak kur farkindan
    # dogan muhasebe dengesizligi onlenir.
    # NULL izni: Eski kayitlar ve HS/HG metal birimli kayitlar icin
    # bu alan dolu olmayabilir; uygulama katmani fallback uygular.
    # ------------------------------------------------------------------
    exchange_rate_tl = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        null=True,
        blank=True,
        verbose_name="İşlem Anındaki Kur (TL Karşılığı)",
        help_text=(
            "İşlem anında 1 birim currency karşılığı kaç TL olduğunu saklar "
            "(örn. 1 USD = 45.50 TL için 45.500000). Fiat para birimli "
            "ürünlerde iptal/reversal sırasında korunur. HS/HG (metal) "
            "ledger kayıtlarında NULL bırakılabilir."
        ),
    )
    process_no = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'SupplierLedgers'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['supplier', 'currency', 'transaction_type', 'is_active']),
            models.Index(fields=['process_no']),
            models.Index(fields=['created_on']),
        ]

    def save(self, *args, **kwargs):
        if not self.currency:
            self.currency = (self.product.currency if self.product and self.product.currency else 'HS')
        # para kodunu standartlaştır
        self.currency = (self.currency or 'HS').upper()
        super().save(*args, **kwargs)