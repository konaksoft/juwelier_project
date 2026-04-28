import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone


# ============================================================================
# FAZ R-3: GÜNLÜK MAĞAZA RAPOR ÖZETİ (Rollup / Cache Tablosu)
# ============================================================================

class DailyStoreReport(models.Model):
    """
    Günlük mağaza bazında hesaplanmış rapor özeti.

    Bu tablo bir CACHE tablosudur. Gerçek kaynak Process + Payment tablolarıdır.
    Celery task'ı ile periyodik olarak hesaplanır/güncellenir.

    Kullanım:
        - Dashboard KPI kartları bu tablodan < 5ms'de okunur.
        - Tarih aralığı grafikleri bu tablodan çekilir.
        - Geçmiş günler gece 02:05'te, bugün 15dk'da bir güncellenir.

    Kurallar:
        - Mevcut tablolara (Process, Payment, StockLedger) dokunulmaz.
        - UPSERT mantığı ile çalışır (store + report_date unique).
        - Canlı işlem tablolarını kilitlemez (READ COMMITTED).

    FAZ A GÜNCELLEMESİ (Çoklu Maden/Ürün Entegrasyonu):
        Gümüş, Pırlanta ve Saat stok değerleri için yeni alanlar eklendi.
        Bu alanlar null=True olarak başlatıldı; mevcut satırlar bozulmaz.
        Rollup hesaplamasında Products.material_type filtresi ile conditional
        aggregation (tek sorgu, N+1 yok) kullanılacaktır.
    """

    id = models.BigAutoField(primary_key=True)

    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='daily_reports',
        verbose_name='Mağaza',
    )

    report_date = models.DateField(
        verbose_name='Rapor Tarihi',
        db_index=True,
    )

    # --- Satış Metrikleri ---
    total_sales_tl = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Toplam Satış (TL)',
    )
    total_sales_hs = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0.0000'),
        verbose_name='Toplam Satış (Has)',
    )
    sale_count = models.IntegerField(
        default=0, verbose_name='Satış Adet',
    )
    unique_customers = models.IntegerField(
        default=0, verbose_name='Tekil Müşteri Sayısı',
    )

    # --- Alış Metrikleri ---
    total_purchases_tl = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Toplam Alış (TL)',
    )
    total_purchases_hs = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0.0000'),
        verbose_name='Toplam Alış (Has)',
    )
    purchase_count = models.IntegerField(
        default=0, verbose_name='Alış Adet',
    )

    # --- İade Metrikleri ---
    total_returns_tl = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Toplam İade (TL)',
    )
    return_count = models.IntegerField(
        default=0, verbose_name='İade Adet',
    )

    # --- Kâr Metrikleri ---
    total_gross_profit = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Toplam Brüt Kâr (TL)',
    )
    total_net_profit = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Toplam Net Kâr (TL)',
    )

    # --- Kasa Metrikleri (Payment tablosundan) ---
    cash_in = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Nakit Giriş (TL)',
    )
    cash_out = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Nakit Çıkış (TL)',
    )
    card_in = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Kredi Kartı Giriş (TL)',
    )
    card_out = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Kredi Kartı Çıkış (TL)',
    )
    transfer_in = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Havale Giriş (TL)',
    )
    transfer_out = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Havale Çıkış (TL)',
    )
    commission_total = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Toplam Komisyon (TL)',
    )

    # --- Personel Metrikleri ---
    transaction_count = models.IntegerField(
        default=0, verbose_name='Toplam İşlem Sayısı',
    )

    # --- Stok Değer Snapshot (Altın - Mevcut) ---
    stock_value_tl = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        verbose_name='Stok Değeri (WAC TL)',
        help_text='Gün sonunda StockSnapshot WAC bazlı toplam stok değeri.',
    )
    stock_value_hs = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0.0000'),
        verbose_name='Stok Değeri (WAC Has)',
    )

    # ========================================================================
    # FAZ A: ÇOKLU MADEN/ÜRÜN ENTEGRASYONU - YENİ METRİK ALANLARI
    # ========================================================================
    # Aşağıdaki tüm alanlar null=True olarak eklenmiştir. Mevcut DailyStoreReport
    # satırlarındaki altın verileri bozulmaz. Celery rollup task'ı yeni alanları
    # Products.material_type filtresi ile conditional aggregation kullanarak
    # tek sorguda hesaplayacaktır (N+1 yok).

    # --- Gümüş Stok ---
    silver_stock_gram = models.DecimalField(
        max_digits=14, decimal_places=4,
        null=True, blank=True, default=None,
        verbose_name='Gümüş Stok (Gram)',
        help_text='Material_type=SILVER ürünlerin toplam gramı.',
    )
    silver_stock_value_hg = models.DecimalField(
        max_digits=14, decimal_places=4,
        null=True, blank=True, default=None,
        verbose_name='Gümüş Stok Değeri (Has Gümüş)',
        help_text='Material_type=SILVER WAC Has Gümüş cinsinden toplam değer.',
    )
    silver_stock_value_tl = models.DecimalField(
        max_digits=18, decimal_places=2,
        null=True, blank=True, default=None,
        verbose_name='Gümüş Stok Değeri (TL)',
        help_text='Material_type=SILVER WAC TL cinsinden toplam değer.',
    )

    # --- Pırlanta Stok ---
    diamond_stock_pieces = models.IntegerField(
        null=True, blank=True, default=None,
        verbose_name='Pırlanta Stok (Adet)',
        help_text='Material_type=DIAMOND ürünlerin toplam adedi.',
    )
    diamond_stock_value_tl = models.DecimalField(
        max_digits=18, decimal_places=2,
        null=True, blank=True, default=None,
        verbose_name='Pırlanta Stok Değeri (TL)',
        help_text='Material_type=DIAMOND WAC TL cinsinden toplam değer.',
    )

    # --- Saat Stok ---
    watch_stock_pieces = models.IntegerField(
        null=True, blank=True, default=None,
        verbose_name='Saat Stok (Adet)',
        help_text='Material_type=WATCH ürünlerin toplam adedi.',
    )
    watch_stock_value_tl = models.DecimalField(
        max_digits=18, decimal_places=2,
        null=True, blank=True, default=None,
        verbose_name='Saat Stok Değeri (TL)',
        help_text='Material_type=WATCH WAC TL cinsinden toplam değer.',
    )

    # --- Meta ---
    computed_at = models.DateTimeField(
        verbose_name='Hesaplandığı Zaman',
        help_text='Son hesaplama zamanı. Delta güncellemeler bu alanı günceller.',
    )

    class Meta:
        db_table = 'DailyStoreReport'
        verbose_name = 'Günlük Mağaza Raporu'
        verbose_name_plural = 'Günlük Mağaza Raporları'
        ordering = ['-report_date']
        constraints = [
            models.UniqueConstraint(
                fields=['store', 'report_date'],
                name='dailyreport_unique_store_date',
            ),
        ]
        indexes = [
            models.Index(fields=['store', 'report_date'], name='dailyrep_store_date_idx'),
            models.Index(fields=['report_date'], name='dailyrep_date_idx'),
            models.Index(fields=['-computed_at'], name='dailyrep_computed_idx'),
        ]

    def __str__(self):
        return f"[{self.store}] {self.report_date} | Satış: {self.total_sales_tl} TL"

    @property
    def net_cash_flow(self) -> Decimal:
        """Net nakit akışı: Toplam giriş - Toplam çıkış."""
        total_in = self.cash_in + self.card_in + self.transfer_in
        total_out = self.cash_out + self.card_out + self.transfer_out
        return total_in - total_out

    @property
    def net_sales(self) -> Decimal:
        """Net satış: Satış - Alış - İade."""
        return self.total_sales_tl - self.total_purchases_tl - self.total_returns_tl


