from django.db import models
from apps.accounts.models import *
from apps.products.models import *
from apps.customers.models import *
from apps.stores.models import *
from django.utils import timezone

from apps.workshops.models import Workshops


class InventoryCountSession(models.Model):
    # ──────────────────────────────────────────────────────────────
    # FAZ 2: Kapsam (Scope) Bazlı Sayım Desteği
    # ──────────────────────────────────────────────────────────────
    # Bir sayım oturumu artık yalnızca "tüm mağaza" değil; kategori,
    # materyal tipi, marka veya mücevher tipi gibi bir alt kümeyle
    # başlatılabilir. Geriye uyum için default='ALL' — eski sessionlar
    # aynı davranışı korur.
    class ScopeType(models.TextChoices):
        ALL           = 'ALL',           'Tüm Mağaza'
        CATEGORY      = 'CATEGORY',      'Kategori'
        MATERIAL      = 'MATERIAL',      'Materyal Tipi'
        BRAND         = 'BRAND',         'Marka'
        JEWELRY_TYPE  = 'JEWELRY_TYPE',  'Mücevher Tipi'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True)
    start_time = models.DateTimeField(default=timezone.now)
    end_time = models.DateTimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)

    # Kapsam meta-verisi
    scope_type = models.CharField(
        max_length=16,
        choices=ScopeType.choices,
        default=ScopeType.ALL,
        db_index=True,
        verbose_name="Sayım Kapsam Tipi",
        help_text=(
            "Oturumun hangi alt küme için başlatıldığını belirtir. "
            "ALL: Tüm mağaza (varsayılan, geriye uyum). "
            "CATEGORY/MATERIAL/BRAND/JEWELRY_TYPE: scope_filter alanı bu tip için "
            "hangi değerlerin kapsama dahil olduğunu içerir."
        ),
    )

    # scope_filter: kapsam tipine göre içerdiği değerler (JSON).
    # Örn:
    #   CATEGORY      -> {"category_ids": ["uuid1", "uuid2"]}
    #   MATERIAL      -> {"material_types": ["GOLD", "SILVER"]}
    #   BRAND         -> {"brands": ["Cartier", "Rolex"]}
    #   JEWELRY_TYPE  -> {"jewelry_types": ["Yüzük", "Bilezik"]}
    #   ALL           -> {} (boş)
    scope_filter = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Kapsam Filtre Parametreleri",
        help_text=(
            "Kapsam tipine göre uygulanacak filtre değerleri (ID listeleri veya "
            "string listeleri). ALL için {} — boş. JSON şemasını backend yorumlar."
        ),
    )

    # Kullanıcıya gösterilecek okunur etiket. Rapor ve UI'da kullanılır.
    scope_label = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name="Kapsam Etiketi",
        help_text=(
            "Raporlarda ve UI'da gösterilecek okunur metin. "
            "Örn: '14 Ayar Yüzükler', 'Rolex Koleksiyonu'. "
            "ALL için boş bırakılır."
        ),
    )

    def __str__(self):
        return f"{self.store} - {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}"

    class Meta:
        db_table = 'InventoryCountSession'


class InventoryCountItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(InventoryCountSession, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Products,null=True, blank=True, on_delete=models.SET_NULL)
    is_counted = models.BooleanField(default=False)
    scanned_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.product.barcode} / {self.session}"

    class Meta:
        db_table = 'InventoryCountItem'
