# ============================================================================
# DOSYA: apps/store_transfers/models.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v1 — FAZ 45: Çoklu Şube Transfer Altyapısı
#
# AMAÇ:
#   Şubeler arası nakit/döviz/HS ve stok transferlerinin yaşam
#   döngüsünü orkestre eden ana model çiftidir. Mevcut CashboxLedger
#   (TRANSFER_IN/TRANSFER_OUT) ve StockLedger (paired_entry FK) altyapısı
#   üzerine inşa edilir; transfer durumu, kalemleri ve audit trail'i
#   bu tabloda tutulur.
#
# DURUM MAKİNESİ:
#   DRAFT → IN_TRANSIT → ACCEPTED
#                     → REJECTED
#                     → PARTIALLY_ACCEPTED
#   DRAFT → CANCELLED  (ledger'a yazmadan iptal)
#
# DEFTER İLİŞKİSİ:
#   IN_TRANSIT'e geçişte:
#     - Kaynak kasa CashboxLedger.TRANSFER_OUT
#     - Kaynak transit hesap CashboxLedger.TRANSFER_IN
#     - Stok için kaynak StockLedger.TRANSFER_OUT
#   ACCEPTED'a geçişte:
#     - Kaynak transit CashboxLedger.TRANSFER_OUT
#     - Hedef kasa CashboxLedger.TRANSFER_IN
#     - Hedef StockLedger.TRANSFER_IN (paired_entry ile kaynak ledger'a bağlı)
#   REJECTED'a geçişte:
#     - Kaynak transit CashboxLedger.TRANSFER_OUT
#     - Kaynak kasa CashboxLedger.REVERSAL (giriş)
#     - Stok için kaynak StockLedger.REVERSAL
#
# ÖNEMLİ: Bu dosya FAZ 45'te yalnızca ŞEMAYI tanımlar.
#         Aktif servis katmanı (StoreTransferService) FAZ 46'da yazılacaktır.
# ============================================================================

