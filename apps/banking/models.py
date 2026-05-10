# ============================================================================
# DOSYA: apps/banking/models.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v4 — Kasa-Cari Entegrasyon Altyapısı (Faz 14)
#
# DEĞİŞİKLİK ÖZETİ (v3 → v4):
#   + CashboxLedger modeli eklendi:
#       - Append-Only kasa hareketi defteri
#       - INCOME / EXPENSE / TRANSFER_IN / TRANSFER_OUT / DAILY_OPEN / DAILY_CLOSE
#       - related_payment FK ile Payment'a bağlanır
#       - related_expense FK ile IncomeExpenseLedger'a bağlanır
#       - balance_snapshot alanı: yazım anındaki kasa bakiyesi (sorgu hızı)
#   + IncomeExpenseLedger modeli eklendi:
#       - Mağaza P&L (gelir-gider) defteri
#       - DISCOUNT_EXPENSE / FX_LOSS_EXPENSE / FX_GAIN_INCOME / WRITEOFF_EXPENSE / OTHER
#       - related_customer_ledger FK ile CustomerLedger.DISCOUNT/FX_GAIN/WRITEOFF'a bağlanır
#       - Onay sonrası ApprovalService tarafından otomatik yazılır
#   + BankAccount.get_balance() metodu eklendi:
#       - CashboxLedger SSOT'u olarak kasa bakiyesi sorgusu
#
# ÖNCEKİ DEĞİŞİKLİK ÖZETİ (v2 → v3):
#   + BankAccount modeline eklenenler:
#       - AccountType enum (POS / BANK / CASH)
#       - account_type alanı: hesabın hangi ödeme tipine hizmet ettiği
#       - reconciliation_tolerance alanı: tutar eşleşme toleransı (TL)
#   + BankTransaction modeline eklenenler:
#       - bank_account FK: IBAN string yerine normalize hesap bağlantısı
#       - kp_bank_account_idx index'i
#
# ÖNCEKİ DEĞİŞİKLİK ÖZETİ (v1 → v2):
#   + EsurecTenantCredential modeli eklendi:
#       - Mağaza bazlı e-Süreç tenant token saklama (Fernet şifreli)
#       - efatura_active / banking_active durum önbelleği
#       - health check zaman damgası + durum takibi
#       - tenant_token property: şeffaf şifrele/çöz
#       - is_token_valid, health_check_stale, update_health() yardımcıları
# ============================================================================

import uuid
import logging
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

log = logging.getLogger(__name__)


# ============================================================================
# BANKA HAREKETİ (Mysoft Açık Bankacılık API)
# ============================================================================

class BankTransaction(models.Model):
    """
    Mysoft açık bankacılık API'den çekilen banka hareketleri.
    Kuyum Plus mimarisine göre store FK kullanır (dealer yerine).
    """

    class PlusMinus(models.IntegerChoices):
        DEBIT = 1, 'Borç / Giriş (Gelen)'
        CREDIT = -1, 'Alacak / Çıkış (Giden)'

    class Currency(models.TextChoices):
        TRY = 'TRY', 'Türk Lirası'
        USD = 'USD', 'Amerikan Doları'
        EUR = 'EUR', 'Euro'
        GBP = 'GBP', 'İngiliz Sterlini'

    class MatchStatus(models.TextChoices):
        UNMATCHED = 'UNMATCHED', 'Eşleştirilmedi'
        AUTO_MATCHED = 'AUTO', 'Otomatik Eşleşti'
        MANUAL_MATCHED = 'MANUAL', 'Manuel Eşleşti'

    class PaymentStatus(models.TextChoices):
        UNPAID = 'UNPAID', 'Ödenmedi'
        PARTIAL = 'PARTIAL', 'Kısmi Ödendi'
        PAID = 'PAID', 'Tamamen Ödendi'
        OVERPAID = 'OVERPAID', 'Fazla Ödeme'

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Store İlişkisi (Kuyum Plus mimarisine uygun) ---
    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='bank_transactions',
        verbose_name=_('Mağaza'),
        null=True, blank=True,
    )

    # --- API Kimlik Bilgileri (İdempotency için) ---
    api_transaction_id = models.BigIntegerField(
        _('Mysoft İşlem ID'),
        null=True, blank=True,
        help_text="Mysoft banka hareket tablosunun birincil anahtarı (id).",
    )
    api_created_date = models.DateTimeField(_('Mysoft Kayıt Zamanı'), null=True, blank=True)

    # --- e-Süreç İç Referansı (FAZ A.1 / GAP-07) ---
    # e-Süreç tarafındaki BankTransaction.id (UUID) değerini saklar. Fatura
    # gönderme, mark-invoiced geri bildirimi, PDF/XML erişimi gibi S2S
    # akışlarında karşı tarafın iç ID'sine referans vermek için kullanılır.
    # api_transaction_id (Mysoft ID) ile karıştırılmamalıdır.
    esurec_transaction_id = models.UUIDField(
        _('e-Süreç İşlem UUID'),
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "e-Süreç BankTransaction.id (UUID). S2S yanıtındaki 'esurec_id' "
            "alanından doldurulur. mark-invoiced geri bildirimi ve "
            "fatura↔hareket bağlamı için zorunludur."
        ),
    )

    # --- Hesap Bilgileri ---
    iban = models.CharField(_('IBAN'), max_length=50, null=True, blank=True)
    account_no = models.CharField(_('Hesap Numarası'), max_length=50, null=True, blank=True)
    account_name = models.CharField(_('Hesap Adı'), max_length=255, null=True, blank=True)
    bank_name = models.CharField(_('Banka Adı'), max_length=255, null=True, blank=True)
    bank_branch_code = models.CharField(_('Şube Kodu'), max_length=50, null=True, blank=True)
    bank_branch_name = models.CharField(_('Şube Adı'), max_length=255, null=True, blank=True)

    # --- Normalize Edilmiş Hesap Bağlantısı (v3: Mutabakat) ---
    bank_account = models.ForeignKey(
        'banking.BankAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transactions',
        verbose_name=_('Banka Hesabı'),
        help_text=(
            "IBAN eşleşmesinden otomatik doldurulur. "
            "Mutabakat motorunun Payment ile karşılaştırma yapabilmesi için "
            "her iki tarafta da aynı BankAccount referansı bulunmalıdır."
        ),
    )
    currency_code = models.CharField(
        _('Para Birimi'), max_length=3, choices=Currency.choices, default=Currency.TRY
    )

    # --- İşlem Bilgileri ---
    doc_no = models.CharField(_('İşlem No'), max_length=100, null=True, blank=True)
    doc_date = models.DateTimeField(_('İşlem Tarihi'), null=True, blank=True)
    reference = models.CharField(_('Referans'), max_length=100, null=True, blank=True)
    plus_minus = models.IntegerField(
        _('Hareket Yönü'), choices=PlusMinus.choices, default=PlusMinus.DEBIT,
        help_text="1: Gelen (Borç), -1: Giden (Alacak)",
    )
    amount = models.DecimalField(_('Tutar'), max_digits=18, decimal_places=2, default=0.00)
    balance = models.DecimalField(_('Bakiye (İşlem Öncesi)'), max_digits=18, decimal_places=2, default=0.00)
    current_balance = models.DecimalField(_('Anlık Bakiye'), max_digits=18, decimal_places=2, default=0.00)
    note = models.TextField(_('Açıklama'), null=True, blank=True)

    # --- Karşı Taraf Bilgileri ---
    other_iban = models.CharField(_('Karşı Taraf IBAN'), max_length=50, null=True, blank=True)
    other_vkn_tckn = models.CharField(_('Karşı Taraf VKN/TCKN'), max_length=20, null=True, blank=True)
    other_name = models.CharField(_('Karşı Taraf Adı'), max_length=255, null=True, blank=True)

    # --- Mysoft Kodlamaları ---
    bank_transaction_code = models.CharField(_('İşlem Kodu'), max_length=50, null=True, blank=True)
    bank_transaction_desc = models.CharField(_('İşlem Açıklaması'), max_length=255, null=True, blank=True)
    mysoft_transaction_type = models.CharField(_('Mysoft İşlem Tipi'), max_length=100, null=True, blank=True)

    # --- Cari Eşleştirme ---
    customer = models.ForeignKey(
        'customers.Customers',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='bank_transactions',
        verbose_name=_('Eşleşen Cari'),
    )
    match_status = models.CharField(
        _('Eşleştirme Durumu'),
        max_length=10,
        choices=MatchStatus.choices,
        default=MatchStatus.UNMATCHED,
    )
    match_score = models.IntegerField(
        _('Eşleştirme Skoru'),
        default=0,
        help_text="0-100 arası güven skoru. 60+ otomatik eşleştirme için yeterli.",
    )

    payment_status = models.CharField(
        _('Ödeme Durumu'),
        max_length=10,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
    )

    # --- API Durum ---
    is_succeed = models.BooleanField(_('API Başarılı mı?'), default=True)
    api_message = models.TextField(_('API Mesajı'), null=True, blank=True)
    is_read = models.BooleanField(
        _('Mysoft\'ta Okundu mu?'), default=False,
        help_text="bankTransactionSavedByCustomer ile işaretlendikten sonra True.",
    )

    # --- Sistem ---
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'KP_BankTransactions'
        ordering = ['-doc_date']
        verbose_name = 'Banka Hareketi'
        verbose_name_plural = 'Banka Hareketleri'
        indexes = [
            models.Index(fields=['store', 'doc_date'], name='kp_bank_store_date_idx'),
            models.Index(fields=['store', 'match_status'], name='kp_bank_store_match_idx'),
            models.Index(fields=['api_transaction_id'], name='kp_bank_api_id_idx'),
            models.Index(fields=['other_vkn_tckn'], name='kp_bank_vkn_idx'),
            models.Index(fields=['bank_account'], name='kp_bank_account_idx'),
        ]
        constraints = [
            # İdempotency: Aynı store + api_transaction_id çifti bir kez kaydedilir
            models.UniqueConstraint(
                fields=['store', 'api_transaction_id'],
                name='uniq_kp_bank_txn_per_store',
                condition=models.Q(api_transaction_id__isnull=False),
            )
        ]

    def __str__(self):
        sign = '+' if self.plus_minus == 1 else '-'
        return f"{self.bank_name or '?'} {sign}{self.amount} {self.currency_code} ({self.doc_date})"

    @property
    def is_incoming(self) -> bool:
        """Gelen para mı (havale/EFT)."""
        return self.plus_minus == self.PlusMinus.DEBIT

    @property
    def is_matched(self) -> bool:
        return self.match_status != self.MatchStatus.UNMATCHED


