from django.urls import path

from apps.customers.views import *
from apps.customers.cari_views import (
    cari_collect,
    cari_custody_offset,
    cari_reverse,
    cari_approve,
    cari_balance,
    cari_ledger_list,
    cari_pending_approvals,
    cari_collect_bank_accounts,
    cari_preview_close,
    cari_fx_rates,
    cari_detail_page,
    cari_pending_approvals_page,
    # FAZ 49 — Ürün/Hurda ile tahsilat ve ödeme
    cari_collect_with_products,
    cari_product_search,
    # FAZ 51 (R-05) — Ürün/Hurda tahsilat/ödeme geri alma
    cari_reverse_product_collection,
)

app_name = 'customers'

urlpatterns = [
    path('index', customers_view, name='index'),
    path('add', add_customer, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
    path('get-customers', get_customers, name='get-customers'),
    path('get-detail', get_customer_detail_json, name='get-detail-json'),
    path('detail/<uuid:customer_id>', customer_detail_view, name='detail'),
    path('<uuid:customer_id>/verify/state', customer_verify_state, name='customer-verify-state'),
    path('<uuid:customer_id>/verify/send', send_customer_verification, name='customer-verify-send'),
    path('<uuid:customer_id>/verify/confirm', confirm_customer_verification, name='customer-verify-confirm'),
    path('detail/<str:customer_id>/transactions', get_customer_transactions, name='detail-transactions'),
    path('save-debt-collection', save_debt_collection, name='save_debt_collection'),

    # ────────────────────────────────────────────────────────────
    # Cari & Emanet Refactor (Append-Only Ledger)
    # ────────────────────────────────────────────────────────────
    # HTML sayfaları
    path('<uuid:customer_id>/cari/',
         cari_detail_page, name='cari-detail-page'),
    path('cari/pending-approvals/page',
         cari_pending_approvals_page, name='cari-pending-approvals-page'),

    # JSON endpoint'leri
    path('<uuid:customer_id>/cari/collect',
         cari_collect, name='cari-collect'),
    path('<uuid:customer_id>/cari/custody-offset',
         cari_custody_offset, name='cari-custody-offset'),
    path('<uuid:customer_id>/cari/reverse',
         cari_reverse, name='cari-reverse'),
    path('<uuid:customer_id>/cari/approve',
         cari_approve, name='cari-approve'),
    path('<uuid:customer_id>/cari/balance',
         cari_balance, name='cari-balance'),
    path('<uuid:customer_id>/cari/ledger',
         cari_ledger_list, name='cari-ledger-list'),
    path('<uuid:customer_id>/cari/preview-close',
         cari_preview_close, name='cari-preview-close'),
    path('cari/pending-approvals',
         cari_pending_approvals, name='cari-pending-approvals'),
    path('cari/bank-accounts',
         cari_collect_bank_accounts, name='cari-bank-accounts'),
    path('cari/fx-rates',
         cari_fx_rates, name='cari-fx-rates'),

    # ────────────────────────────────────────────────────────────
    # FAZ 49 — Ürün/Hurda ile Tahsilat ve Ödeme
    # ────────────────────────────────────────────────────────────
    path('<uuid:customer_id>/cari/collect-with-products',
         cari_collect_with_products, name='cari-collect-with-products'),
    path('cari/product-search',
         cari_product_search, name='cari-product-search'),
    # FAZ 51 (R-05): Ürün/Hurda ile tahsilat/ödeme geri alma (atomik)
    path('<uuid:customer_id>/cari/reverse-product-collection',
         cari_reverse_product_collection,
         name='cari-reverse-product-collection'),
]
