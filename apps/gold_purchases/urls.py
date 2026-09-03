from django.urls import path
from apps.gold_purchases.views import *

app_name = 'gold-purchases'

urlpatterns = [
    path('index', gold_purchases_index, name='index'),
    path('add', gold_purchase_add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),
    path('print-barcode', print_barcode, name='print-barcode'),
    path('print-barcode-normal', print_barcode_normal, name='print-barcode-normal'),
    path('stats', gold_purchases_stats, name="gold_purchases_stats"),
    path('gold-purchases/export', gold_purchases_export, name='gold_purchases_export'),
    path('get-print-data', get_print_data, name='get_print_data'),
    path('mark-printed', mark_as_printed, name='mark_printed'),

    # --- GÖREV 1: Ürün detay endpoint ---
    path('get-details', get_details, name='get_details'),

    # --- GÖREV 2: Kategori CRUD ---
    path('category-list', category_list, name='category_list'),
    path('category-add', category_add, name='category_add'),
    path('category-delete', category_delete, name='category_delete'),

    # --- GÖREV 3: Şablon CRUD ---
    path('template-list', template_list, name='template_list'),
    path('template-save', template_save, name='template_save'),
    path('template-delete', template_delete, name='template_delete'),

    # --- GÖREV 4: Kategori Bazlı Rapor ---
    path('category-report', gold_purchases_category_report, name='category_report'),

    # --- FAZ 10: Kategori + Ayar Bazlı Detaylı Rapor ---
    path('detailed-report', get_barcoded_products_report, name='detailed_report'),
    path('export-detailed-report-pdf', export_detailed_report_pdf, name='export_detailed_report_pdf'),

    # --- GÖREV 5: Excel Import ---
    path('import-excel', gold_purchases_import_excel, name='import_excel'),

    # --- GÖREV 6: ZIP Tam Yedekleme ---
    path('backup-export', backup_export, name='backup_export'),
    path('backup-import', backup_import, name='backup_import'),

    # --- PIVOT FAZ E (2026-04-23): Çoklu Maden Batch Entry (Saat + Pırlanta) ---
    # Bu endpoint YALNIZCA material_type ∈ {WATCH, DIAMOND} kabul eder.
    # Altın için mevcut 'add' endpoint'i (gold_purchase_add) kullanılmaya devam eder.
    # 3'lü tab yapısında Pırlanta ve Saat tab'ları AJAX ile buraya POST atacaktır.
    path('multi-material-add', multi_material_product_add, name='multi_material_add'),

    # --- 2026-09-01: Pırlanta/Saat GÜNCELLEME (PATCH semantiği) ---
    # Düzenleme akışı BURAYA gider; create endpoint'ine ASLA düşmez.
    # Yeni barkod üretmez, stok girmez, tedarikçi carisine 2. borç yazmaz.
    path('multi-material-update', multi_material_product_update, name='multi_material_update'),
]