# ============================================================================
# BANKA HESABI
# ============================================================================

class BankAccount(models.Model):
    """
    Kuyumcu mağazasına ait banka hesapları ve POS terminalleri.

    v3 Eklentileri:
        - AccountType enum: POS (Kredi Kartı), BANK (Havale/EFT), CASH (Kasa)
        - account_type: Ödeme tipi ile hesap eşlemesini sağlar
        - reconciliation_tolerance: Banka komisyon farkını tolere eder
    """

    class AccountType(models.TextChoices):
        POS  = 'POS',  _('POS Terminali (Kredi Kartı)')
        BANK = 'BANK', _('Banka Hesabı (Havale/EFT)')
        CASH = 'CASH', _('Kasa (Nakit)')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='bank_accounts',
        null=True, blank=True,
    )
    name = models.CharField(_('Hesap Adı'), max_length=150)
    bank_name = models.CharField(_('Banka Adı'), max_length=150, null=True, blank=True)
    iban = models.CharField(_('IBAN'), max_length=34, null=True, blank=True)
    currency = models.CharField(_('Para Birimi'), max_length=10, default='TRY')

    # --- v3: Mutabakat Altyapısı ---
    account_type = models.CharField(
        _('Hesap Tipi'),
        max_length=10,
        choices=AccountType.choices,
        default=AccountType.BANK,
        db_index=True,
        help_text=(
            "POS: Kredi kartı tahsilatlarını alan POS terminali. "
            "BANK: Havale/EFT gelen banka hesabı. "
            "CASH: Fiziksel nakit kasası (opsiyonel)."
        ),
    )
    reconciliation_tolerance = models.DecimalField(
        _('Mutabakat Toleransı (TL)'),
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.50'),
        help_text=(
            "Banka komisyon kesintisi nedeniyle oluşabilecek tutar farkı toleransı. "
            "Garanti POS %%0.89 keserken Akbank %%0.75 kesebilir; "
            "hesap bazlı tolerans gereksiz DISCREPANCY alarmlarını önler."
        ),
    )

    # ────────────────────────────────────────────────────────────────────
    # FAZ 45 — Multi-Branch Transit Hesap Bayrağı (DORMANT)
    # ────────────────────────────────────────────────────────────────────
    # Transit hesap, şubeler arası transferin "yolda" döneminde sistem
    # genelinde bilanço açığı oluşmasını engelleyen sanal bir kasa
    # hesabıdır. Her şubede otomatik olarak bir transit hesap üretilir
    # (FAZ 46+ aktivasyonunda) ve personel arayüzünde GİZLENİR.
    #
    # Akış:
    #   1) Transfer başladı → kaynak kasa - amount, kaynak transit + amount
    #   2) Hedef kabul etti → kaynak transit - amount, hedef kasa + amount
    # Sistem genelinde toplam varlık her an dengededir.
    #
    # Bu alan FAZ 45'te yalnızca ŞEMA olarak eklenir; transit hesaplar
    # FAZ 46 data migration'ı tarafından oluşturulacaktır.
    is_inter_branch_transit_account = models.BooleanField(
        _('Şubeler Arası Transit Hesap mı?'),
        default=False,
        db_index=True,
        help_text=(
            "Şubeler arası transferde 'yolda' bekleyen tutarları geçici "
            "olarak tutan sanal sistem hesabı. Personel arayüzlerinde "
            "gizlenir; sadece transfer servisleri yazar/okur."
        ),
    )

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'KP_BankAccounts'
        verbose_name = _('Banka Hesabı')
        verbose_name_plural = _('Banka Hesapları')
        indexes = [
            models.Index(
                fields=['store', 'account_type', 'is_active'],
                name='kp_ba_store_type_active_idx',
            ),
        ]

    def __str__(self):
        type_label = self.get_account_type_display() if self.account_type else ''
        return f"{self.name} ({type_label}) — {self.iban or 'IBAN yok'}"

    # ────────────────────────────────────────────────────────────────────
    # FAZ 14 — KASA BAKİYE SSOT (Single Source of Truth)
    # ────────────────────────────────────────────────────────────────────
    def get_balance(self, currency: str = None) -> Decimal:
        """Kasanın anlık bakiyesini CashboxLedger üzerinden hesaplar.

        Bu metod kasa bakiyesinin TEK GERÇEK KAYNAĞI'dır (SSOT). Payment
        agregasyonu veya DailyCashClose kullanılmamalıdır.

        Hesaplama:
            Σ(is_inflow satırlar) - Σ(is_outflow satırlar)

        Args:
            currency: 'TRY' / 'USD' / 'EUR' / 'GBP' / 'HS'. None ise
                      kasanın varsayılan para birimi (self.currency)
                      kullanılır.

        Returns:
            Decimal — bakiye (negatif olabilir, örn. açıkta kasa).
        """
        from apps.banking.models import CashboxLedger

        if currency is None:
            currency = self.currency or 'TRY'
        currency = currency.upper()

        qs = CashboxLedger.objects.filter(
            cashbox=self,
            currency=currency,
        )

        inflow_types = (
            CashboxLedger.MovementType.INCOME,
            CashboxLedger.MovementType.TRANSFER_IN,
            CashboxLedger.MovementType.DAILY_OPEN,
        )
        outflow_types = (
            CashboxLedger.MovementType.EXPENSE,
            CashboxLedger.MovementType.TRANSFER_OUT,
            CashboxLedger.MovementType.REVERSAL,
        )

        total_in = qs.filter(movement_type__in=inflow_types).aggregate(
            s=models.Sum('amount'),
        )['s'] or Decimal('0')
        total_out = qs.filter(movement_type__in=outflow_types).aggregate(
            s=models.Sum('amount'),
        )['s'] or Decimal('0')

        # DAILY_CLOSE düzeltmeleri: signed_amount alanıyla işlenmesi gerekir
        # ama mevcut model amount'u pozitif tutuyor. Bu tipleri ayrıca
        # ele almak için liste üzerinden iterate edilir.
        close_qs = qs.filter(
            movement_type=CashboxLedger.MovementType.DAILY_CLOSE,
        )
        close_adjustment = Decimal('0')
        for entry in close_qs.only('amount'):
            close_adjustment += entry.amount

        balance = (total_in - total_out + close_adjustment).quantize(Decimal('0.01'))
        return balance

    def get_balance_eur_equivalent(self) -> Decimal:
        """Kasanın varsayılan para biriminden bağımsız, TL karşılığı bakiye.

        Tüm currency'lerin amount_eur_equivalent toplamı üzerinden hesaplanır.
        Çok-para-birimli kasalar için raporlama amaçlıdır.
        """
        from apps.banking.models import CashboxLedger

        qs = CashboxLedger.objects.filter(cashbox=self)

        inflow_types = (
            CashboxLedger.MovementType.INCOME,
            CashboxLedger.MovementType.TRANSFER_IN,
            CashboxLedger.MovementType.DAILY_OPEN,
        )
        outflow_types = (
            CashboxLedger.MovementType.EXPENSE,
            CashboxLedger.MovementType.TRANSFER_OUT,
            CashboxLedger.MovementType.REVERSAL,
        )

        total_in = qs.filter(movement_type__in=inflow_types).aggregate(
            s=models.Sum('amount_eur_equivalent'),
        )['s'] or Decimal('0')
        total_out = qs.filter(movement_type__in=outflow_types).aggregate(
            s=models.Sum('amount_eur_equivalent'),
        )['s'] or Decimal('0')

        close_qs = qs.filter(
            movement_type=CashboxLedger.MovementType.DAILY_CLOSE,
        )
        close_adjustment = sum(
            (e.amount_eur_equivalent for e in close_qs.only('amount_eur_equivalent')),
            Decimal('0'),
        )

        return (total_in - total_out + close_adjustment).quantize(Decimal('0.01'))


