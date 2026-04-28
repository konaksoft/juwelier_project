from django.urls import path
from apps.counts.views import *

app_name = 'counts'

urlpatterns = [
    path('index', counts_index, name='index'),
    path('get-stock-count-data', get_stock_count_data, name='get-stock-count-data'),
    path('scan-barcode-for-count', scan_barcode_for_count, name='scan-barcode-for-count'),
    path('bulk-scan', bulk_scan_for_count, name='bulk-scan'),
    path('session/start', start_or_continue_session, name='start_or_continue_session'),
    path('close-session', close_session, name='close-session'),
    path('reset-session', reset_session, name='reset-session'),
    path('download-pdf/<uuid:session_id>/', download_inventory_pdf, name='download_inventory_pdf'),
    path('get-product-image', get_product_image, name='get_product_image'),
    path('preview-report/<uuid:session_id>/', preview_report, name='preview_report'),
    # FAZ 2: Kapsam seçenekleri (kategori/materyal/marka/mücevher tipi listesi)
    path('scope-options', scope_options, name='scope_options'),
]
