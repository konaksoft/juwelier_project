from django.urls import path

from apps.custody.views import (
    custody_index,
    custody_get_all,
    add_custody,
    delete,
    change_status,
    custody_reverse,
    custody_receipt_data,
    customer_custody_history,
    custody_offset_preview,
    custody_store_summary,
    custody_by_process_no,
    custody_to_stock_transfer,
    custody_to_stock_reverse,
    custody_settlement,
    custody_settlement_preview,
)

app_name = 'custody'

urlpatterns = [
    path('', custody_index, name='index'),
    path('get-all', custody_get_all, name='get_all'),
    path('add', add_custody, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change_status'),

    # FAZ 23 — Emanet Kriz Fix
    path('reverse', custody_reverse, name='reverse'),
    # FAZ 24 — BUG-2: UUID → int (CustomerCustodyLedger PK = AutoField)
    path('receipt/<int:custody_id>', custody_receipt_data, name='receipt_data'),
    path('customer/<uuid:customer_id>/history',
         customer_custody_history, name='customer_history'),
    path('customer/<uuid:customer_id>/offset-preview',
         custody_offset_preview, name='offset_preview'),

    # FAZ 24 — BUG-1: process_no → emanet detay (CUS prefix routing)
    path('by-process-no/<str:process_no>',
         custody_by_process_no, name='by_process_no'),

    # FAZ 24 — GEREKSİNİM-1: Mağaza geneli emanet özeti
    path('store-summary', custody_store_summary, name='store_summary'),

    # FAZ 24 — GEREKSİNİM-2: Emanetten stoğa transfer
    path('to-stock', custody_to_stock_transfer, name='to_stock'),
    # FAZ 51 (R-07): Emanetten stoğa transferin atomik geri alımı
    path('to-stock/reverse', custody_to_stock_reverse, name='to_stock_reverse'),

    # FAZ 24 — GEREKSİNİM-3: Multi-method settlement
    path('settlement', custody_settlement, name='settlement'),
    path('settlement-preview',
         custody_settlement_preview, name='settlement_preview'),
]