# ============================================================================
# POS KOMİSYON ORANLARI  ←  FAZ 4
# ============================================================================

class POSCommissionRate(models.Model):
    """
    POS terminali bazında taksit komisyon oranları.

    Her BankAccount (account_type=POS) için farklı taksit sayılarına
    ve kart tiplerine göre komisyon oranları tanımlanabilir.

    Kullanım:
        Garanti POS → Visa → 3 taksit → %2.49, 30 gün vade
        Garanti POS → Genel → 1 (Tek Çekim) → %1.10, 1 gün vade
    """

    class CardType(models.TextChoices):
        GENERIC    = 'GENERIC',    'Genel'
        VISA       = 'VISA',       'Visa'
        MASTERCARD = 'MASTERCARD', 'Mastercard'
        AMEX       = 'AMEX',       'Amex'
        TROY       = 'TROY',       'Troy'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bank_account = models.ForeignKey(
        'BankAccount',
        on_delete=models.CASCADE,
        related_name='commission_rates',
        limit_choices_to={'account_type': 'POS'},
        verbose_name='POS Hesabı',
    )
    card_type = models.CharField(
        'Kart Tipi',
        max_length=15,
        choices=CardType.choices,
        default=CardType.GENERIC,
    )
    installment_count = models.PositiveIntegerField(
        'Taksit Sayısı',
        default=1,
        help_text="1 = Tek Çekim, 2+ = Taksit sayısı",
    )
    commission_rate = models.DecimalField(
        'Komisyon Oranı (%)',
        max_digits=5,
        decimal_places=2,
        help_text="Yüzde olarak komisyon oranı (ör: 1.89)",
    )
    maturity_days = models.PositiveIntegerField(
        'Vade Süresi (gün)',
        default=1,
        help_text="Tutarın banka hesabına geçiş süresi",
    )
    is_active = models.BooleanField(default=True)
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'KP_POSCommissionRates'
        verbose_name = 'POS Komisyon Oranı'
        verbose_name_plural = 'POS Komisyon Oranları'
        unique_together = ['bank_account', 'card_type', 'installment_count']
        indexes = [
            models.Index(
                fields=['bank_account', 'is_active'],
                name='kp_pcr_ba_active_idx',
            ),
        ]

    def __str__(self):
        return (
            f"{self.bank_account.name} — {self.get_card_type_display()} "
            f"— {self.installment_count} taksit → %{self.commission_rate}"
        )


# ============================================================================
# GÜNLÜK KAPANIŞ (Z-RAPORU / FİZİKSEL SAYIM)  ←  FAZ 6
# ============================================================================

