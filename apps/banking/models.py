# ============================================================================
# DOSYA: apps/banking/models.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v3 — Mutabakat Altyapısı (Faz 1)
#
# DEĞİŞİKLİK ÖZETİ (v2 → v3):
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

    # --- Fatura Bağlantısı ---
    invoice = models.ForeignKey(
        'invoices.Invoice',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='bank_transactions',
        verbose_name=_('Bağlı Fatura'),
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
    ):
        """
        Health check sonucunu kaydeder. Yalnızca ilgili alanları günceller (update_fields).

        Kullanım:
            cred.update_health('OK', efatura=True, banking=True, esurec_uuid='uuid-str')
            cred.update_health('ERROR')
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

        self.save(update_fields=update_fields)
