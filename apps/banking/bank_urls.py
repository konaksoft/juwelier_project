from django.urls import path

from apps.banking.bank_views import *
# FAZ 61: Hızlı Gider Modülü endpoint'leri (yeni ayrı modül)
from apps.banking import expense_views

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

    # FAZ 31 / BUG-3: Manuel Gider Girişi (tek satır, eski endpoint — korunur)
    path('manual-expense', manual_expense, name='manual_expense'),

    # ────────────────────────────────────────────────────────────────
    # FAZ 61: Hızlı Gider Modülü
    # ────────────────────────────────────────────────────────────────

    # Hızlı giriş (toplu satır + klavye navigasyonu)
    path('expense/quick/', expense_views.expense_quick_index, name='expense_quick'),
    path('expense/bulk-save', expense_views.expense_bulk_save, name='expense_bulk_save'),

    # Kategori yönetimi
    path('expense/categories/', expense_views.expense_categories_index, name='expense_categories'),
    path('expense/categories/list', expense_views.expense_categories_list, name='expense_categories_list'),
    path('expense/categories/options', expense_views.expense_categories_options, name='expense_categories_options'),
    path('expense/categories/save', expense_views.expense_categories_save, name='expense_categories_save'),
    path('expense/categories/toggle', expense_views.expense_categories_toggle, name='expense_categories_toggle'),
    path('expense/categories/delete', expense_views.expense_categories_delete, name='expense_categories_delete'),

    # Raporlama
    path('expense/report/', expense_views.expense_report_index, name='expense_report'),
    path('expense/report/data', expense_views.expense_report_data, name='expense_report_data'),

    # İptal (REVERSAL)
    path('expense/reverse', expense_views.expense_reverse, name='expense_reverse'),

    # FAZ 65: Hızlı giriş üst KPI (today + month + top_category)
    path('expense/today-kpi', expense_views.expense_today_kpi, name='expense_today_kpi'),

    # Detay sayfasi
    path('<uuid:account_id>/', bank_management_detail, name='detail'),
    path('<uuid:account_id>/payments', bank_management_payments, name='detail_payments'),
    path('<uuid:account_id>/export', bank_management_export, name='export'),
]