class DailyCashClose(models.Model):
    """
    Gun sonu kasa kapanisi — fiziksel sayim ile sistem bakiyesi karsilastirmasi.

    Kuyumcu gun sonunda kasadaki fiziksel parayi / POS fislerini sayar,
    sisteme girer. Fark otomatik hesaplanir ve kaydedilir.
    MASAK denetimlerinde kanit niteligi tasir.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='daily_cash_closes',
        null=True, blank=True,
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.CASCADE,
        related_name='daily_closes',
        verbose_name=_('Kasa'),
    )
    date = models.DateField(_('Kapanıs Tarihi'), db_index=True)
    system_balance = models.DecimalField(
        _('Sistem Bakiyesi'),
        max_digits=15, decimal_places=2, default=Decimal('0'),
    )
    physical_count = models.DecimalField(
        _('Fiziksel Sayim'),
        max_digits=15, decimal_places=2, default=Decimal('0'),
    )
    difference = models.DecimalField(
        _('Fark (Fiziksel - Sistem)'),
        max_digits=15, decimal_places=2, default=Decimal('0'),
        help_text='Pozitif = fazla, Negatif = eksik',
    )
    note = models.TextField(_('Not'), blank=True, null=True)
    closed_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='daily_closes',
        verbose_name=_('Kapatan Kullanici'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'KP_DailyCashCloses'
        verbose_name = _('Gunluk Kapanıs')
        verbose_name_plural = _('Gunluk Kapanislar')
        unique_together = [('store', 'bank_account', 'date')]
        ordering = ['-date']

    def __str__(self):
        return f"{self.bank_account.name} — {self.date} — Fark: {self.difference}"


# ============================================================================
# ESUREC TENANT KİMLİK BİLGİSİ  ←  FAZ 1 GÜVENLİK ALTYAPISI
# ============================================================================

class EsurecTenantCredential(models.Model):
    """
    Her mağaza için e-Süreç entegrasyon kimlik bilgilerini güvenli olarak saklar.

    Mimari:
        - Katman 2 Token (Tenant Token): mağazaya özgü, döngüsel (90 günde bir rotasyon)
        - tenant_token_enc: cryptography.fernet ile ESUREC_CREDENTIAL_KEY kullanılarak şifreli
        - tenant_token (property): şeffaf şifrele/çöz, hiçbir zaman açık metin olarak log'a yazılmaz
        - efatura_active / banking_active: e-Süreç'e sorulmadan önbellekten okunur (TTL 1 saat)
        - health_check_stale (property): son kontrolden 1 saat geçmişse True

    Kullanım:
        cred = store.esurec_credential
        raw_token = cred.tenant_token          # çözerek okuma
        cred.tenant_token = "yeni_token_xxx"  # şifrele ve tenant_token_enc'e yaz
        cred.save()

    Şifreleme anahtarı üretimi (bir kez, Python'da):
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())  # → .env'e ESUREC_CREDENTIAL_KEY=... olarak ekle
    """

    class HealthStatus(models.TextChoices):
        UNKNOWN = 'UNKNOWN', 'Bilinmiyor'
        OK = 'OK', 'Sağlıklı'
        DEGRADED = 'DEGRADED', 'Bozuk / Yavaş'
        ERROR = 'ERROR', 'Hatalı'

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Mağaza Bağlantısı (Bire-bir) ---
    store = models.OneToOneField(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='esurec_credential',
        verbose_name=_('Mağaza'),
    )

    # --- e-Süreç Tarafındaki UUID Önbelleği ---
    esurec_customer_uuid = models.UUIDField(
        _('e-Süreç Müşteri UUID'),
        null=True,
        blank=True,
        help_text=(
            "e-Süreç tarafındaki DealerCustomer.id önbelleği. "
            "Her başarılı S2S yanıtında güncellenebilir. "
            "VKN değişim senaryolarında sabit referans görevi görür."
        ),
    )

    # --- Fernet Şifreli Tenant Token ---
    tenant_token_enc = models.TextField(
        _('Şifreli Tenant Token'),
        blank=True,
        help_text=(
            "ESUREC_CREDENTIAL_KEY ile Fernet şifreli Katman 2 token. "
            "Hiçbir zaman açık metin olarak log'a veya response'a yazılmamalıdır."
        ),
    )
    token_expires_at = models.DateTimeField(
        _('Token Geçerlilik Tarihi'),
        null=True,
        blank=True,
        help_text="Token süresi bu tarihten önce rotasyon edilmelidir (önerilen: 90 gün).",
    )

    # --- Entegrasyon Durumu ---
    is_active = models.BooleanField(
        _('Entegrasyon Aktif'),
        default=True,
        help_text="False ise tüm e-Süreç S2S istekleri durdurulur.",
    )

    # --- Durum Önbellekleri (Periyodik Health Check ile Güncellenir) ---
    efatura_active = models.BooleanField(
        _('e-Fatura Aktif'),
        default=False,
        help_text="e-Süreç health check'ten önbelleklenen e-fatura modülü durumu (TTL: 1 saat).",
    )
    banking_active = models.BooleanField(
        _('Açık Bankacılık Aktif'),
        default=False,
        help_text="e-Süreç health check'ten önbelleklenen Mysoft bankacılık modülü durumu (TTL: 1 saat).",
    )
    last_health_check_at = models.DateTimeField(
        _('Son Health Check Zamanı'),
        null=True,
        blank=True,
    )
    last_health_status = models.CharField(
        _('Son Health Check Durumu'),
        max_length=20,
        choices=HealthStatus.choices,
        default=HealthStatus.UNKNOWN,
    )

    # --- FAZ B.3 / GAP-05: Uzak Token Durumu (e-Süreç ayna kayıt) ---
    # e-Süreç tarafındaki TenantApiToken.status değerinin kopyası.
    # Health check sırasında güncellenir; UI proaktif uyarı için okur
    # (SUSPENDED/REVOKED vb. durumda kullanıcı 401 almadan önce bilgilenir).
    class RemoteTokenStatus(models.TextChoices):
        UNKNOWN = 'UNKNOWN', _('Bilinmiyor (henüz sorgulanmadı)')
        PENDING = 'PENDING', _('Onay Bekliyor')
        APPROVED = 'APPROVED', _('Onaylı / Aktif')
        REJECTED = 'REJECTED', _('Reddedildi')
        SUSPENDED = 'SUSPENDED', _('Askıya Alındı')
        REVOKED = 'REVOKED', _('İptal Edildi')

    remote_token_status = models.CharField(
        _('e-Süreç Token Durumu'),
        max_length=20,
        choices=RemoteTokenStatus.choices,
        default=RemoteTokenStatus.UNKNOWN,
        help_text=(
            "e-Süreç TenantApiToken.status alanının ayna kopyası. "
            "Health check ile güncellenir."
        ),
    )
    remote_suspension_count = models.PositiveSmallIntegerField(
        _('e-Süreç Askı Sayacı'),
        default=0,
        help_text=(
            "e-Süreç tarafında token kaç kez askıya alındı. "
            "3'e ulaştığında otomatik REVOKED'a geçer (e-Süreç kuralı)."
        ),
    )
    remote_status_changed_at = models.DateTimeField(
        _('e-Süreç Durum Son Değişim'),
        null=True,
        blank=True,
        help_text=(
            "e-Süreç TenantApiToken.status_changed_at değeri (yansıma)."
        ),
    )
    remote_token_expires_at = models.DateTimeField(
        _('e-Süreç Token Geçerlilik (uzaktan)'),
        null=True,
        blank=True,
        help_text=(
            "e-Süreç tarafındaki expires_at değeri. token_expires_at lokal "
            "alandan farklı olabilir; lokal alan yetkili kabul edilmez. "
            "Uyarı dashboard'u bu alanı kullanır."
        ),
    )

    # --- Sistem ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'KP_EsurecTenantCredentials'
        verbose_name = 'e-Süreç Tenant Kimlik Bilgisi'
        verbose_name_plural = 'e-Süreç Tenant Kimlik Bilgileri'

    def __str__(self):
        store_name = self.store.title if self.store_id else '?'
        status = '✅ Aktif' if self.is_active else '❌ Pasif'
        return f"{store_name} — {status}"

    # ────────────────────────────────────────────────────────────────────
    # FERNET ŞİFRELEME / ÇÖZME
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_fernet():
        """
        ESUREC_CREDENTIAL_KEY ayarından Fernet nesnesi oluşturur.

        Gereksinim: cryptography paketi (pip install cryptography)
        Anahtar formatı: URL-safe base64 kodlu 32-byte değer
        Üretim: from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())
        """
        from django.conf import settings
        from cryptography.fernet import Fernet

        key = getattr(settings, 'ESUREC_CREDENTIAL_KEY', '')
        if not key:
            raise ValueError(
                "ESUREC_CREDENTIAL_KEY settings/env'de tanımlı değil. "
                "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode()) "
                "komutuyla bir anahtar üretip .env'e ESUREC_CREDENTIAL_KEY=<değer> olarak ekleyin."
            )
        if isinstance(key, str):
            key = key.encode()
        return Fernet(key)

    @property
    def tenant_token(self) -> str:
        """
        Şifreli token'ı çözerek açık metin döndürür.
        Token yoksa veya çözme başarısız olursa '' döner; exception fırlatmaz.
        """
        if not self.tenant_token_enc:
            return ''
        try:
            fernet = self._get_fernet()
            return fernet.decrypt(self.tenant_token_enc.encode()).decode()
        except Exception as exc:
            # Token değerini asla loga yazma — sadece hata tipini yaz
            log.error(
                "EsurecTenantCredential(pk=%s, store_id=%s) token çözme hatası: %s",
                self.pk, self.store_id, type(exc).__name__,
            )
            return ''

    @tenant_token.setter
    def tenant_token(self, raw_token: str):
        """
        Açık metin token'ı şifreleyerek tenant_token_enc alanına yazar.
        Bu setter çağrıldıktan sonra .save() veya .save(update_fields=[...]) çağırılmalıdır.
        """
        if not raw_token:
            self.tenant_token_enc = ''
            return
        fernet = self._get_fernet()
        self.tenant_token_enc = fernet.encrypt(raw_token.encode()).decode()

    # ────────────────────────────────────────────────────────────────────
    # YARDIMCI METODLAR
    # ────────────────────────────────────────────────────────────────────

    @property
    def is_token_valid(self) -> bool:
        """
        Token hem mevcut hem de süresi geçmemiş mi?
        Servis katmanı bu kontrolü yapmadan isteği göndermeyin.
        """
        if not self.tenant_token_enc:
            return False
        if self.token_expires_at and self.token_expires_at < timezone.now():
            return False
        return True

    @property
    def token_expires_soon(self) -> bool:
        """
        Token önümüzdeki 7 gün içinde sona erecek mi? (rotasyon uyarısı için)
        """
        if not self.token_expires_at:
            return False
        from datetime import timedelta
        return self.token_expires_at < (timezone.now() + timedelta(days=7))

    @property
    def health_check_stale(self) -> bool:
        """
        Son health check 1 saatten eskiyse veya hiç yapılmamışsa True döner.
        Bu durumda e-Süreç'e canlı durum sorgusu yapılmalıdır.
        """
        if not self.last_health_check_at:
            return True
        from datetime import timedelta
        return (timezone.now() - self.last_health_check_at) > timedelta(hours=1)

    def update_health(
        self,
        status: str,
        efatura: bool = None,
        banking: bool = None,
        esurec_uuid=None,
        remote_token_status: str = None,
        remote_suspension_count: int = None,
        remote_status_changed_at=None,
        remote_token_expires_at=None,
    ):
        """
        Health check sonucunu kaydeder. Yalnızca ilgili alanları günceller (update_fields).

        Kullanım:
            cred.update_health('OK', efatura=True, banking=True, esurec_uuid='uuid-str')
            cred.update_health('ERROR')

        FAZ B.3 / GAP-05 ek parametreler:
            remote_token_status         — e-Süreç TenantApiToken.status değeri
            remote_suspension_count     — e-Süreç askı sayacı
            remote_status_changed_at    — e-Süreç status_changed_at (datetime)
            remote_token_expires_at     — e-Süreç expires_at (datetime)
        """
        self.last_health_status = status
        self.last_health_check_at = timezone.now()

        update_fields = ['last_health_status', 'last_health_check_at', 'updated_at']

        if efatura is not None:
            self.efatura_active = efatura
            update_fields.append('efatura_active')

        if banking is not None:
            self.banking_active = banking
            update_fields.append('banking_active')

        if esurec_uuid is not None:
            self.esurec_customer_uuid = esurec_uuid
            update_fields.append('esurec_customer_uuid')

        # ── FAZ B.3 / GAP-05 ─────────────────────────────────────
        if remote_token_status is not None:
            valid = {choice[0] for choice in self.RemoteTokenStatus.choices}
            if remote_token_status in valid:
                self.remote_token_status = remote_token_status
                update_fields.append('remote_token_status')

        if remote_suspension_count is not None:
            try:
                self.remote_suspension_count = max(0, int(remote_suspension_count))
                update_fields.append('remote_suspension_count')
            except (TypeError, ValueError):
                pass

        if remote_status_changed_at is not None:
            self.remote_status_changed_at = remote_status_changed_at
            update_fields.append('remote_status_changed_at')

        if remote_token_expires_at is not None:
            self.remote_token_expires_at = remote_token_expires_at
            update_fields.append('remote_token_expires_at')

        self.save(update_fields=update_fields)

    # ────────────────────────────────────────────────────────────────────
    # FAZ B.3 / GAP-05 — DASHBOARD UYARILARI
    # ────────────────────────────────────────────────────────────────────

    @property
    def remote_token_warning(self) -> dict:
        """
        UI dashboard'una gösterilecek proaktif uyarı bilgisi.

        Returns:
            { 'severity': 'critical'|'warning'|'info'|'ok',
              'code': str,
              'message': str }
        """
        # 1. Henüz health check yapılmamış
        if self.remote_token_status == self.RemoteTokenStatus.UNKNOWN:
            if not self.last_health_check_at:
                return {
                    'severity': 'info',
                    'code': 'NO_HEALTH_CHECK',
                    'message': 'e-Süreç bağlantısı henüz test edilmedi.',
                }
            return {
                'severity': 'warning',
                'code': 'STATUS_UNKNOWN',
                'message': (
                    'e-Süreç token durumu çözülemedi. '
                    'Bağlantıyı tekrar test edin.'
                ),
            }

        # 2. REVOKED — kalıcı iptal
        if self.remote_token_status == self.RemoteTokenStatus.REVOKED:
            return {
                'severity': 'critical',
                'code': 'TOKEN_REVOKED',
                'message': (
                    'e-Süreç token erişiminiz kalıcı olarak iptal edildi. '
                    'Yeni başvuru için e-Süreç yöneticinize başvurun.'
                ),
            }

        # 3. REJECTED — reddedildi
        if self.remote_token_status == self.RemoteTokenStatus.REJECTED:
            return {
                'severity': 'critical',
                'code': 'TOKEN_REJECTED',
                'message': (
                    'e-Süreç entegrasyon başvurunuz reddedildi. '
                    'Detay için e-Süreç yöneticinize başvurun.'
                ),
            }

        # 4. SUSPENDED — askı
        if self.remote_token_status == self.RemoteTokenStatus.SUSPENDED:
            sc = self.remote_suspension_count or 0
            remaining = max(0, 3 - sc)
            return {
                'severity': 'critical',
                'code': 'TOKEN_SUSPENDED',
                'message': (
                    f'e-Süreç hesabınız askıya alındı '
                    f'(askı #{sc}/3, {remaining} hak kaldı). '
                    'e-Süreç yöneticinize başvurun.'
                ),
            }

        # 5. PENDING — onay bekliyor
        if self.remote_token_status == self.RemoteTokenStatus.PENDING:
            return {
                'severity': 'warning',
                'code': 'TOKEN_PENDING',
                'message': (
                    'e-Süreç entegrasyon başvurunuz onay bekliyor. '
                    'Onaylanana kadar API çağrıları reddedilecektir.'
                ),
            }

        # 6. APPROVED + expires soon
        if self.remote_token_status == self.RemoteTokenStatus.APPROVED:
            exp = self.remote_token_expires_at
            if exp:
                from datetime import timedelta
                now = timezone.now()
                if exp < now:
                    return {
                        'severity': 'critical',
                        'code': 'TOKEN_EXPIRED',
                        'message': 'e-Süreç token süresi dolmuş. Yenileme gerekli.',
                    }
                if exp < (now + timedelta(days=7)):
                    days_left = (exp - now).days
                    return {
                        'severity': 'warning',
                        'code': 'TOKEN_EXPIRES_SOON',
                        'message': (
                            f'e-Süreç token süresi {days_left} gün içinde dolacak. '
                            'Rotasyon planlayın.'
                        ),
                    }

        return {
            'severity': 'ok',
            'code': 'OK',
            'message': 'e-Süreç token aktif ve sağlıklı.',
        }


# ============================================================================
# KASA DEFTERİ (CashboxLedger)  ←  FAZ 14 — Kasa-Cari Entegrasyonu
# ============================================================================

class CashboxLedger(models.Model):
    """
    Append-Only Kasa Defteri — kasanın (BankAccount) tüm hareketlerinin
    iz kaydı.

    NEDEN GEREKLİ:
        - Payment kayıtları, kasaya giren parayı temsil eder ama "kasa
          bakiyesi"nin SSOT'u (Single Source of Truth) değildir.
        - DailyCashClose günlük kapanış sayımı; anlık bakiye sorgusu için
          uygun değildir.
        - BankTransaction Mysoft API'sinden beslenir; iç hareketleri
          (nakit tahsilat, nakit ödeme, kasalar arası transfer) içermez.
        - Kasa-Cari mutabakatının yapılabilmesi için her Payment ile
          eşzamanlı yazılan Append-Only bir kasa hareketi gerekir.

    YAZIM KURALLARI:
        - Her satır CollectionService veya CashboxService tarafından
          atomic blok içinde yazılır.
        - Mevcut satırlar ASLA güncellenmez; iptal için REVERSAL satırı
          (movement_type=EXPENSE + parent=original) yazılır.
        - balance_snapshot alanı, sorgu hızı için yazım anındaki
          kümülatif kasa bakiyesini saklar (audit + denetim).

    ESTHETİK ÇERÇEVE:
        BankAccount(CASH) ─ 1:N ─ CashboxLedger
                                      ↕ related_payment (Payment)
                                      ↕ related_expense (IncomeExpenseLedger)
    """

    class MovementType(models.TextChoices):
        INCOME       = 'INCOME',       _('Kasa Girişi (Tahsilat)')
        EXPENSE      = 'EXPENSE',      _('Kasa Çıkışı (Ödeme/Gider)')
        TRANSFER_IN  = 'TRANSFER_IN',  _('Kasalar Arası Giriş')
        TRANSFER_OUT = 'TRANSFER_OUT', _('Kasalar Arası Çıkış')
        DAILY_OPEN   = 'DAILY_OPEN',   _('Gün Açılış Bakiyesi')
        DAILY_CLOSE  = 'DAILY_CLOSE',  _('Gün Kapanış Düzeltmesi')
        REVERSAL     = 'REVERSAL',     _('İptal Karşı Girişi')

    class Currency(models.TextChoices):
        TRY = 'TRY', 'Türk Lirası'
        USD = 'USD', 'Amerikan Doları'
        EUR = 'EUR', 'Euro'
        GBP = 'GBP', 'İngiliz Sterlini'
        HS  = 'HS',  'Has Altın (gram)'

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Kasa Bağlantısı ---
    cashbox = models.ForeignKey(
        'banking.BankAccount',
        on_delete=models.PROTECT,
        related_name='cashbox_ledger_entries',
        verbose_name=_('Kasa'),
        help_text=(
            "CashboxLedger satırlarının yazılabileceği BankAccount. "
            "account_type=CASH/BANK/POS olabilir; tipik olarak CASH "
            "kasaları için kullanılır ama tüm tipler desteklenir."
        ),
    )
    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='cashbox_ledger_entries',
        verbose_name=_('Mağaza'),
        null=True,
        blank=True,
    )

    # --- Hareket Bilgisi ---
    movement_type = models.CharField(
        _('Hareket Tipi'),
        max_length=15,
        choices=MovementType.choices,
        db_index=True,
    )
    amount = models.DecimalField(
        _('Tutar'),
        max_digits=18,
        decimal_places=2,
        help_text=(
            "Hareketin tutarı, daima POZİTİF değer. "
            "Yön (giriş/çıkış) movement_type alanı ile belirlenir."
        ),
    )
    currency = models.CharField(
        _('Para Birimi'),
        max_length=3,
        choices=Currency.choices,
        default=Currency.TRY,
        help_text=(
            "Hareketin para birimi. Çoklu para birimi destekli kasalarda "
            "(USD/EUR kasaları) ilgili currency yazılır."
        ),
    )
    amount_eur_equivalent = models.DecimalField(
        _('TL Karşılığı'),
        max_digits=18,
        decimal_places=2,
        default=Decimal('0'),
        help_text=(
            "Hareketin TL cinsinden karşılığı (raporlama için). "
            "Yazım anındaki kur ile sabitlenir."
        ),
    )
    exchange_rate = models.DecimalField(
        _('İşlem Kuru'),
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=(
            "currency=TRY değilse, işlem anındaki TRY/Döviz kuru. "
            "TRY hareketler için NULL kalır."
        ),
    )

    # --- Bakiye Snapshot ---
    balance_snapshot = models.DecimalField(
        _('Bakiye Snapshot (yazım anında)'),
        max_digits=18,
        decimal_places=2,
        default=Decimal('0'),
        help_text=(
            "Bu satır yazıldıktan sonraki kasa kümülatif bakiyesi. "
            "Sorgu hızı ve denetim amaçlıdır; otorite değildir — "
            "asıl bakiye agregasyon ile hesaplanır."
        ),
    )

    # --- İlişkili Kayıtlar ---
    related_payment = models.ForeignKey(
        'process.Payment',
        on_delete=models.PROTECT,
        related_name='cashbox_entries',
        null=True,
        blank=True,
        verbose_name=_('İlişkili Ödeme'),
        help_text=(
            "Tahsilat veya ödeme akışından gelen Payment kaydı. "
            "Kasa-Cari mutabakatının iki ucu arasındaki bağ."
        ),
    )
    related_expense = models.ForeignKey(
        'banking.IncomeExpenseLedger',
        on_delete=models.PROTECT,
        related_name='cashbox_entries',
        null=True,
        blank=True,
        verbose_name=_('İlişkili Gelir/Gider Kaydı'),
        help_text=(
            "Kasa girişi/çıkışı bir muhasebe gelir/gider kalemi ile "
            "ilişkili ise (örn. iskonto zararının nakit yansıması)."
        ),
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        related_name='reversals',
        null=True,
        blank=True,
        verbose_name=_('Orijinal Satır (REVERSAL için)'),
        help_text=(
            "movement_type=REVERSAL ise iptal edilen orijinal satır. "
            "Append-Only ihlali olmadan iptal akışını sağlar."
        ),
    )

    # --- Audit / İz ---
    process_no = models.CharField(
        _('İşlem No'),
        max_length=50,
        null=True,
        blank=True,
        db_index=True,
    )
    description = models.CharField(
        _('Açıklama'),
        max_length=255,
        blank=True,
        default='',
    )
    created_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cashbox_ledger_entries',
        verbose_name=_('Yazan Kullanıcı'),
    )
    created_on = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default='')

    class Meta:
        db_table = 'KP_CashboxLedger'
        verbose_name = _('Kasa Defteri Satırı')
        verbose_name_plural = _('Kasa Defteri Satırları')
        ordering = ['-created_on']
        indexes = [
            models.Index(
                fields=['cashbox', 'created_on'],
                name='kp_cb_cashbox_date_idx',
            ),
            models.Index(
                fields=['store', 'movement_type', 'created_on'],
                name='kp_cb_store_type_date_idx',
            ),
            models.Index(
                fields=['process_no'],
                name='kp_cb_process_no_idx',
            ),
        ]

    def __str__(self):
        sign = '+' if self.is_inflow else '-'
        return (
            f"{self.cashbox.name if self.cashbox_id else '?'} "
            f"{sign}{self.amount} {self.currency} ({self.get_movement_type_display()})"
        )

    @property
    def is_inflow(self) -> bool:
        """Hareket kasaya giriş yönünde mi?"""
        return self.movement_type in (
            self.MovementType.INCOME,
            self.MovementType.TRANSFER_IN,
            self.MovementType.DAILY_OPEN,
        )

    @property
    def is_outflow(self) -> bool:
        """Hareket kasadan çıkış yönünde mi?"""
        return self.movement_type in (
            self.MovementType.EXPENSE,
            self.MovementType.TRANSFER_OUT,
            self.MovementType.REVERSAL,
        )

    @property
    def signed_amount(self) -> Decimal:
        """Bakiye etkisi (giriş +, çıkış -)."""
        if self.is_inflow:
            return self.amount
        if self.is_outflow:
            return -self.amount
        # DAILY_CLOSE → düzeltme; pozitif/negatif olabilir, signed_amount
        # alanı bu durumda amount olarak kabul edilir (kuyumcu yorumlar).
        return self.amount


# ============================================================================
# GELİR/GİDER DEFTERİ (IncomeExpenseLedger)  ←  FAZ 14
# ============================================================================

class IncomeExpenseLedger(models.Model):
    """
    Mağaza P&L (Kâr-Zarar) Defteri — CustomerLedger ile paralel kayıt.

    NEDEN GEREKLİ:
        - CustomerLedger.DISCOUNT/FX_GAIN/WRITEOFF kayıtları müşteri
          carisini kapatır ama "mağaza ne kadar zarar etti?" sorusuna
          cevap vermez.
        - Mağaza yöneticisinin aylık gelir-gider tablosunda iskonto
          zararları, kur farkı kayıpları/kazançları, şüpheli alacak
          silmeleri ayrı bir defterden okunmalıdır.
        - Bu defter, ApprovalService.approve_entry tarafından otomatik
          beslenir; CustomerLedger satırı onaylandığında eşleşik bir
          IncomeExpenseLedger satırı yazılır.

    YAZIM AKIŞI:
        1. CollectionService → CustomerLedger.DISCOUNT (requires_approval=True)
        2. Müdür onaylar → ApprovalService.approve_entry()
        3. approve_entry içindeki post-approval hook →
           IncomeExpenseLedger.objects.create(...)

    KURALLAR:
        - Append-Only: kayıtlar güncellenmez; iptal için karşı kayıt yazılır.
        - related_customer_ledger üzerinden CustomerLedger satırına bağlanır.
        - amount_eur daima POZİTİF; yön entry_type ile belirlenir.
    """

    class EntryType(models.TextChoices):
        DISCOUNT_EXPENSE   = 'DISCOUNT_EXPENSE',   _('İskonto Gideri (Mağaza Kaybı)')
        FX_LOSS_EXPENSE    = 'FX_LOSS_EXPENSE',    _('Kur Farkı Zararı')
        FX_GAIN_INCOME     = 'FX_GAIN_INCOME',     _('Kur Farkı Karı')
        WRITEOFF_EXPENSE   = 'WRITEOFF_EXPENSE',   _('Şüpheli Alacak Silme')
        COMMISSION_EXPENSE = 'COMMISSION_EXPENSE', _('POS Komisyon Gideri')
        OTHER_INCOME       = 'OTHER_INCOME',       _('Diğer Gelir')
        OTHER_EXPENSE      = 'OTHER_EXPENSE',      _('Diğer Gider')

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Mağaza ---
    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='income_expense_entries',
        verbose_name=_('Mağaza'),
    )

    # --- Kayıt Tipi ve Tutar ---
    entry_type = models.CharField(
        _('Kayıt Tipi'),
        max_length=25,
        choices=EntryType.choices,
        db_index=True,
    )
    amount_eur = models.DecimalField(
        _('Tutar (TL)'),
        max_digits=18,
        decimal_places=2,
        help_text="Daima pozitif; yön entry_type ile belirlenir.",
    )
    amount_hs = models.DecimalField(
        _('Tutar (HS)'),
        max_digits=18,
        decimal_places=3,
        default=Decimal('0'),
        help_text=(
            "Altın bazlı zararlar için HS karşılığı. "
            "Pure TL kayıtlarda 0 kalır."
        ),
    )
    exchange_rate_eur = models.DecimalField(
        _('Has Kuru (1 gr HS = X TL)'),
        max_digits=12,
        decimal_places=4,
        default=Decimal('0'),
        help_text="Yazım anındaki has kuru, sabitlenir.",
    )

    # --- İlişkili Kayıtlar ---
    related_customer_ledger = models.ForeignKey(
        'customers.CustomerLedger',
        on_delete=models.PROTECT,
        related_name='income_expense_entries',
        null=True,
        blank=True,
        verbose_name=_('İlişkili Cari Hareket'),
        help_text=(
            "DISCOUNT/FX_GAIN/FX_LOSS/WRITEOFF tipinde bir CustomerLedger "
            "kaydının onaylanması sonucunda yazıldıysa, kaynak satır."
        ),
    )
    related_payment = models.ForeignKey(
        'process.Payment',
        on_delete=models.SET_NULL,
        related_name='income_expense_entries',
        null=True,
        blank=True,
        verbose_name=_('İlişkili Ödeme'),
        help_text=(
            "POS komisyon gideri gibi Payment'a bağlı kalemler için."
        ),
    )

    # --- Gider Kategorisi (FAZ 61 — Hızlı Gider Modülü) ---
    # Sadece OTHER_EXPENSE / OTHER_INCOME tipinde manuel gider kalemlerinde
    # doldurulur. NULL = "Kategorisiz" (eski kayıtlar). SET_NULL koruması
    # ile kategori silinse bile gider kayıtları kaybolmaz.
    expense_category = models.ForeignKey(
        'banking.ExpenseCategory',
        on_delete=models.SET_NULL,
        related_name='ledger_entries',
        null=True,
        blank=True,
        verbose_name=_('Gider Kategorisi'),
        help_text=(
            "OTHER_EXPENSE / OTHER_INCOME tipi manuel gider kalemleri için "
            "operasyonel kategori (Yemek, Kargo, Atölye vb.). Diğer entry "
            "tiplerinde NULL kalır."
        ),
    )

    # --- İptal Karşı Girişi ---
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        related_name='reversals',
        null=True,
        blank=True,
        verbose_name=_('Orijinal Satır (REVERSAL için)'),
    )
    is_reversed = models.BooleanField(
        _('İptal Edildi mi?'),
        default=False,
        help_text=(
            "Aynı kayda karşı bir REVERSAL satırı yazıldıysa True. "
            "Append-Only ihlali değildir; sadece denormalize bayrak."
        ),
    )

    # --- Audit / İz ---
    description = models.CharField(_('Açıklama'), max_length=255, blank=True, default='')
    created_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='income_expense_entries',
        verbose_name=_('Yazan Kullanıcı'),
    )
    created_on = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default='')

    class Meta:
        db_table = 'KP_IncomeExpenseLedger'
        verbose_name = _('Gelir/Gider Defteri Satırı')
        verbose_name_plural = _('Gelir/Gider Defteri Satırları')
        ordering = ['-created_on']
        indexes = [
            models.Index(
                fields=['store', 'entry_type', 'created_on'],
                name='kp_iel_store_type_date_idx',
            ),
            models.Index(
                fields=['related_customer_ledger'],
                name='kp_iel_cl_idx',
            ),
            # FAZ 61: Kategori bazlı raporlama sorguları için
            models.Index(
                fields=['expense_category', 'created_on'],
                name='kp_iel_cat_date_idx',
            ),
        ]

    def __str__(self):
        sign = '+' if self.is_income else '-'
        return f"{self.get_entry_type_display()} {sign}{self.amount_eur} TL"

    # --- Sınıflandırma Yardımcıları ---
    INCOME_TYPES = (
        EntryType.FX_GAIN_INCOME,
        EntryType.OTHER_INCOME,
    )
    EXPENSE_TYPES = (
        EntryType.DISCOUNT_EXPENSE,
        EntryType.FX_LOSS_EXPENSE,
        EntryType.WRITEOFF_EXPENSE,
        EntryType.COMMISSION_EXPENSE,
        EntryType.OTHER_EXPENSE,
    )

    @property
    def is_income(self) -> bool:
        return self.entry_type in self.INCOME_TYPES

    @property
    def is_expense(self) -> bool:
        return self.entry_type in self.EXPENSE_TYPES

    @property
    def signed_amount_eur(self) -> Decimal:
        """P&L'e etki: gelir +, gider -."""
        if self.is_income:
            return self.amount_eur
        return -self.amount_eur


