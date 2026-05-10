from django.urls import path
from apps.banking.views import *

app_name = 'banking'

urlpatterns = [
    # Ana Sayfa
    path('index', banking_index, name='index'),

    # DataTables — tüm hareketleri JSON
    path('get-all', get_all_transactions, name='get_all'),

    # Mysoft'tan hareketleri çek ve otomatik eşleştir
    path('sync', sync_transactions, name='sync'),

    # Tüm bekleyen hareketleri toplu otomatik eşleştir
    path('auto-match', auto_match_transactions, name='auto_match'),

    # Tek hareketi cari / faturaya eşleştir
    path('match', match_transaction, name='match'),

    # Eşleşmeyi kaldır
    path('unmatch', unmatch_transaction, name='unmatch'),

    # Mysoft tarafında okundu işaretle
    path('mark-read', mark_as_read, name='mark_read'),

    # Tek tıkla e-fatura oluştur
    path('create-invoice', create_invoice_from_transaction, name='create_invoice'),

    # Cari öneri (AJAX autocomplete için)
    path('suggest-customer', suggest_customer_for_transaction, name='suggest_customer'),

    # Banka Hesapları CRUD
    path('bank-accounts', get_bank_accounts, name='bank_accounts'),
    path('fx-breakdown', get_fx_breakdown, name='fx_breakdown'),
    path('bank-accounts/save', save_bank_account, name='bank_accounts_save'),
    path('bank-accounts/delete', delete_bank_account, name='bank_accounts_delete'),

    # e-Süreç Entegrasyon Ayarları (FAZ 2)
    path('settings', integration_settings, name='settings'),

    # POS Komisyon Yönetimi (FAZ 4)
    path('commission-rates/get', get_commission_rates, name='commission_rates_get'),
    path('commission-rates/save', save_commission_rate, name='commission_rates_save'),
    path('commission-rates/delete', delete_commission_rate, name='commission_rates_delete'),
    path('commission/calculate', calculate_commission_preview, name='commission_calculate'),
    path('commission/report', commission_report, name='commission_report'),

    # Mutabakat / Reconciliation (FAZ 3)
    path('reconcile/run-all', reconcile_run_all, name='reconcile_run_all'),
    path('reconcile/manual-match', reconcile_manual_match, name='reconcile_manual_match'),
    path('reconcile/summary', reconcile_summary, name='reconcile_summary'),
    path('reconcile/payments', reconcile_payments_list, name='reconcile_payments_list'),
]
