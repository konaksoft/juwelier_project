from django.db import models
from apps.accounts.models import *
from apps.products.models import *
from apps.suppliers.models import Suppliers
from django.utils import timezone


class ProductCategory(models.Model):
    """Mağazaya özel takı tipi kategorileri ve barkod kısayolları (prefix)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='product_categories')
    name = models.CharField(max_length=100)
    barcode_prefix = models.CharField(max_length=10)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ProductCategories'
        unique_together = ('store', 'barcode_prefix')
        indexes = [
            models.Index(fields=['store', 'is_deleted', 'is_active'], name='pc_store_active_idx'),
        ]

    def __str__(self):
        return f"{self.name} ({self.barcode_prefix})"


class BarcodeTemplate(models.Model):
    """Hızlı barkodlama için veri giriş şablonları. Gram hariç sabit değerler saklanır.

    PIVOT FAZ E UI FIX (2026-04-23):
      - material_type alanı eklendi ('GOLD'/'SILVER'/'DIAMOND'/'WATCH').
        Default 'GOLD' — mevcut şablonlar otomatik Altın kategorisine düşer.
      - extra_data JSONField eklendi: Pırlanta/Saat'e özel alanlar (mount_karat,
        sale_currency, sale_price, buy_price_eur, watch_condition, ...) buraya
        yazılır. Bu sayede ileride yeni alan eklendiğinde migration gerekmez.
      - Mevcut altın alanları (gold_rate, product_mileage, labor_mileage, ...)
        geriye dönük tam uyumlu korundu. Pırlanta/Saat şablonlarında boş kalır.
    """
    MATERIAL_TYPE_CHOICES = [
        ('GOLD', 'Altın'),
        ('SILVER', 'Gümüş'),
        ('DIAMOND', 'Pırlanta'),
        ('WATCH', 'Saat'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='barcode_templates')
    template_name = models.CharField(max_length=100)
    material_type = models.CharField(
        max_length=10,
        choices=MATERIAL_TYPE_CHOICES,
        default='GOLD',
        db_index=True,
        help_text="Şablonun ait olduğu ürün tipi — form tabları bu değere göre filtrelenir.",
    )
    jewelry_type = models.CharField(max_length=100, blank=True, default='')
    gold_rate = models.CharField(max_length=10, blank=True, default='')
    product_mileage = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    labor_mileage = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    piece_labor = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    profit = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    ring_size = models.CharField(max_length=10, blank=True, default='', verbose_name="Alyans Numarası")
    process_supplier_ledger = models.BooleanField(default=False, verbose_name="Tedarikçi Carisine İşle")
    supplier = models.ForeignKey(Suppliers, on_delete=models.SET_NULL, null=True, blank=True)
    extra_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Material-type özel alanları. Pırlanta: mount_metal, mount_karat, "
            "mount_gram, sale_currency, sale_price, buy_price_eur, diamond_shape, ... "
            "Saat: watch_condition, watch_movement_type, watch_case_material, "
            "sale_currency, sale_price, buy_price_eur, ..."
        ),
    )
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'BarcodeTemplates'
        indexes = [
            models.Index(fields=['store', 'is_deleted', 'is_active'], name='bt_store_active_idx'),
            models.Index(fields=['store', 'material_type', 'is_deleted'], name='bt_store_mat_idx'),
        ]

    def __str__(self):
        return f"{self.template_name} ({self.get_material_type_display()})"


class GoldPurchases(models.Model):
    COUNT_STATUS_CHOICES = [
        (0, 'Yapılmadı'),
        (1, 'Yapıldı'),
        (2, 'Hatalı'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Products, on_delete=models.CASCADE, null=True, blank=True, related_name='product')
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    # FAZ TS-1 (Tedarikçi Silme Güvenliği): on_delete=CASCADE -> SET_NULL
    # Barkodlu/etiketlenmiş ürün alış kayıtlarının (is_labeled=True dahil)
    # tedarikçi silindiğinde fiziksel olarak yok olmasını engellemek için
    # CASCADE kaldırıldı. Tedarikçi silindiğinde supplier alanı NULL olur,
    # GoldPurchases satırı korunur ve barkod yazdırma akışı çalışmaya devam eder.
    supplier = models.ForeignKey(Suppliers, on_delete=models.SET_NULL, null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='gold_purchases')
    created_on = models.DateTimeField(auto_now_add=True)
    count_is_status = models.IntegerField(choices=COUNT_STATUS_CHOICES, default=0, null=True, blank=True)
    is_status = models.BooleanField(default=True, null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)
    is_labeled = models.BooleanField(default=False, verbose_name="Etiket Basıldı")

    class Meta:
        db_table = 'GoldPurchases'
        indexes = [
            models.Index(fields=['store', 'is_deleted'], name='gp_store_deleted_idx'),
            models.Index(fields=['store', '-created_on'], name='gp_store_created_idx'),
            models.Index(fields=['supplier'], name='goldpurchases_supplier_idx'),
            models.Index(fields=['-created_on'], name='gp_created_on_desc_idx'),
        ]