# ============================================================================
# GİDER KATEGORİSİ (ExpenseCategory) ← FAZ 61 (Hızlı Gider Girişi Modülü)
# ============================================================================

class ExpenseCategory(models.Model):
    """
    Operasyonel gider kategorisi — mağaza bazlı referans tablosu.

    NEDEN GEREKLİ:
        - IncomeExpenseLedger.entry_type sistem odaklı sınıflandırmadır
          (DISCOUNT_EXPENSE, FX_LOSS_EXPENSE, COMMISSION_EXPENSE vb.).
        - Kuyumcunun günlük operasyonel kayıtlarında "Yemek 220 TL",
          "Kargo 320 TL", "Atölye 1080 TL" gibi kategoriler için ayrı bir
          referans veri katmanı gerekiyor.
        - Bu model, IncomeExpenseLedger.expense_category FK üzerinden
          OTHER_EXPENSE / OTHER_INCOME satırlarına bağlanır; mevcut sistem
          tipleri (FX_LOSS_EXPENSE vb.) ile çakışmaz.

    KURALLAR:
        - store FK ile mağaza-izole. Bir mağazanın kategorisi başka mağazada
          görünmez.
        - is_system_preset=True olan kayıtlar populate komutu tarafından
          oluşturulur ve UI'da silinemez (sadece deaktif edilebilir).
        - Soft-delete pattern'i: is_active=False kategoriler dropdown'larda
          görünmez ama mevcut ledger satırları korunur.

    YAZIM:
        - Manuel: kategori yönetim sayfasından kullanıcı oluşturur.
        - Sistem preset: populate_expense_categories management komutu.
    """

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Mağaza İzolasyonu ---
    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='expense_categories',
        verbose_name=_('Mağaza'),
    )

    # --- Görünüm Alanları ---
    name = models.CharField(
        _('Kategori Adı'),
        max_length=100,
        help_text="Yemek, Kargo, Atölye, Kırtasiye vb.",
    )
    short_code = models.CharField(
        _('Kısa Kod'),
        max_length=10,
        blank=True,
        default='',
        help_text=(
            "Hızlı giriş için 2-5 harfli kısaltma (örn. YMK, KRG, ATL). "
            "Klavye odaklı arayüzde aramayı hızlandırır."
        ),
    )
    icon = models.CharField(
        _('İkon'),
        max_length=30,
        blank=True,
        default='',
        help_text="Bootstrap Icons sınıf adı (örn. 'bi-truck', 'bi-cup-hot').",
    )
    color_css = models.CharField(
        _('Renk (CSS)'),
        max_length=20,
        blank=True,
        default='',
        help_text="UI rozeti için CSS renk kodu (#0d47a1) veya bootstrap sınıfı.",
    )
    display_order = models.PositiveSmallIntegerField(
        _('Sıralama'),
        default=100,
        help_text="Dropdown ve listelerde gösterim sırası (küçük = üstte).",
    )

    # --- Durum Bayrakları ---
    is_active = models.BooleanField(
        _('Aktif'),
        default=True,
        db_index=True,
        help_text="False ise dropdown'larda gizlenir; mevcut kayıtlar korunur.",
    )
    is_system_preset = models.BooleanField(
        _('Sistem Preseti'),
        default=False,
        help_text=(
            "True ise populate_expense_categories komutu tarafından oluşturulmuş "
            "demektir; UI'dan silinemez (sadece deaktif edilebilir)."
        ),
    )

    # --- Audit ---
    created_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_expense_categories',
        verbose_name=_('Oluşturan'),
    )
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'KP_ExpenseCategory'
        verbose_name = _('Gider Kategorisi')
        verbose_name_plural = _('Gider Kategorileri')
        ordering = ['display_order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['store', 'name'],
                name='kp_expense_cat_store_name_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['store', 'is_active', 'display_order'],
                name='kp_expense_cat_lookup_idx',
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.store_id})"
