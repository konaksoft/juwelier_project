from django.urls import path

from apps.banking.bank_views import *

app_name = 'bank-management'

urlpatterns = [
    # Ana liste
    path('', bank_management_index, name='index'),
    path('get-all', bank_management_get_all, name='get_all'),

    # Konsolide rapor (tum kasalarin genel durumu)
    path('consolidated', bank_consolidated_report, name='consolidated'),

    # Banka hesap CRUD (banking/index.html'den tasindi)
    path('save', save_bank_account, name='save'),
    path('delete', delete_bank_account, name='delete'),

    # FAZ 18.2: Hızlı kasa oluşturma (ödeme akışı sırasında)
    path('quick-create', quick_create_account, name='quick_create'),

    # Kasa arasi transfer (virman)
    path('transfer', bank_transfer_view, name='transfer'),
    path('transfer-detail/<uuid:payment_id>/', bank_transfer_detail, name='transfer_detail'),

    # Gunluk kapanıs (Z-raporu)
    path('daily-close', daily_close_view, name='daily_close'),
    path('<uuid:account_id>/daily-closes', daily_close_list, name='daily_close_list'),

    # FAZ 18: Bekleyen İşlemler (Onaylı Kasa)
    path('pending-payments', pending_payments_list, name='pending_payments'),
    path('approve-payment', approve_payment, name='approve_payment'),
    path('reject-payment', reject_payment, name='reject_payment'),

    # FAZ 19: Bakiye Düzeltme / Açılış Fişi
    path('adjustment', adjustment_payment, name='adjustment'),

    # Detay sayfasi
    path('<uuid:account_id>/', bank_management_detail, name='detail'),
    path('<uuid:account_id>/payments', bank_management_payments, name='detail_payments'),
    path('<uuid:account_id>/export', bank_management_export, name='export'),
]
