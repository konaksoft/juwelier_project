from apps.accounts.models import Users
import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.accounts.models import Users
from apps.customers.models import Customers
from apps.products.models import Products
from apps.stores.models import Stores
from apps.suppliers.models import Suppliers


class ProcessGroup(models.Model):
    """Her fiş/işlem grubunu (process_no) temsil eder.
    Process ve Payment kayıtları buraya UUID FK ile bağlanarak tam referans bütünlüğü sağlanır.
    Mevcut process_no alanları geriye dönük uyum için korunmaktadır."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    process_no = models.CharField(max_length=15, unique=True)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.process_no

    class Meta:
        db_table = 'ProcessGroup'
        indexes = [
            models.Index(fields=['store'], name='processgroup_store_idx'),
            models.Index(fields=['-created_at'], name='processgroup_created_at_idx'),
        ]


# apps/process/models.py

class Process(models.Model):
    PAYMENT_TYPE_CHOICES = [
        ('CREDIT_CARD', 'Kredi Kartı'),
        ('CASH', 'Nakit'),
        ('TRANSFER', 'Havale'),
        ('COMMISSION', 'Komisyon'),
    ]

    PROCESS_TYPE_CHOICES = [
        ('RETAIL', 'Perakende'),
        ('WHOLESALE', 'Toptan'),
        ('FAST_PROCESS', 'Hızlı İşlem'),
    ]

    TRANSACTION_TYPE_CHOICES = [
        ('STOCK_IN', 'Stok Girişi'),
        ('ORDER_IN', 'Sipariş'),
        ('PURCHASE', 'Alış'),
        ('SALE', 'Satış'),
        ('RETURN', 'İade'),
        ('PAYMENT', 'Ödeme'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Beklemede'),
        ('OPEN_BINDING', 'Açıktan Bağlama'),
        ('COMPLETED', 'Tamamlandı'),
        ('IN_PROGRESS', 'Tamamlanmadı'),
        ('CANCELED', 'İptal Edildi'),
        ('WAITING_STOCK', 'Bekleyen Stok'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    process_no = models.CharField(max_length=15, null=True, blank=True)
    process_group = models.ForeignKey(
        ProcessGroup,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='process_items',
        verbose_name="İşlem Grubu",
    )

    customer = models.ForeignKey(Customers, on_delete=models.SET_NULL, null=True, blank=True)
    supplier = models.ForeignKey(Suppliers, on_delete=models.SET_NULL, null=True, blank=True)

    process_type = models.CharField(max_length=150, choices=PROCESS_TYPE_CHOICES, default='RETAIL')
    transaction_type = models.CharField(max_length=150, choices=TRANSACTION_TYPE_CHOICES, default='SALE')

    employee = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name='employee')
    product = models.ForeignKey(Products, on_delete=models.SET_NULL, null=True, blank=True)

    # --- FAZ 23: Kasa/Ödeme sepet kalemi desteği ---
    bank_account = models.ForeignKey(
        'banking.BankAccount',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='process_items',
        verbose_name="Banka Hesabı (Kasa)",
        help_text="Kasa/ödeme sepet kalemleri için. Dolu ise bu satır ürün değil kasa çıkışıdır.",
    )
    payment_currency = models.CharField(
        max_length=10, null=True, blank=True,
        verbose_name="Ödeme Para Birimi",
        help_text="FX kasası için döviz kodu (USD, EUR vb.). Normal kasalarda boş kalır.",
    )

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)

    piece = models.IntegerField(default=0)
    gram = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    # --- KAR ALANLARI GÜNCELLEMESİ BAŞLANGIÇ ---
    # Eski profit alanı yerine iki yeni alan eklendi:

    gross_profit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Brüt Ticari Kâr (Cebine Giren)"
    )

    net_profit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Net Kâr (KDV/Vergi Düşülmüş)"
    )

    # İşlem tamamlandığında mühürlenen maliyet (veri güvenliği için tersten hesaplama yerine)
    cost_amount_tl = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=Decimal('0.00'),
        null=True,
        blank=True,
        verbose_name="Maliyet (TL)"
    )
    cost_amount_hs = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal('0.000'),
        null=True,
        blank=True,
        verbose_name="Maliyet (Has)"
    )
    # --- KAR ALANLARI GÜNCELLEMESİ BİTİŞ ---

    process_mileage = models.CharField(max_length=50, default=0)

    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    price_hs = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True, null=True)
    hs_rate_sale_tl = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    hs_rate_buy_tl = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    karat = models.PositiveSmallIntegerField(null=True, blank=True)

    labor_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        null=True, blank=True, help_text="Özel matrah kapsamında brüt işçilik toplamı (TL)."
    )

    waiting_stock = models.BooleanField(default=False)

    invoice_no = models.CharField(max_length=64, null=True, blank=True, verbose_name="Fatura No")
    invoice_url = models.CharField(max_length=255, null=True, blank=True, verbose_name="Fatura Linki")

    is_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='IN_PROGRESS')
    date = models.DateTimeField(default=timezone.now)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"Process - {self.id}"

    class Meta:
        db_table = 'Process'
        indexes = [
            models.Index(fields=['process_no'], name='process_no_idx'),
            models.Index(fields=['process_group'], name='process_process_group_idx'),
            models.Index(fields=['store', 'is_deleted', 'is_status'], name='proc_store_del_stat_idx'),
            models.Index(fields=['store', 'date'], name='process_store_date_idx'),
            models.Index(fields=['store', 'transaction_type'], name='process_store_txtype_idx'),
            models.Index(fields=['customer'], name='process_customer_idx'),
            models.Index(fields=['employee'], name='process_employee_idx'),
            models.Index(fields=['is_deleted'], name='process_is_deleted_idx'),
            models.Index(fields=['-date'], name='process_date_desc_idx'),
        ]


class Payment(models.Model):
    """
    Parçalı ödeme kaydı.

    v3 Mutabakat Eklentileri:
        - ReconciliationStatus enum: Mutabakat yaşam döngüsü
        - bank_account FK: Ödemenin hangi POS/banka hesabına gittiği
        - matched_bank_transaction FK: Eşleşen gerçek banka hareketi
        - reconciliation_diff: Banka tutarı - İç kayıt tutarı
        - reconciled_at / reconciled_by: Mutabakat meta verisi

    Kurallar:
        - payment_type CREDIT_CARD veya TRANSFER ise bank_account zorunludur (Faz 2'de view'da enforce)
        - payment_type CASH veya COMMISSION ise reconciliation_status = NOT_REQUIRED kalır
    """

    PAYMENT_TYPE_CHOICES = [
        ('CASH', 'Nakit'),
        ('CREDIT_CARD', 'Kredi Kartı'),
        ('TRANSFER', 'Havale / EFT'),
        ('COMMISSION', 'Komisyon'),
        ('ADJUSTMENT', 'Bakiye Düzeltme / Açılış'),  # FAZ 19
    ]

    class ReconciliationStatus(models.TextChoices):
        NOT_REQUIRED = 'NOT_REQUIRED', 'Mutabakat Gerekmez'
        PENDING      = 'PENDING',      'Bekliyor'
        MATCHED      = 'MATCHED',      'Eşleşti'
        PARTIAL      = 'PARTIAL',      'Kısmi Eşleşme'
        DISCREPANCY  = 'DISCREPANCY',  'Tutar Uyuşmazlığı'
        MANUAL       = 'MANUAL',       'Manuel Teyit Edildi'

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- İşlem Bağlantısı (Mevcut) ---
    process_no = models.CharField(max_length=15, null=True, blank=True)
    process_group = models.ForeignKey(
        ProcessGroup,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='payments',
        verbose_name="İşlem Grubu",
    )

    # --- Ödeme Bilgileri (Mevcut) ---
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    date = models.DateTimeField(default=timezone.now)
    is_output = models.BooleanField(default=False)
    installment = models.IntegerField(default=1, verbose_name="Taksit Sayısı", null=True, blank=True)
    reference = models.CharField(max_length=100, verbose_name="POS Referans/Ref No", null=True, blank=True)

    # --- v4: POS Komisyon Alanları ---
    commission_rate_applied = models.DecimalField(
        "Uygulanan Komisyon Oranı (%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Ödeme anında POS komisyon oranı.",
    )
    commission_amount = models.DecimalField(
        "Komisyon Tutarı (TL)",
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="amount * commission_rate / 100",
    )
    net_amount = models.DecimalField(
        "Net Tutar (Bankaya Gelen)",
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="amount - commission_amount. Mutabakat bu tutara göre yapılır.",
    )
    maturity_date = models.DateField(
        "Vade Tarihi",
        null=True,
        blank=True,
        help_text="Tutarın banka hesabına geçeceği tahmini tarih.",
    )

    # --- v3: Banka/POS Hesabı Bağlantısı ---
    bank_account = models.ForeignKey(
        'banking.BankAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        verbose_name="Banka/POS Hesabı",
        help_text=(
            "CREDIT_CARD ise POS terminali, TRANSFER ise banka hesabı seçilir. "
            "CASH ve COMMISSION için boş kalır."
        ),
    )

    # --- v3: Mutabakat Durumu ---
    reconciliation_status = models.CharField(
        "Mutabakat Durumu",
        max_length=15,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.NOT_REQUIRED,
        help_text=(
            "NOT_REQUIRED: Nakit/komisyon — mutabakat gerekmez. "
            "PENDING: Banka teyidi bekleniyor. "
            "MATCHED: Banka hareketi ile eşleşti. "
            "PARTIAL: Kısmi tutar eşleşmesi. "
            "DISCREPANCY: Tutar uyuşmazlığı (banka komisyonu aşımı). "
            "MANUAL: Personel tarafından manuel teyit edildi."
        ),
    )

    # --- v3: Eşleşen Banka Hareketi ---
    matched_bank_transaction = models.ForeignKey(
        'banking.BankTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='matched_payments',
        verbose_name="Eşleşen Banka Hareketi",
        help_text=(
            "ReconciliationService tarafından doldurulur. "
            "Payment (iç kayıt) ile BankTransaction (dış kayıt) arasındaki bağlantı."
        ),
    )

    # --- v3: Mutabakat Meta Verisi ---
    reconciliation_diff = models.DecimalField(
        "Mutabakat Farkı (TL)",
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Banka tutarı - İç kayıt tutarı. "
            "Pozitif = bankaya fazla gelmiş, Negatif = eksik gelmiş (komisyon). "
            "Debugging ve raporlama için saklanır."
        ),
    )
    reconciled_at = models.DateTimeField(
        "Mutabakat Zamanı",
        null=True,
        blank=True,
    )
    reconciled_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reconciled_payments',
        verbose_name="Mutabakatı Yapan",
        help_text="Otomatik eşleşmelerde NULL kalır, manuel eşleşmelerde personel kaydedilir.",
    )

    # --- v5: İptal (Cancellation) Alanları ---
    is_cancelled = models.BooleanField(
        "İptal Edildi",
        default=False,
        help_text=(
            "True ise bu ödeme kaydı iptal edilmiştir. "
            "Bakiye hesaplamaları ve raporlarda dikkate alınmaz."
        ),
    )
    cancelled_at = models.DateTimeField(
        "İptal Zamanı",
        null=True,
        blank=True,
        help_text="İptal işleminin gerçekleştiği tarih ve saat.",
    )

    # --- v6: Çoklu Para Birimi (Multi-Currency) Alanları ---
    # Kasanın para birimi TRY olmadığında (USD, EUR vb.),
    # amount alanı HER ZAMAN TL tutarını saklar.
    # currency_amount → döviz cinsinden gerçek tutarı saklar.
    # exchange_rate → işlem anındaki TRY/döviz kurunu saklar.
    # Formül: currency_amount = amount / exchange_rate
    currency_amount = models.DecimalField(
        "Döviz Tutarı",
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Kasanın para birimi TRY değilse, döviz cinsinden gerçek tutar. "
            "Örn: amount=13887 TL, exchange_rate=46.29 → currency_amount=300 USD. "
            "TRY kasaları için NULL kalır."
        ),
    )
    exchange_rate = models.DecimalField(
        "Döviz Kuru",
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text=(
            "İşlem anındaki TRY/Döviz kuru. "
            "Örn: 1 USD = 46.29 TL → exchange_rate=46.29. "
            "TRY kasaları için NULL kalır."
        ),
    )

    # --- v7: Onaylı Kasa (Safe Approval) ---
    # Mağaza ayarlarında is_safe_approval_required AÇIK ise, yeni Payment
    # kayıtları is_approved=False olarak oluşturulur. Bakiye hesaplamalarına
    # dahil edilmez. Yönetici onayı ile is_approved=True olur.
    # Ayar KAPALI ise doğrudan is_approved=True kaydedilir (mevcut davranış).
    is_approved = models.BooleanField(
        "Onaylandı",
        default=True,
        help_text=(
            "False ise bu ödeme yönetici onayı beklemektedir. "
            "Bakiye hesaplamalarına dahil edilmez. "
            "Onaylandığında True olur ve bakiyeye yansır."
        ),
    )

    # --- v8: Transfer Personel & Açıklama ---
    performed_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='performed_payments',
        verbose_name="İşlemi Yapan Personel",
        help_text=(
            "Ödemeyi veya transferi gerçekleştiren kullanıcı. "
            "reconciled_by ile karıştırılmamalıdır (o mutabakat içindir)."
        ),
    )
    notes = models.TextField(
        "Açıklama / Not",
        null=True,
        blank=True,
        help_text=(
            "İşlem hakkında serbest metin açıklama. "
            "reference alanı POS referans kodu için ayrılmıştır; "
            "uzun açıklamalar bu alana yazılır."
        ),
    )

    def __str__(self):
        return f"{self.get_payment_type_display()} – {self.amount}"

    class Meta:
        db_table = 'Payment'
        indexes = [
            models.Index(fields=['process_no'], name='payment_process_no_idx'),
            models.Index(fields=['process_group'], name='payment_process_group_idx'),
            models.Index(fields=['date'], name='payment_date_idx'),
            models.Index(
                fields=['bank_account', 'reconciliation_status'],
                name='payment_bank_recon_idx',
            ),
            models.Index(
                fields=['reconciliation_status', 'date'],
                name='payment_recon_date_idx',
            ),
            models.Index(
                fields=['is_cancelled'],
                name='payment_cancelled_idx',
            ),
            models.Index(
                fields=['is_approved'],
                name='payment_approved_idx',
            ),
        ]
