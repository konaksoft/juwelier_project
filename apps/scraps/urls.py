from django.urls import path
from .views import *

app_name = 'scraps'

urlpatterns = [
    # ── Altın Hurda Yönetimi (default) ──
    path('index', scrap_index, name='index'),

    # ── Gümüş Yönetimi (ONARIM FAZI 4 / ADIM 4 — izole sayfa) ──
    # Aynı endpoint'leri kullanır; template `view_material_type='SILVER'`
    # olarak set edilir → tüm POST/GET çağrıları gümüşe sabitlenir.
    path('silver/index', silver_index, name='silver_index'),

    # Ortak endpoint'ler (material_type query/POST ile filtre yapılır)
    path('add', scrap_add, name='add'),
    path('delete', delete, name='delete'),
    path('get-pool-sources', get_pool_sources, name='get-pool-sources'),
    path('get-pool-contents', get_pool_contents, name='get-pool-contents'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),

    # ── ONARIM FAZI 9 — Duplicate havuz birleştirme (operatör tetikli) ──
    path('merge-duplicates', merge_scrap_duplicates_view, name='merge_duplicates'),

    # ── ONARIM FAZI 12 — Havuz Detay Sayfası (Pool Detail & Ledger) ──
    # Modal yerine her havuza özel URL: 3 yıllık ledger için ölçeklenir.
    path('pool/<uuid:scrap_id>/', pool_detail, name='pool_detail'),
    path('pool/<uuid:scrap_id>/ledger', pool_ledger, name='pool_ledger'),

    # ── FAZ 22 — İşlem Detay (Process Detail Modal) ──
    # Havuz detay sayfasındaki işlem numarası tıklandığında modal için JSON.
    path('pool/<uuid:scrap_id>/process/<str:process_no>/', pool_process_detail,
         name='pool_process_detail'),

    # ── ONARIM FAZI 13 — Toplu İptal (Bulk Cancel) ──
    # Havuzdaki tüm aktif PURCHASE'ları tek tıkla iptal eder.
    # cancel_stock_entry zinciri korunur → REVERSAL'lar audit trail'de kalır.
    path('pool/<uuid:scrap_id>/bulk-cancel', pool_bulk_cancel, name='pool_bulk_cancel'),
]
