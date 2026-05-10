from django.urls import path
from apps.bracelets.views import *

app_name = 'bracelets'

urlpatterns = [
    path('index', bracelet_index, name='index'),
    path('add', bracelet_add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),

    # Havuz (pool) yönetimi — 2026-04-21
    path('get-pool-sources', get_pool_sources, name='get-pool-sources'),
    path('get-pool-contents', get_pool_contents, name='get-pool-contents'),

    # ── B-FAZ 7 — Havuz Detay Sayfası (Pool Detail & Ledger) ──
    path('pool/<uuid:bracelet_id>/', pool_detail, name='pool_detail'),
    path('pool/<uuid:bracelet_id>/ledger', pool_ledger, name='pool_ledger'),
    path('pool/<uuid:bracelet_id>/bulk-cancel', pool_bulk_cancel, name='pool_bulk_cancel'),

    # ── FAZ 22 — İşlem Detay (Process Detail Modal) ──
    path('pool/<uuid:bracelet_id>/process/<str:process_no>/', pool_process_detail,
         name='pool_process_detail'),
]
