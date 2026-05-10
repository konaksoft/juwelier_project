from django.urls import path
from apps.products.views import *

app_name = 'products'

urlpatterns = [
    path('index', product_index, name='index'),
    path('add', product_add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),
    path('get-product-details', get_product_details, name='get-product-details'),
    path('get-has-gold-prices', get_has_gold_prices, name='get-has-gold-prices'),
    path('update-inventory', update_inventory_ajax, name='update-inventory'),
    path('update-inventory-bulk', update_inventory_bulk_ajax, name='products_update_inventory_bulk'),
    path('get-stock-movements-log/', get_stock_movements_log, name='get_stock_movements_log'),

    # --- FAZ S5 (PIVOT 2026-04-23): WATCH/DIAMOND için döviz kurları ---
    # GET ?currencies=USD,EUR,GBP,CHF -> {"rates": {...}, "missing": [...]}
    path('get-fx-rates', get_fx_rates, name='get-fx-rates'),

    # --- T2 (2026-04-29): Özel Ürün Sıralaması (drag-drop) ---
    # POST items=[{product_id, display_order}, ...] -> Products.display_order bulk update
    path('reorder', product_reorder, name='reorder'),

    # --- UAT V3 P2 (2026-04-29): Güvenli Silme (soft-delete) ---
    # GET  /<uuid>/deletion-check  -> ledger/stok varlığı kontrolü
    # POST /<uuid>/delete-single   -> tek ürün soft-delete
    path('<uuid:product_id>/deletion-check', deletion_check, name='deletion_check'),
    path('<uuid:product_id>/delete-single', delete_single, name='delete_single'),
]
