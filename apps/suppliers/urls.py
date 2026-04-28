from django.urls import path

from apps.suppliers.views import *

app_name = 'suppliers'

urlpatterns = [
    path('index', suppliers_view, name='index'),
    path('detail/<uuid:record_id>', suppliers_detail, name='detail'),

    path('add', add_supplier, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),

    path('get-suppliers', get_suppliers, name='get-suppliers'),
    path('get-balances', get_balances, name='get-balances'),
    path('get-balances-all', get_balances_all, name='get-balances-all'),
    path('get-supplier-process-history', get_supplier_process_history, name='get-supplier-process-history'),
    path('fis-data', supplier_fis_data, name='fis-data'),

    path('download-report/<uuid:record_id>', download_supplier_report, name='download-report'),

    # Çantacı Modülü
    path('cantaci/<uuid:record_id>', cantaci_detail_view, name='cantaci-detail'),
    path('cantaci/hareketler', cantaci_hareketler, name='cantaci-hareketler'),
    path('cantaci/islem-ekle', cantaci_islem_ekle, name='cantaci-islem-ekle'),
    path('cantaci/export/<uuid:record_id>', cantaci_export, name='cantaci-export'),

    # FAZ 11 / BL-01 — Cari Sıfırlama / Manuel Düzeltme (Adjustment Entry)
    path('adjustment/create', supplier_adjustment_create, name='adjustment-create'),
]