# ============================================================================
# FAZ R-4: PERSONEL GÜNLÜK PERFORMANS (Rollup)
# ============================================================================

class DailyEmployeeReport(models.Model):
    """
    Personel bazında günlük performans özeti.
    DailyStoreReport ile aynı Celery task'ı tarafından hesaplanır.
    """

    id = models.BigAutoField(primary_key=True)

    store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.CASCADE,
        related_name='daily_employee_reports',
    )
    employee = models.ForeignKey(
        'accounts.Users',
        on_delete=models.CASCADE,
        related_name='daily_reports',
        verbose_name='Personel',
    )
    report_date = models.DateField(db_index=True)

    # --- Satış ---
    sale_count = models.IntegerField(default=0)
    total_sales_tl = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
    )
    total_sales_hs = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal('0.0000'),
    )
    total_gross_profit = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
    )

    # --- Alış ---
    purchase_count = models.IntegerField(default=0)
    total_purchases_tl = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
    )

    # --- Toplam ---
    transaction_count = models.IntegerField(default=0)

    computed_at = models.DateTimeField()

    class Meta:
        db_table = 'DailyEmployeeReport'
        verbose_name = 'Personel Günlük Raporu'
        verbose_name_plural = 'Personel Günlük Raporları'
        ordering = ['-report_date']
        constraints = [
            models.UniqueConstraint(
                fields=['store', 'employee', 'report_date'],
                name='daily_emp_unique_store_emp_date',
            ),
        ]
        indexes = [
            models.Index(
                fields=['store', 'report_date'],
                name='daily_emp_store_date_idx',
            ),
            models.Index(
                fields=['employee', 'report_date'],
                name='daily_emp_emp_date_idx',
            ),
        ]

    def __str__(self):
        return f"[{self.employee}] {self.report_date} | {self.sale_count} satış"


class GeneratedReports(models.Model):
    """Arka planda Celery ile üretilen raporları saklar."""

    STATUS_CHOICES = [
        ('PENDING', 'Beklemede'),
        ('SUCCESS', 'Tamamlandı'),
        ('FAILED', 'Hata'),
    ]

    task_id = models.CharField(max_length=255, unique=True, verbose_name="Celery Task ID")
    report_type = models.CharField(max_length=64, verbose_name="Rapor Tipi")
    file = models.FileField(upload_to='reports/%Y/%m/', null=True, blank=True, verbose_name="PDF Dosyası")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(default=timezone.now)
    error_message = models.TextField(null=True, blank=True, verbose_name="Hata Mesajı")

    class Meta:
        db_table = 'GeneratedReports'
        verbose_name = "Üretilen Rapor"
        verbose_name_plural = "Üretilen Raporlar"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task_id'], name='genrep_task_id_idx'),
            models.Index(fields=['status'], name='genrep_status_idx'),
            models.Index(fields=['-created_at'], name='genrep_created_idx'),
        ]

    def __str__(self):
        return f"{self.report_type} - {self.task_id} ({self.status})"