import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class StoreTransfer(models.Model):
    """Şubeler arası transferin ana kaydı (header).

    Bir transfer 1+ kalem içerebilir. Tüm kalemler tek bir durum makinesi
    altında yönetilir (PARTIALLY_ACCEPTED hariç — bu durumda kalem-bazlı
    kabul/red bilgisi StoreTransferItem.accepted_* alanlarında tutulur).
    """

    class TransferType(models.TextChoices):
        CASH  = 'CASH',  _('Yalnız Nakit/Döviz/HS')
        STOCK = 'STOCK', _('Yalnız Stok (Ürün/Gram)')
        MIXED = 'MIXED', _('Karma (Nakit + Stok)')

    class Status(models.TextChoices):
        DRAFT               = 'DRAFT',               _('Taslak (Henüz gönderilmedi)')
        IN_TRANSIT          = 'IN_TRANSIT',          _('Yolda (Kabul Bekliyor)')
        ACCEPTED            = 'ACCEPTED',            _('Tamamı Kabul Edildi')
        PARTIALLY_ACCEPTED  = 'PARTIALLY_ACCEPTED',  _('Kısmen Kabul Edildi')
        REJECTED            = 'REJECTED',            _('Reddedildi (Geri Döndü)')
        CANCELLED           = 'CANCELLED',           _('İptal Edildi (DRAFT iptali)')

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- İnsan Okunabilir Numara ---
    transfer_no = models.CharField(
        _('Transfer No'),
        max_length=30,
        unique=True,
        null=True, blank=True,
        db_index=True,
        help_text='TRF-2026-0001 formatında otomatik üretilir. DRAFT aşamasında NULL kalabilir.',
    )

    # --- Şubeler ---
    source_store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.PROTECT,
        related_name='outbound_transfers',
        verbose_name=_('Gönderen Şube'),
    )
    destination_store = models.ForeignKey(
        'stores.Stores',
        on_delete=models.PROTECT,
        related_name='inbound_transfers',
        verbose_name=_('Alan Şube'),
    )

    # --- Tip ve Durum ---
    transfer_type = models.CharField(
        _('Transfer Tipi'),
        max_length=10,
        choices=TransferType.choices,
        default=TransferType.MIXED,
        db_index=True,
    )
    status = models.CharField(
        _('Durum'),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    # --- Aktörler ---
    initiated_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='initiated_transfers',
        verbose_name=_('Transferi Başlatan'),
    )
    dispatched_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='dispatched_transfers',
        verbose_name=_('Yola Çıkaran (DRAFT→IN_TRANSIT)'),
    )
    accepted_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='accepted_transfers',
        verbose_name=_('Kabul Eden'),
    )
    rejected_by = models.ForeignKey(
        'accounts.Users',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='rejected_transfers',
        verbose_name=_('Reddeden'),
    )

    # --- Notlar ---
    notes_sender = models.TextField(
        _('Gönderen Notları'),
        blank=True, default='',
        help_text='Gönderen şubenin transfere eklediği açıklama.',
    )
    notes_receiver = models.TextField(
        _('Alan Şube Notları / Red Sebebi'),
        blank=True, default='',
        help_text='Kabul/red sırasında girilen not. REJECTED durumunda ZORUNLUDUR.',
    )
    notes_admin = models.TextField(
        _('Yönetici Müdahale Notu'),
        blank=True, default='',
        help_text='Süresi geçmiş transferleri patron manuel REJECTED yaparken zorunlu gerekçe.',
    )

    # --- Zaman Çizelgesi ---
    initiated_at = models.DateTimeField(default=timezone.now, verbose_name=_('Taslak Oluşturma Zamanı'))
    dispatched_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Yola Çıkış Zamanı'))
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Tamamlanma Zamanı (ACCEPTED/REJECTED)'))
    expected_arrival_date = models.DateField(
        null=True, blank=True,
        verbose_name=_('Tahmini Varış Tarihi'),
        help_text='Bu tarihten sonra transfer hâlâ IN_TRANSIT ise sistem uyarı üretir.',
    )

    # --- Reversal / İptal Zincirleme ---
    is_reversed = models.BooleanField(
        _('İptal Edildi mi?'),
        default=False,
        help_text='REJECTED veya manuel reversal sonrası True olur. Append-Only ihlali değildir; denormalize bayrak.',
    )
    parent_transfer = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='child_transfers',
        verbose_name=_('Üst Transfer (Kısmi Kabul Sonrası)'),
        help_text='Kısmi kabul sonrasında reddedilen kalemler için yeni bir transfer açılırsa, ana transfere bağlanır.',
    )

    # --- Toplam Özetleri (Denormalize, Cache; Otorite TransferItem) ---
    total_cash_tl_equivalent = models.DecimalField(
        _('Toplam Nakit (TL Karşılığı)'),
        max_digits=18, decimal_places=2,
        default=Decimal('0'),
        help_text='Hızlı liste görünümü için TransferItem toplamı. Otorite TransferItem satırlarıdır.',
    )
    total_stock_hs = models.DecimalField(
        _('Toplam Stok (HS)'),
        max_digits=18, decimal_places=3,
        default=Decimal('0'),
        help_text='Stok kalemlerinin Has-altın karşılığı toplamı (özetleme için).',
    )
    line_count = models.PositiveIntegerField(
        _('Kalem Sayısı'),
        default=0,
        help_text='StoreTransferItem satır sayısı (cache).',
    )

    # --- Audit ---
    created_on = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'StoreTransfers'
        verbose_name = _('Şube Transferi')
        verbose_name_plural = _('Şube Transferleri')
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['source_store', 'status'], name='st_source_status_idx'),
            models.Index(fields=['destination_store', 'status'], name='st_dest_status_idx'),
            models.Index(fields=['status', 'created_on'], name='st_status_date_idx'),
            models.Index(fields=['transfer_no'], name='st_transfer_no_idx'),
        ]
        constraints = [
            # Kaynak ve hedef aynı şube olamaz
            models.CheckConstraint(
                condition=~models.Q(source_store=models.F('destination_store')),
                name='st_source_neq_dest',
            ),
        ]

    def __str__(self):
        no = self.transfer_no or f"DRAFT-{str(self.id)[:8]}"
        return f"{no}: {self.source_store_id} → {self.destination_store_id} ({self.get_status_display()})"

    # --- Yardımcı Property'ler ---

    @property
    def is_terminal(self) -> bool:
        """Durum değiştirilemez son hâl mi?"""
        return self.status in (
            self.Status.ACCEPTED,
            self.Status.REJECTED,
            self.Status.CANCELLED,
            self.Status.PARTIALLY_ACCEPTED,
        )

    @property
    def is_pending_action(self) -> bool:
        """Hedef şubenin aksiyon almasını bekliyor mu?"""
        return self.status == self.Status.IN_TRANSIT

    @property
    def is_overdue(self) -> bool:
        """Beklenen varış tarihi geçtiği halde hâlâ yolda mı?"""
        if self.status != self.Status.IN_TRANSIT:
            return False
        if not self.expected_arrival_date:
            return False
        return self.expected_arrival_date < timezone.now().date()


