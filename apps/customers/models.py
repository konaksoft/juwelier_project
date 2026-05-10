import uuid
from django.db import models
from django.db.models import Sum, Q, Case, When, F, DecimalField
from django.db.models.functions import Coalesce
from apps.stores.models import Stores
from apps.definitions.locations.models import City, District, TaxOffice
from decimal import Decimal


class Customers(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ManyToManyField(Stores, related_name='customers', blank=True)
    first_name = models.CharField(max_length=100, blank=False, null=False)
    last_name = models.CharField(max_length=100, blank=False, null=False)
    identification_number = models.CharField(max_length=11, null=True, blank=True)
    customer_number = models.CharField(max_length=25, null=True, blank=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    gender = models.CharField(max_length=15, blank=True, null=True)
    email = models.EmailField(max_length=255, blank=True, null=True)

    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İl")
    district = models.ForeignKey(District, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İlçe")
    tax_office = models.ForeignKey(TaxOffice, on_delete=models.SET_NULL, null=True, blank=True,
                                   verbose_name="Vergi Dairesi")

    tax_office_code = models.CharField(max_length=10, null=True, blank=True, verbose_name="Vergi Dairesi Kodu")
    address = models.TextField(blank=True, null=True)

    identification_front_image = models.ImageField(upload_to='customers/identity/', null=True, blank=True)
    identification_back_image = models.ImageField(upload_to='customers/identity/', null=True, blank=True)

    # NOT: receivable_hs / payable_hs alanları geriye uyum için korunuyor — artık
    # doğrudan mutate edilmez; gerçek bakiye `balance_hs` property üzerinden
    # CustomerLedger üzerinden hesaplanır. (R-Faz 4 + Cari/Emanet Refactor)
    receivable_hs = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    payable_hs = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))

    is_email_verified = models.BooleanField(default=False, null=True, blank=True)
    is_phone_verified = models.BooleanField(default=False, null=True, blank=True)

    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    # ─────────────────────────────────────────────────────────────
    # BAKİYE HESAPLAMA — append-only Ledger üzerinden
    # ─────────────────────────────────────────────────────────────
    #
    # Cari/Emanet Refactor:
    #   Borç tarafı (müşterinin mağazaya borcu artar):
    #     DEBT
    #   Alacak/Kapatma tarafı (borcu azaltır):
    #     CREDIT, COLLECTION_TL, COLLECTION_FX, COLLECTION_HS,
    #     FX_GAIN, DISCOUNT, WRITEOFF, CUSTODY_OFFSET
    #   İptal/düzeltme:
    #     REVERSAL  (parent kaydın işaretini ters çevirir; karşı giriş)
    #     CORRECTION (sistem aktarım düzeltmesi, +/- işareti
    #                 amount_hs'in işaretiyle gelir)
    #
    # Geriye uyum:
    #   `is_active=False` ile pasifleştirilmiş eski kayıtlar bakiyeye dahil
    #   edilmez (mevcut cancel_row / cancel_group akışını bozmamak için).
    # ─────────────────────────────────────────────────────────────

    DEBT_INCREASING_TYPES = ('DEBT',)
    DEBT_DECREASING_TYPES = (
        'CREDIT',
        'COLLECTION_TL', 'COLLECTION_FX', 'COLLECTION_HS',
        'FX_GAIN', 'DISCOUNT', 'WRITEOFF',
        'CUSTODY_OFFSET',
    )

    def _ledger_aggregate(self):
        """Aktif (onaylı + pasifleştirilmemiş) ledger satırlarını topla."""
        base_qs = self.ledger_entries.filter(is_active=True, is_approved=True)

        agg = base_qs.aggregate(
            total_debt=Coalesce(
                Sum('amount_hs', filter=Q(transaction_type__in=self.DEBT_INCREASING_TYPES)),
                Decimal('0'),
            ),
            total_credit=Coalesce(
                Sum('amount_hs', filter=Q(transaction_type__in=self.DEBT_DECREASING_TYPES)),
                Decimal('0'),
            ),
            # REVERSAL: işareti ters; amount_hs daima pozitif yazılır,
            # parent kaydın tipine göre net etki hesaplanır.
            reversal_debt=Coalesce(
                Sum(
                    'amount_hs',
                    filter=Q(transaction_type='REVERSAL',
                             reversal_target_type__in=self.DEBT_INCREASING_TYPES),
                ),
                Decimal('0'),
            ),
            reversal_credit=Coalesce(
                Sum(
                    'amount_hs',
                    filter=Q(transaction_type='REVERSAL',
                             reversal_target_type__in=self.DEBT_DECREASING_TYPES),
                ),
                Decimal('0'),
            ),
            correction=Coalesce(
                Sum('amount_hs_signed', filter=Q(transaction_type='CORRECTION')),
                Decimal('0'),
            ),
        )

        debt = (agg['total_debt'] or Decimal('0')) - (agg['reversal_debt'] or Decimal('0'))
        credit = (agg['total_credit'] or Decimal('0')) - (agg['reversal_credit'] or Decimal('0'))
        correction = agg['correction'] or Decimal('0')
        return debt, credit, correction

    @property
    def balance_hs(self):
        """Net Has bakiyesi.
        Pozitif → müşteri mağazaya borçlu (alacak)
        Negatif → mağaza müşteriye borçlu (verecek)
        """
        debt, credit, correction = self._ledger_aggregate()
        return debt - credit + correction

    # ─────────────────────────────────────────────────────────────
    # FAZ 33.3 — TL bakiyesi STORED amount_eur üzerinden hesaplanır
    # ─────────────────────────────────────────────────────────────
    # Eskiden balance_eur = balance_hs × get_current_has_rate(store)
    # şeklinde anlık piyasa kuruyla hesaplanıyordu. Bu, satış anında
    # SATIŞ kuruyla yazılan borçların görüntülemede ALIŞ kuruyla
    # tekrar TL'ye çevrilmesi → spread farkı görüntü sapması.
    #
    # Doğru mimari: Her CustomerLedger satırı `amount_eur` alanını
    # yazıldığı andaki kurla saklar (append-only, tarihi sabit).
    # Bakiye TL = Σ signed amount_eur. Borç yazıldığı andaki TL
    # korunur, kur dalgalanması cari görünümünü etkilemez.
    #
    # Hesap kuralı (HS bakiyesiyle simetrik):
    #   DEBT_INCREASING_TYPES → +amount_eur
    #   DEBT_DECREASING_TYPES → -amount_eur
    #   REVERSAL              → parent tipine göre ters sign
    #   CORRECTION            → amount_hs_signed işaretine göre +/-amount_eur
    # ─────────────────────────────────────────────────────────────

    def _ledger_aggregate_eur(self):
        """Aktif onaylı ledger satırlarından TL bakiye toplamı.
        balance_hs ile aynı tip kümeleri ve REVERSAL/CORRECTION mantığı."""
        base_qs = self.ledger_entries.filter(is_active=True, is_approved=True)

        _tl_field = DecimalField(max_digits=14, decimal_places=2)

        agg = base_qs.aggregate(
            total_debt_eur=Coalesce(
                Sum('amount_eur', filter=Q(transaction_type__in=self.DEBT_INCREASING_TYPES)),
                Decimal('0'),
                output_field=_tl_field,
            ),
            total_credit_eur=Coalesce(
                Sum('amount_eur', filter=Q(transaction_type__in=self.DEBT_DECREASING_TYPES)),
                Decimal('0'),
                output_field=_tl_field,
            ),
            reversal_debt_eur=Coalesce(
                Sum(
                    'amount_eur',
                    filter=Q(transaction_type='REVERSAL',
                             reversal_target_type__in=self.DEBT_INCREASING_TYPES),
                ),
                Decimal('0'),
                output_field=_tl_field,
            ),
            reversal_credit_eur=Coalesce(
                Sum(
                    'amount_eur',
                    filter=Q(transaction_type='REVERSAL',
                             reversal_target_type__in=self.DEBT_DECREASING_TYPES),
                ),
                Decimal('0'),
                output_field=_tl_field,
            ),
            # CORRECTION: amount_hs_signed > 0 → debt artırıcı (+amount_eur)
            #             amount_hs_signed < 0 → debt azaltıcı (-amount_eur)
            correction_eur=Coalesce(
                Sum(
                    Case(
                        When(
                            transaction_type='CORRECTION',
                            amount_hs_signed__gt=0,
                            then=F('amount_eur'),
                        ),
                        When(
                            transaction_type='CORRECTION',
                            amount_hs_signed__lt=0,
                            then=-F('amount_eur'),
                        ),
                        default=Decimal('0'),
                        output_field=_tl_field,
                    ),
                ),
                Decimal('0'),
                output_field=_tl_field,
            ),
        )

        debt_eur = (agg['total_debt_eur'] or Decimal('0')) - (agg['reversal_debt_eur'] or Decimal('0'))
        credit_eur = (agg['total_credit_eur'] or Decimal('0')) - (agg['reversal_credit_eur'] or Decimal('0'))
        correction_eur = agg['correction_eur'] or Decimal('0')
        return debt_eur, credit_eur, correction_eur

    @property
    def balance_eur(self):
        """Net TL bakiyesi (stored — borç yazıldığı andaki kur).

        FAZ 33.3 SSOT: Σ signed amount_eur. Anlık piyasa kuru
        kullanılmaz; her satır kendi `exchange_rate_eur` alanıyla
        zaten doğru TL'sini saklar. Kur dalgalanması cari ekrana
        yansımaz → "satıştaki TL = cari TL = tahsilattaki TL"
        garantisi.
        """
        debt_eur, credit_eur, correction_eur = self._ledger_aggregate_eur()
        return (debt_eur - credit_eur + correction_eur).quantize(Decimal('0.01'))

    @property
    def effective_rate_tl(self):
        """Bakiyenin efektif TL/HS kuru (stored TL'den türetilir).

        balance_eur / balance_hs → açık borçların ortalama yazım
        kuru. Bu kur tahsilat ekranında "kullanıcının girdiği TL'i
        kaç HS'ye çevireyim?" sorusuna doğru cevabı verir →
        sahte overpayment elimine edilir.

        Bakiye sıfır ya da yön farklı (signed) durumda None döner;
        caller anlık piyasa kuruna fallback edebilir.
        """
        b_hs = self.balance_hs
        if b_hs == 0:
            return None
        b_tl = self.balance_eur
        if b_tl == 0:
            return None
        # Aynı işaretli olmalılar; ters yönlü ise stored TL kuru
        # tutarsızdır (geçmiş veri korumalı), None dön.
        if (b_hs > 0) != (b_tl > 0):
            return None
        rate = (b_tl / b_hs).quantize(Decimal('0.000001'))
        return rate if rate > 0 else None

    @property
    def receivable_hs_computed(self):
        b = self.balance_hs
        return b if b > 0 else Decimal('0')

    @property
    def payable_hs_computed(self):
        b = self.balance_hs
        return -b if b < 0 else Decimal('0')

    class Meta:
        db_table = 'Customers'
        indexes = [
            models.Index(fields=['phone'], name='customers_phone_idx'),
            models.Index(fields=['identification_number'], name='customers_identification_idx'),
            models.Index(fields=['customer_number'], name='customers_customer_number_idx'),
            models.Index(fields=['is_deleted', 'is_active'], name='customers_deleted_active_idx'),
        ]


class CustomerLedger(models.Model):
    """Müşteri carisi — Append-Only Defter (Cari/Emanet Refactor).

    Tasarım kuralları:
      1) APPEND-ONLY: Bu tabloya UPDATE çekilmez. İptal/iade için
         `transaction_type='REVERSAL'` karşı girişi yazılır.
         (Geriye uyum: `is_active=False` mutation eski cancel_row /
         cancel_group akışında hâlâ destekleniyor; yeni kod yolu
         REVERSAL pattern'i kullanmalıdır.)

      2) ÇİFT BİRİM: Her satır hem Has (`amount_hs`) hem TL
         (`amount_eur`) cinsinden tutulur. `exchange_rate` o anki kurun
         tarihi (sabit) anlık kaydıdır — sonradan yeniden hesaplanmaz.

      3) TİPLENDİRME: `transaction_type` alanı sadece DEBT/CREDIT
         değil; tahsilat (COLLECTION_TL/FX/HS), kur farkı (FX_GAIN/
         FX_LOSS), iskonto (DISCOUNT), silme (WRITEOFF), emanet
         mahsuplaşma (CUSTODY_OFFSET), iptal (REVERSAL) ve sistem
         düzeltmesi (CORRECTION) tiplerini de destekler.

      4) ONAY ZİNCİRİ: Eşik üstü tipler (DISCOUNT, WRITEOFF, FX_GAIN/
         LOSS) `requires_approval=True` ile yazılır ve ApprovalService
         tarafından `is_approved=True` yapılana kadar bakiyeye dahil
         edilmez.

      5) AUDIT: `created_by`, `created_on`, `ip_address`, `user_agent`
         alanları her satıra yazılır. Onay zinciri ayrı alanlarda
         (`approved_by`, `approved_at`) saklanır.
    """

    # ── İşlem Tipleri ───────────────────────────────────────────────
    DEBT = 'DEBT'
    CREDIT = 'CREDIT'
    COLLECTION_TL = 'COLLECTION_TL'
    COLLECTION_FX = 'COLLECTION_FX'
    COLLECTION_HS = 'COLLECTION_HS'
    FX_GAIN = 'FX_GAIN'
    FX_LOSS = 'FX_LOSS'
    DISCOUNT = 'DISCOUNT'
    WRITEOFF = 'WRITEOFF'
    CUSTODY_OFFSET = 'CUSTODY_OFFSET'
    REVERSAL = 'REVERSAL'
    CORRECTION = 'CORRECTION'
    # FAZ 30 — Hızlı Onay Mimarisi:
    # Müşteri borçtan fazla ödeme yaptığında oluşan "kasa fazlası". Müşteri
    # bakiyesini etkilemez (debt-neutral); onaylandığında IncomeExpenseLedger
    # üzerinde OTHER_INCOME (Diğer Gelir) olarak yazılır.
    OVERPAYMENT = 'OVERPAYMENT'

    TRANSACTION_TYPES = [
        (DEBT, 'Borç (müşteri mağazaya borçlandı)'),
        (CREDIT, 'Alacak (mağaza müşteriye borçlandı)'),
        (COLLECTION_TL, 'TL Nakit Tahsilat'),
        (COLLECTION_FX, 'Döviz Tahsilat'),
        (COLLECTION_HS, 'Has (Altın) Tahsilat'),
        (FX_GAIN, 'Kur Farkı Zararı (mağaza zararı kabul)'),
        (FX_LOSS, 'Kur Farkı Karı (müşteri aleyhine fark)'),
        (DISCOUNT, 'Müşteri İskontosu'),
        (WRITEOFF, 'Şüpheli Alacak Silme'),
        (CUSTODY_OFFSET, 'Emanet Mahsuplaşması'),
        (REVERSAL, 'İptal Karşı Girişi'),
        (CORRECTION, 'Sistem Düzeltmesi'),
        (OVERPAYMENT, 'Kasa Fazlası (Fazla Tahsilat)'),
    ]

    # Onay zorunlu tipler (eşik servis katmanında belirlenir)
    APPROVAL_REQUIRED_TYPES = (
        FX_GAIN, FX_LOSS, DISCOUNT, WRITEOFF, REVERSAL, CORRECTION,
        OVERPAYMENT,
    )

    # Tahsilat ailesi (Payment kaydı zorunlu)
    COLLECTION_TYPES = (COLLECTION_TL, COLLECTION_FX, COLLECTION_HS)

    # ── Para Birimi ─────────────────────────────────────────────────
    CURRENCY_TRY = 'TRY'
    CURRENCY_USD = 'USD'
    CURRENCY_EUR = 'EUR'
    CURRENCY_GBP = 'GBP'
    CURRENCY_HS = 'HS'  # Has Altın

    CURRENCY_CHOICES = [
        (CURRENCY_TRY, 'Türk Lirası'),
        (CURRENCY_USD, 'US Dollar'),
        (CURRENCY_EUR, 'Euro'),
        (CURRENCY_GBP, 'British Pound'),
        (CURRENCY_HS, 'Has Altın'),
    ]

    # ── Alanlar ─────────────────────────────────────────────────────
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customers, on_delete=models.PROTECT, related_name='ledger_entries',
    )
    store = models.ForeignKey(
        Stores, on_delete=models.PROTECT, related_name='customer_ledger_entries',
        null=True, blank=True,
    )

    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)

    # ── Çift Birim ──────────────────────────────────────────────────
    # amount_hs daima POZİTİF tutulur. Yön (artırma/azaltma) tip
    # üzerinden okunur.
    amount_hs = models.DecimalField(
        max_digits=18, decimal_places=3, default=Decimal('0.000'),
        help_text='Has cinsinden tutar (daima pozitif)',
    )
    # amount_hs_signed: yalnız CORRECTION tipi için kullanılır,
    # düzeltmenin yönünü +/- ile saklar.
    amount_hs_signed = models.DecimalField(
        max_digits=18, decimal_places=3, default=Decimal('0.000'),
        help_text='İşaretli has tutar (yalnız CORRECTION için)',
    )
    amount_eur = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        help_text='İşlem anındaki TL karşılığı (tarihi, sabit)',
    )
    amount_fx = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        help_text='Döviz tahsilatlarda orijinal döviz tutarı',
    )
    exchange_rate_eur = models.DecimalField(
        max_digits=18, decimal_places=6, default=Decimal('0.000000'),
        help_text='1 gr Has = X TL (kaydedilme anındaki, sabit)',
    )
    fx_to_eur_rate = models.DecimalField(
        max_digits=18, decimal_places=6, default=Decimal('0.000000'),
        help_text='Döviz tahsilatlarda 1 birim döviz = X TL (anlık)',
    )
    currency = models.CharField(
        max_length=5, choices=CURRENCY_CHOICES, default=CURRENCY_TRY,
    )

    # ── Bağlantı / Referans ─────────────────────────────────────────
    process_no = models.CharField(max_length=100, blank=True, default='')
    parent = models.ForeignKey(
        'self', on_delete=models.PROTECT,
        null=True, blank=True, related_name='child_entries',
        help_text='REVERSAL/FX_GAIN/DISCOUNT için ana ledger satırı',
    )
    # REVERSAL kaydının iptal ettiği orijinal kaydın tipini ayrıca tutmak,
    # bakiye aggregate sorgularında gereksiz JOIN'i önler.
    reversal_target_type = models.CharField(
        max_length=20, blank=True, default='',
        help_text='REVERSAL kayıtları için: iptal edilen kaydın tipi',
    )
    related_payment = models.ForeignKey(
        'process.Payment', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='customer_ledger_entries',
        help_text='Tahsilat kayıtlarında kasaya giren Payment',
    )
    related_custody = models.ForeignKey(
        'custody.CustomerCustodyLedger', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ledger_offsets',
        help_text='CUSTODY_OFFSET kayıtlarında bağlı emanet hareketi',
    )

    # ── Onay Zinciri ────────────────────────────────────────────────
    requires_approval = models.BooleanField(default=False)
    is_approved = models.BooleanField(default=True)
    approved_by = models.ForeignKey(
        'accounts.Users', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='approved_ledger_entries',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.TextField(blank=True, default='')

    # ── Açıklama ────────────────────────────────────────────────────
    description = models.TextField(blank=True, default='')

    # ── Geriye Uyum: is_active mutation pattern ─────────────────────
    # YENİ KOD bu alanı KULLANMAMALI; iptal için REVERSAL yazmalıdır.
    # Eski cancel_row/cancel_group akışlarını bozmamak için tutuluyor.
    is_active = models.BooleanField(default=True)

    # ── Audit Trail ─────────────────────────────────────────────────
    created_on = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        'accounts.Users', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_ledger_entries',
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'CustomerLedger'
        indexes = [
            models.Index(fields=['customer', 'is_active'], name='cust_ledger_cust_active_idx'),
            models.Index(fields=['process_no'], name='cust_ledger_process_no_idx'),
            models.Index(fields=['transaction_type', 'is_active'], name='cust_ledger_type_active_idx'),
            models.Index(fields=['parent'], name='cust_ledger_parent_idx'),
            models.Index(fields=['is_approved', 'requires_approval'], name='cust_ledger_approval_idx'),
            models.Index(fields=['related_payment'], name='cust_ledger_payment_idx'),
            models.Index(fields=['related_custody'], name='cust_ledger_custody_idx'),
        ]
        ordering = ['-created_on']

    def __str__(self):
        return f"{self.customer} {self.transaction_type} {self.amount_hs} HS"

    # ── Yardımcı Property'ler ───────────────────────────────────────
    @property
    def is_collection(self):
        return self.transaction_type in self.COLLECTION_TYPES

    @property
    def is_adjustment(self):
        """Kur farkı / iskonto / silme — dolaylı (gerçek tahsilat olmayan) kapatma kalemi."""
        return self.transaction_type in (
            self.FX_GAIN, self.FX_LOSS, self.DISCOUNT, self.WRITEOFF,
        )

    @property
    def is_debt_increasing(self):
        return self.transaction_type in Customers.DEBT_INCREASING_TYPES

    @property
    def is_debt_decreasing(self):
        return self.transaction_type in Customers.DEBT_DECREASING_TYPES

    @property
    def signed_amount_hs(self):
        """Bakiye etkisi (işaretli). Pozitif = borç artar, negatif = borç azalır."""
        if self.transaction_type == self.CORRECTION:
            return self.amount_hs_signed
        if self.transaction_type == self.REVERSAL:
            # REVERSAL parent'ın işaretini ters çevirir
            if self.reversal_target_type in Customers.DEBT_INCREASING_TYPES:
                return -self.amount_hs
            elif self.reversal_target_type in Customers.DEBT_DECREASING_TYPES:
                return self.amount_hs
            return Decimal('0')
        if self.is_debt_increasing:
            return self.amount_hs
        if self.is_debt_decreasing:
            return -self.amount_hs
        return Decimal('0')
