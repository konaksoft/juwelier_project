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
    REVERSAL = 'REVRS'  # FAZ 51 (R-02) — Append-only iptal işareti.
    TX_CHOICES = [
        (ENTRY, 'Giriş (Borç)'),
        (EXIT, 'Çıkış (Alacak)'),
        (REVERSAL, 'İptal (Reversal)'),
    ]

    # Çantacı İşlem Tipleri (sadece account_type=CANTACI olan tedarikçilerde anlamlıdır)
    class CantaciTxType(models.TextChoices):
        HURDA_VERILDI = 'HURDA_VERILDI', 'Hurda Verildi'
        URUN_ALINDI = 'URUN_ALINDI', 'Ürün Alındı'
        ODEME = 'ODEME', 'Ödeme'
        TAHSILAT = 'TAHSILAT', 'Tahsilat'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # ------------------------------------------------------------------
    # FAZ TS-1 (Tedarikçi Silme Güvenliği): on_delete=CASCADE -> SET_NULL
    # Tedarikçi silindiğinde cari geçmişin (audit trail) kaybolmaması
    # için CASCADE kaldırıldı. Silme akışında uygulama katmanı ayrıca
    # bu kayıtların is_active=False yapılmasını yönetir.
    # ------------------------------------------------------------------
    supplier = models.ForeignKey(Suppliers, on_delete=models.SET_NULL,
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
    exchange_rate_eur = models.DecimalField(
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
    # ------------------------------------------------------------------
    # FAZ 21 / Bug 2B: Satir-bazli iptal icin Process UUID referansi
    # ------------------------------------------------------------------
    # cancel_row PROC dalinda, ayni process_no altinda yer alan diger
    # SupplierLedger satirlarini etkilemeden yalnizca bu Process satirina
    # ait cariyi pasiflestirebilmek icin eklendi. Legacy kayitlarda NULL.
    # ------------------------------------------------------------------
    source_process_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Kaynak Process UUID",
        help_text=(
            "Bu cari satirini olusturan Process satirinin UUID'si. "
            "Coklu kalemli toptan seansda satir-bazli iptal icin kullanilir."
        ),
    )
    is_active = models.BooleanField(default=True)
    created_on = models.DateTimeField(auto_now_add=True)

    # ------------------------------------------------------------------
    # FAZ 51 (R-02): SupplierLedger Append-Only REVERSAL alanları.
    # ------------------------------------------------------------------
    # Mevcut akış orijinal satırı `is_active=False` ile pasifleştiriyor.
    # Yeni REVERSAL pattern bu davranışı KORUR (balance_summary aynı kalır)
    # fakat denetim için ayrı bir REVERSAL satırı yazar:
    #   - parent: orijinal satıra FK (ne iptal edildiği)
    #   - reversal_target_type: orijinal satırın tx_type'ı (audit kolaylığı)
    #   - reversed_by/reversed_at/reverse_reason: kim/ne zaman/neden
    #
    # REVERSAL satırı da `is_active=False` ile yazılır → balance_summary
    # toplama girmez → bakiye davranışı aynı, fakat denetim sorgusu
    # `transaction_type='REVRS'` ile iptal hareketlerini izleyebilir.
    # Eski (FAZ 51 öncesi) iptal kayıtlarında bu alanlar NULL'dur;
    # raporlar varsayılan olarak "audit yok" gösterir.
    # ------------------------------------------------------------------
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reversals',
        verbose_name="İptal Edilen Orijinal Satır",
    )
    reversal_target_type = models.CharField(
        max_length=5,
        null=True, blank=True,
        verbose_name="İptal Edilen Tip",
        help_text="Orijinal satırın transaction_type değeri (audit kolaylığı).",
    )
    reversed_by = models.ForeignKey(
        Users,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='supplier_ledger_reversals',
        verbose_name="İptal Eden",
    )
    reversed_at = models.DateTimeField(null=True, blank=True, verbose_name="İptal Zamanı")
    reverse_reason = models.CharField(
        max_length=255, null=True, blank=True,
        verbose_name="İptal Nedeni",
    )

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