class StoreTransferItem(models.Model):
    """Bir transferin tek bir kalemi (nakit veya stok).

    Bir StoreTransfer 1+ StoreTransferItem içerir. Her kalem ya CASH
    (currency + amount) ya da STOCK (product + miktar) tipindedir.

    Ledger bağlantıları (source_*/destination_*) servis katmanı
    tarafından doldurulur ve audit trail oluşturur.
    """

    class ItemType(models.TextChoices):
        CASH  = 'CASH',  _('Nakit / Döviz / HS')
        STOCK = 'STOCK', _('Stok (Ürün veya Gram)')

    class Currency(models.TextChoices):
        TRY = 'TRY', 'Türk Lirası'
        USD = 'USD', 'Amerikan Doları'
        EUR = 'EUR', 'Euro'
        GBP = 'GBP', 'İngiliz Sterlini'
        HS  = 'HS',  'Has Altın (gram)'

    class ItemStatus(models.TextChoices):
        PENDING  = 'PENDING',  _('Beklemede')
        ACCEPTED = 'ACCEPTED', _('Kabul Edildi')
        REJECTED = 'REJECTED', _('Reddedildi')

    # --- Birincil Anahtar ---
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Üst Transfer ---
    transfer = models.ForeignKey(
        StoreTransfer,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name=_('Üst Transfer'),
    )

    # --- Kalem Tipi ---
    item_type = models.CharField(
        _('Kalem Tipi'),
        max_length=10,
        choices=ItemType.choices,
        db_index=True,
    )

    # ── CASH ALANLARI (item_type='CASH' ise dolu) ──────────────
    currency = models.CharField(
        _('Para Birimi'),
        max_length=3,
        choices=Currency.choices,
        null=True, blank=True,
        help_text="item_type='CASH' ise zorunlu; aksi halde NULL.",
    )
    amount = models.DecimalField(
        _('Miktar'),
        max_digits=18, decimal_places=2,
        null=True, blank=True,
        help_text="CASH için tutar (TRY/USD/EUR/GBP/HS). HS için decimal_places=2 yerine TransferItem.amount_hs uygulamalıdır; bu alan genel kullanım içindir.",
    )
    amount_eur_equivalent = models.DecimalField(
        _('TL Karşılığı (gönderim anında)'),
        max_digits=18, decimal_places=2,
        default=Decimal('0'),
        help_text='Kur dalgalanmasından bağımsız, gönderim anındaki TL karşılığı.',
    )
    exchange_rate_at_dispatch = models.DecimalField(
        _('Gönderim Kuru'),
        max_digits=12, decimal_places=4,
        null=True, blank=True,
        help_text='currency=TRY için NULL; diğer döviz/HS için tarihsel sabit kur.',
    )

    # ── STOCK ALANLARI (item_type='STOCK' ise dolu) ────────────
    product = models.ForeignKey(
        'products.Products',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='transfer_items',
        verbose_name=_('Ürün'),
        help_text="item_type='STOCK' ise zorunlu.",
    )
    quantity_pieces = models.PositiveIntegerField(
        _('Adet'),
        default=0,
        help_text='Barkodlu/parçalı ürünler için. Gramajlı altında 0.',
    )
    quantity_gram = models.DecimalField(
        _('Gram'),
        max_digits=18, decimal_places=3,
        default=Decimal('0'),
        help_text='Gramajlı altın/bilezik için. Barkodlu parça ürünlerde 0.',
    )
    unit_cost_hs = models.DecimalField(
        _('Birim Maliyet (HS)'),
        max_digits=18, decimal_places=4,
        default=Decimal('0'),
        help_text='Kaynak şubedeki StockSnapshot.weighted_avg_cost_hs değeri kopyalanır. Hedef WAC hesaplamasının girdisi.',
    )
    unit_cost_eur = models.DecimalField(
        _('Birim Maliyet (TL)'),
        max_digits=18, decimal_places=2,
        default=Decimal('0'),
        help_text='Kaynak şubedeki WAC TL değeri (tarihsel kopya).',
    )

    # ── KAYNAK / HEDEF KASA SEÇİMİ (CASH için) ─────────────────
    # FAZ 46: DRAFT aşamasında kaynak şubedeki kasa açıkça seçilir.
    # Hedef kasa accept anında otomatik çözülür (currency match) veya
    # alan tarafça manuel seçilir. STOCK kalemler için NULL kalır.
    source_bank_account = models.ForeignKey(
        'banking.BankAccount',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='outbound_transfer_items',
        verbose_name=_('Kaynak Kasa (CASH)'),
        help_text='CASH item için DRAFT aşamasında zorunlu. Hangi kasadan çıkacak?',
    )
    destination_bank_account = models.ForeignKey(
        'banking.BankAccount',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='inbound_transfer_items',
        verbose_name=_('Hedef Kasa (CASH)'),
        help_text='Accept anında çözülür. NULL ise alan şubenin currency-eşleşen ilk CASH hesabı kullanılır.',
    )

    # ── KISMI KABUL ALANLARI ───────────────────────────────────
    item_status = models.CharField(
        _('Kalem Durumu'),
        max_length=10,
        choices=ItemStatus.choices,
        default=ItemStatus.PENDING,
        db_index=True,
    )
    accepted_amount = models.DecimalField(
        _('Kabul Edilen Tutar (CASH)'),
        max_digits=18, decimal_places=2,
        null=True, blank=True,
        help_text='Kısmi kabul senaryosu için. Tam kabulde amount ile eşit, redde 0.',
    )
    accepted_pieces = models.PositiveIntegerField(
        _('Kabul Edilen Adet (STOCK)'),
        null=True, blank=True,
    )
    accepted_gram = models.DecimalField(
        _('Kabul Edilen Gram (STOCK)'),
        max_digits=18, decimal_places=3,
        null=True, blank=True,
    )
    rejection_reason = models.CharField(
        _('Red Sebebi'),
        max_length=255,
        blank=True, default='',
    )

    # ── LEDGER BAĞLANTILARI (audit trail) ──────────────────────
    source_cashbox_ledger = models.ForeignKey(
        'banking.CashboxLedger',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transfer_item_sources',
        verbose_name=_('Kaynak Kasa Defteri Satırı'),
        help_text='IN_TRANSIT yazımındaki TRANSFER_OUT girdisinin FK referansı.',
    )
    source_stock_ledger = models.ForeignKey(
        'stock_management.StockLedger',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transfer_item_sources',
        verbose_name=_('Kaynak Stok Defteri Satırı'),
    )
    destination_cashbox_ledger = models.ForeignKey(
        'banking.CashboxLedger',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transfer_item_destinations',
        verbose_name=_('Hedef Kasa Defteri Satırı'),
        help_text='ACCEPTED yazımındaki TRANSFER_IN girdisinin FK referansı.',
    )
    destination_stock_ledger = models.ForeignKey(
        'stock_management.StockLedger',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transfer_item_destinations',
        verbose_name=_('Hedef Stok Defteri Satırı'),
    )

    # --- Audit ---
    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'StoreTransferItems'
        verbose_name = _('Transfer Kalemi')
        verbose_name_plural = _('Transfer Kalemleri')
        ordering = ['transfer_id', 'created_on']
        indexes = [
            models.Index(fields=['transfer', 'item_type'], name='sti_transfer_type_idx'),
            models.Index(fields=['transfer', 'item_status'], name='sti_transfer_status_idx'),
            models.Index(fields=['product'], name='sti_product_idx'),
        ]

    def __str__(self):
        if self.item_type == self.ItemType.CASH:
            return f"{self.transfer_id} • {self.amount} {self.currency or '?'}"
        return f"{self.transfer_id} • {self.quantity_pieces}ad / {self.quantity_gram}gr"

    # --- Yardımcı Property'ler ---

    @property
    def is_cash(self) -> bool:
        return self.item_type == self.ItemType.CASH

    @property
    def is_stock(self) -> bool:
        return self.item_type == self.ItemType.STOCK
