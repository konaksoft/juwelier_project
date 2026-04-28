import uuid
from django.db import models
from django.utils import timezone
from decimal import Decimal
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.products.models import *
from apps.gold_purchases.models import *
from apps.accounts.models import *
from apps.scraps.models import *
from apps.stores.models import Stores
from apps.accounts.models import Users


class Inventories(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Products, on_delete=models.CASCADE, related_name='product_inventories')
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    stock_pieces = models.IntegerField(default=0)
    stock_weight = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    incoming_stock_pieces = models.IntegerField(default=0)
    incoming_stock_weight = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    # Mağaza Özel Fiyatlandırma Ayarları
    use_custom_pricing = models.BooleanField(default=False, verbose_name="Özel Fiyat Kullan")
    weighted_buy_price_hs = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'))

    # Eğer mağaza bu ürünü global değerden farklı bir has maliyetiyle alıyorsa:
    custom_buy_price_hs = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('0.0000'),
        null=True, blank=True, verbose_name="Özel Alış Has"
    )

    # Eğer mağaza bu ürünü global değerden farklı bir has satış değeriyle satıyorsa:
    custom_sale_price_hs = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('0.0000'),
        null=True, blank=True, verbose_name="Özel Satış Has"
    )

    # İsteğe bağlı: Sabit işçilik de mağazaya göre değişebilir
    custom_fixed_labor = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        null=True, blank=True, verbose_name="Özel Sabit İşçilik"
    )
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE, null=True, blank=True)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'Inventories'
        ordering = ['-created_on']
        unique_together = ('store', 'product')
        indexes = [
            models.Index(fields=['store', 'product'], name='inventories_store_product_idx'),
            models.Index(fields=['store'], name='inventories_store_idx'),
        ]


class InventoryMovement(models.Model):
    MOVEMENT_TYPES = (
        ('entry', 'Giriş'),
        ('exit', 'Çıkış'),
        ('transfer', 'Transfer'),
        ('update', 'Manuel Güncelleme'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    product = models.ForeignKey(Products, on_delete=models.CASCADE, related_name='inventory_movements')
    process_no = models.CharField(max_length=15, null=True, blank=True)
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)  # max_length arttırıldı
    quantity_pieces = models.IntegerField(default=0)
    quantity_weight = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    stock_before_pieces = models.IntegerField(default=0, null=True, blank=True)
    stock_after_pieces = models.IntegerField(default=0, null=True, blank=True)
    stock_before_weight = models.DecimalField(max_digits=10, decimal_places=3, default=0, null=True, blank=True)
    stock_after_weight = models.DecimalField(max_digits=10, decimal_places=3, default=0, null=True, blank=True)
    description = models.TextField(blank=True, null=True)

    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='created_movements')
    created_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.product.name} - {self.movement_type}"

    class Meta:
        db_table = 'InventoryMovement'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['store', 'product'], name='invmovement_store_product_idx'),
            models.Index(fields=['process_no'], name='invmovement_process_no_idx'),
            models.Index(fields=['store', '-created_on'], name='invmovement_store_created_idx'),
            models.Index(fields=['movement_type'], name='invmovement_type_idx'),
        ]

