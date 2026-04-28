from django.urls import path
from apps.process.views import *
from apps.process.retail_views import *
from apps.process.fast_views import *
from apps.process.wholesale_views import *
from apps.process.operations import *

app_name = 'process'

urlpatterns = [
    path('index', process_index, name='index'),

    path("ops/detail/", ops_detail, name="ops_detail"),
    path("ops/cancel-row/", cancel_row, name="cancel_row"),
    path("ops/cancel-group/", cancel_group, name="cancel_group"),

    path('add-process', add_process, name='add-process'),
    path('add-scrap-to-process', add_scrap_to_process, name='add_scrap_to_process'),  # <--- YENİ
    path("detail/<str:process_no>/", process_detail_page, name="detail"),  # ← YENİ

    path('add-wholesale-process', add_wholesale_process, name='add-wholesale-process'),
    path('add-wholesale-cash-item', add_wholesale_cash_item, name='add-wholesale-cash-item'),
    path('add-fast-process', add_fast_process, name='add-fast-process'),
    path('check-fast-stock', check_fast_stock, name='check-fast-stock'),
    path('check-retail-compliance', check_retail_compliance, name='check-retail-compliance'),

    path('complete-process', complete_process, name='complete-process'),
    path('complete-process-wholesale', complete_process_wholesale, name='complete-process-wholesale'),
    path('convert-debt', convert_debt, name='convert-debt'),
    path('open-binding', open_binding, name='open-binding'),
    path('delete', delete, name='delete'),
    path('get-all', get_all, name='get-all'),
    path('get-sales', get_sales, name='get-sales'),
    path('get-sales-wholesale', get_sales_wholesale, name='get-sales-wholesale'),
    path('get-parities', get_parities, name='get-parities'),
    path('get-process-details', get_process_details, name='get-process-details'),
    path("receipt/<str:process_no>/", process_receipt_view, name="process-receipt"),
    path('add-scrap-to-wholesale', add_scrap_to_wholesale_process, name='add-scrap-wholesale'),
    path('add-bracelet-to-wholesale', add_bracelet_to_wholesale_process, name='add-bracelet-wholesale'),
path('process-detail/<str:process_no>/', process_detail_view, name='process_detail'),
path('add-bracelet-to-retail-process', add_bracelet_to_retail_process, name='add-bracelet-retail'),

    # FAZ 21: Tedarikçi nakit ödeme (kasa entegrasyonu)
    path('supplier-cash-payment', supplier_cash_payment, name='supplier-cash-payment'),

    # Çoklu Hurda Girişi (Multi-Row)
    path('add-scrap-multi-to-wholesale', add_scrap_multi_to_wholesale_process, name='add-scrap-multi-wholesale'),

    # Bekleyen stok tamamlama
    path('fulfill-waiting-stock', fulfill_waiting_stock, name='fulfill-waiting-stock'),

    # FAZ: Detaylı Kârlılık Raporu (kategori bazlı)
    path('profit-report', profit_report_data, name='profit_report'),
    path('profit-report-pdf', profit_report_pdf, name='profit_report_pdf'),
]
