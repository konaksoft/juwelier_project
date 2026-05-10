import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from apps.customers.models import *
from apps.accounts.models import *
from apps.stores.models import *
from apps.products.models import *


# apps/custody/models.py
class CustomerCustodyLedger(models.Model):
    """Müşteri Emanet Defteri — Append-Only (Cari/Emanet Refactor + FAZ 22).

    Tasarım:
      - Her hareket bir satır olarak yazılır (IN / OUT / OFFSET / REVERSAL).
      - Net emanet bakiyesi:
            Σ(IN.amount_hs)
          - Σ(OUT.amount_hs)
          - Σ(OFFSET.amount_hs)
          - Σ(REVERSAL.amount_hs ile parent.tipine göre işaret)
        (yalnız is_active=True ve is_deleted=False satırlar dahil)
      - `OFFSET` tipi: emanet → cari mahsuplaşma. CustomerLedger
        tarafında karşılık olarak `CUSTODY_OFFSET` satırı yazılır;
        iki kayıt karşılıklı bağlanır (`related_ledger` ↔
        `CustomerLedger.related_custody`).
      - Geriye uyum: `is_returned` boolean ve mevcut IN/OUT toggle
        akışı (custody/views.py) bozulmadan korunur. Yeni mahsuplaşma
        akışı OFFSET tipini kullanmalıdır.

    FAZ 22 — Emanet Kriz Fix:
      - is_active / is_deleted alanları eklendi → soft-delete + denetim.
      - parent FK her kısmi OUT kaydında doldurulur (append-only;
        orijinal IN satırına UPDATE çekilmez).
      - remaining_quantity_* property'leri ile per-row kalan miktar
        hesaplanır (mutasyonsuz).
    """

    # ── Hareket Tipleri ──────────────────────────────────────────
    CUSTODY_IN = 'IN'        # ürün/altın bize bırakıldı
    CUSTODY_OUT = 'OUT'      # ürün/altın teslim edildi
    CUSTODY_OFFSET = 'OFFSET'  # cari ile mahsuplaşma (borçtan düşüldü)
    CUSTODY_REVERSAL = 'REVERSAL'  # bir emanet hareketinin iptal karşı girişi
    # FAZ 24 — GEREKSİNİM-2: Emanet → Mağaza stoğuna transfer.
    # Müşteri emaneti mağazanın serbest stoğuna geçirildiğinde yazılır.
    # Bakiye etkisi OUT/OFFSET ile aynıdır (emanet bakiyesinden düşer).
    # Ancak müşterinin cari hesabına bir borç doğar (stok değer kadar Has).
    CUSTODY_STOCK_TRANSFER = 'STOCK'

    CUSTODY_TYPE_CHOICES = [
        (CUSTODY_IN, 'Emanet Girişi'),
        (CUSTODY_OUT, 'Emanet Çıkışı'),
        (CUSTODY_OFFSET, 'Cari Mahsuplaşma'),
        (CUSTODY_REVERSAL, 'İptal Karşı Girişi'),
        (CUSTODY_STOCK_TRANSFER, 'Stoğa Transfer'),
    ]

    # Net emanet bakiyesini AZALTAN tipler
    CUSTODY_DECREASING_TYPES = (CUSTODY_OUT, CUSTODY_OFFSET, CUSTODY_STOCK_TRANSFER)
    CUSTODY_INCREASING_TYPES = (CUSTODY_IN,)

    custody_type = models.CharField(
        max_length=10,
        choices=CUSTODY_TYPE_CHOICES,
        default=CUSTODY_IN,
    )

    customer = models.ForeignKey(Customers, on_delete=models.CASCADE)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE)
    product = models.ForeignKey(Products, null=True, blank=True,
                                on_delete=models.SET_NULL)

    quantity_piece = models.PositiveIntegerField(default=0)
    quantity_gram = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    amount_hs = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    # ── Çift Birim (raporlama için) ──────────────────────────────
    # Mahsuplaşmalarda kur farkı yok ama TL raporlaması için
    # işlem anındaki kur ve TL karşılığı kaydedilir.
    exchange_rate_eur = models.DecimalField(
        max_digits=18, decimal_places=6, default=Decimal('0.000000'),
        help_text='1 gr Has = X TL (kayıt anındaki, sabit)',
    )
    amount_eur = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'),
        help_text='İşlem anındaki TL karşılığı (raporlama için)',
    )

    # ── Bağlantı ─────────────────────────────────────────────────
    process_no = models.CharField(max_length=20, db_index=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='child_entries',
        help_text='REVERSAL/OFFSET/Kısmi OUT için bağlı orijinal emanet kaydı',
    )
    related_ledger = models.ForeignKey(
        'customers.CustomerLedger', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='custody_offsets',
        help_text='OFFSET satırlarında karşılık CustomerLedger satırı',
    )

    # ── Geriye Uyum: Toggle pattern ──────────────────────────────
    # YENİ KOD bu alanı kullanmamalı; emanet iadesi için ayrı bir OUT
    # satırı veya REVERSAL kaydı yazılmalıdır. Ancak custody/views.py
    # mevcut toggle akışı (return_custody / cancel_row) bu alanı aktif
    # kullandığından korunuyor.
    is_returned = models.BooleanField(default=False)

    # ── FAZ 22: Soft-delete & denetim ───────────────────────────
    is_active = models.BooleanField(
        default=True,
        help_text='False ise bakiyeye dahil edilmez (REVERSAL sonrası).',
    )
    is_deleted = models.BooleanField(
        default=False,
        help_text='Soft-delete: silinmiş kayıt; denetim izi korunur.',
    )
    reverse_reason = models.CharField(
        max_length=255, blank=True, default='',
        help_text='REVERSAL kayıtlarında iptal nedeni metni.',
    )
    cancelled_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cancelled_custody_entries',
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_on = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL,
                                   null=True, related_name='+')

    received_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    delivered_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    # ── Audit ────────────────────────────────────────────────────
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'CustomerCustodyLedger'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['customer', 'is_returned'], name='custody_cust_returned_idx'),
            models.Index(fields=['custody_type'], name='custody_type_idx'),
            models.Index(fields=['parent'], name='custody_parent_idx'),
            models.Index(fields=['related_ledger'], name='custody_rel_ledger_idx'),
            models.Index(fields=['customer', 'is_active', 'is_deleted'], name='custody_active_idx'),
        ]

    def __str__(self):
        return f"{self.customer} {self.custody_type} {self.amount_hs} HS"

    # ── Yardımcı Property'ler ────────────────────────────────────
    @property
    def signed_amount_hs(self):
        """Net emanet bakiyesine etkisi (işaretli)."""
        if not self.is_active or self.is_deleted:
            return Decimal('0')
        if self.custody_type == self.CUSTODY_REVERSAL:
            # parent yoksa etki yok (savunma)
            if self.parent is None:
                return Decimal('0')
            if self.parent.custody_type in self.CUSTODY_INCREASING_TYPES:
                return -self.amount_hs
            elif self.parent.custody_type in self.CUSTODY_DECREASING_TYPES:
                return self.amount_hs
            return Decimal('0')
        if self.custody_type in self.CUSTODY_INCREASING_TYPES:
            return self.amount_hs
        if self.custody_type in self.CUSTODY_DECREASING_TYPES:
            return -self.amount_hs
        return Decimal('0')

    # ── FAZ 22+24: Per-row kalan miktar (append-only kısmi teslim) ─
    # FAZ 24 — BUG-3-B: Legacy fallback eklendi.
    # Eski kodda kısmi/tam teslim OUT kayıtları `parent=None` ile yazılmıştı.
    # 0005_faz24_legacy_parent_fix migration'ı çoğunu onarır; ancak
    # process_no eksik olan veya birebir eşleşmeyen kayıtlar için
    # property'ler ek olarak `parent IS NULL AND process_no = self.process_no
    # AND customer = self.customer` koşullu fallback hesabı yapar.
    # Bu sayede 5. ve sonraki teslim denemelerinde "kalan miktar"
    # tutarlı kalır.

    def _legacy_outs_qs(self):
        """Eski parent=None OUT/OFFSET kayıtları (aynı process_no + customer + store)."""
        if not self.process_no:
            return CustomerCustodyLedger.objects.none()
        return CustomerCustodyLedger.objects.filter(
            customer_id=self.customer_id,
            store_id=self.store_id,
            process_no=self.process_no,
            parent__isnull=True,
            custody_type__in=(self.CUSTODY_OUT, self.CUSTODY_OFFSET, self.CUSTODY_STOCK_TRANSFER),
            is_active=True,
            is_deleted=False,
        ).exclude(pk=self.pk)

    @property
    def delivered_quantity_gram(self):
        """Bu IN kaydından çıkmış toplam gram (parent FK + legacy fallback)."""
        if self.custody_type != self.CUSTODY_IN:
            return Decimal('0')
        primary = CustomerCustodyLedger.objects.filter(
            parent=self,
            custody_type__in=(self.CUSTODY_OUT, self.CUSTODY_OFFSET, self.CUSTODY_STOCK_TRANSFER),
            is_active=True,
            is_deleted=False,
        )
        legacy = self._legacy_outs_qs()
        agg = (primary | legacy).aggregate(
            s=Coalesce(Sum('quantity_gram'), Decimal('0')),
        )
        return agg['s'] or Decimal('0')

    @property
    def delivered_quantity_piece(self):
        if self.custody_type != self.CUSTODY_IN:
            return 0
        primary = CustomerCustodyLedger.objects.filter(
            parent=self,
            custody_type__in=(self.CUSTODY_OUT, self.CUSTODY_OFFSET, self.CUSTODY_STOCK_TRANSFER),
            is_active=True,
            is_deleted=False,
        )
        legacy = self._legacy_outs_qs()
        agg = (primary | legacy).aggregate(
            s=Coalesce(Sum('quantity_piece'), 0),
        )
        return int(agg['s'] or 0)

    @property
    def delivered_amount_hs(self):
        if self.custody_type != self.CUSTODY_IN:
            return Decimal('0')
        primary = CustomerCustodyLedger.objects.filter(
            parent=self,
            custody_type__in=(self.CUSTODY_OUT, self.CUSTODY_OFFSET, self.CUSTODY_STOCK_TRANSFER),
            is_active=True,
            is_deleted=False,
        )
        legacy = self._legacy_outs_qs()
        agg = (primary | legacy).aggregate(
            s=Coalesce(Sum('amount_hs'), Decimal('0')),
        )
        return agg['s'] or Decimal('0')

    @property
    def remaining_quantity_gram(self):
        """Bu IN kaydında kalan gram (kısmi teslim sonrası)."""
        return max(self.quantity_gram - self.delivered_quantity_gram, Decimal('0'))

    @property
    def remaining_quantity_piece(self):
        return max(self.quantity_piece - self.delivered_quantity_piece, 0)

    @property
    def remaining_amount_hs(self):
        return max(self.amount_hs - self.delivered_amount_hs, Decimal('0'))

    @property
    def is_fully_delivered(self):
        """is_returned bayrağı VEYA parent OUT'larıyla tükendi mi?"""
        if self.is_returned:
            return True
        if self.custody_type != self.CUSTODY_IN:
            return False
        return self.remaining_amount_hs <= Decimal('0.0005')

    @property
    def has_reversal(self):
        """Bu kayıt için zaten REVERSAL yazılmış mı?"""
        return CustomerCustodyLedger.objects.filter(
            custody_type=self.CUSTODY_REVERSAL,
            parent=self,
            is_active=True,
        ).exists()

    def can_be_reversed(self):
        """REVERSAL yazılabilir mi? (idempotent kontrol)"""
        if self.custody_type == self.CUSTODY_REVERSAL:
            return False, 'REVERSAL kaydı tekrar iptal edilemez.'
        if self.is_deleted:
            return False, 'Bu kayıt zaten silinmiş.'
        if self.has_reversal:
            return False, 'Bu kayıt zaten iptal edilmiş.'
        return True, ''

    def mark_cancelled(self, *, user, reason: str):
        """REVERSAL yazıldıktan sonra orijinali pasifleştir + denetim."""
        self.is_active = False
        self.reverse_reason = (reason or '')[:255]
        self.cancelled_by = user
        self.cancelled_at = timezone.now()
        self.save(update_fields=[
            'is_active', 'reverse_reason', 'cancelled_by', 'cancelled_at',
        ])
