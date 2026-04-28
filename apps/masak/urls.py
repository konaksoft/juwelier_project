from django.urls import path
from apps.masak.views import *

app_name = 'masak'

urlpatterns = [
    # Mevcut private rotalar
    path('index', masak_index, name='index'),
    path('get-logs', get_query_logs, name='get_logs'),
    path('get-blacklist', get_blacklist, name='get_blacklist'),
    path('add-blacklist', add_blacklist_item, name='add_blacklist'),
    path('delete-blacklist', delete_blacklist_item, name='delete_blacklist'),
    path('upload-data', upload_masak_data, name='upload_page'),
    path('check-risk', check_customer_risk, name='check_risk'),

    # --- YENİ: MASAK Müşteri Tanı Formu ---
    # Public (auth'suz) rotalar
    path('public/form/<uuid:store_token>/', masak_public_form, name='public_form'),
    path('public/form/<uuid:store_token>/success/', masak_public_success, name='public_success'),

    # Private rotalar
    path('print/<uuid:customer_id>/', masak_print_view, name='print'),
    path('qr-modal/', masak_qr_modal, name='qr_modal'),
    path('toggle-iys', masak_toggle_iys_consent, name='toggle_iys'),
]
