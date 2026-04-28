from django.urls import path
from apps.definitions.contracts.views import *

app_name = 'contracts'

urlpatterns = [
    # Yönetim Paneli
    path('index', contract_view, name='index'),
    path('get-all', get_all_contracts, name='get-all'),
    path('save', save_contract, name='save'),  # Ekle ve Düzenle tek url
    path('delete', delete_contract, name='delete'),
    path('change-status', change_status_contract, name='change-status'),
    path('start-process', start_contract_process, name='start-process'),
    path('resend-contract-mail', resend_contract_mail, name='resend-contract-mail'),

    # Public (Müşteri)
    path('public/view/<uuid:token>', public_contract_view, name='public_view'),
    path('api/send-sms', public_send_sms, name='public_send_sms'),
    path('api/confirm', public_confirm_contract, name='public_confirm'),
    path('test-sms', test_sms_send, name='test_sms'),
    path('public/download/<uuid:token>/', download_signed_contract_pdf, name='download_pdf'),

]
