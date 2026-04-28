from django.urls import path
from rest_framework.routers import DefaultRouter
from apps.pavo.views import *
from apps.pavo.views_settings import *

app_name = 'pavo'

urlpatterns = [
    # Pavo Cloud
    path('v1/pavo/create-payment/', PavoCreatePaymentView.as_view(), name='pavo-create-payment'),
    path('v1/pavo/status/<str:pavo_id>/', PavoPaymentStatusView.as_view(), name='pavo-status'),
    path('v1/pavo/webhook/', PavoWebhookView.as_view(), name='pavo-webhook'),

    # Pavo Local Device
    path('v1/pavo-local/pair/', PavoLocalPairView.as_view(), name='pavo-local-pair'),
    path('v1/pavo-local/jewellery-sale/', PavoLocalJewellerySaleView.as_view(), name='pavo-local-jewellery-sale'),
    path('v1/pavo-local/get-sale-result/', PavoLocalGetSaleResultView.as_view(), name='pavo-local-get-sale-result'),
    path('v1/pavo-local/cancel/', PavoLocalCancelView.as_view(), name='pavo-local-cancel'),

    # Demo local finalize
    path('v1/pavo-local/demo-complete/', pavo_local_jewellery_sale, name='pavo-local-demo-complete'),

    # E-Doc
    path('v1/edoc/status/', edoc_status, name='edoc-status'),
    path('v1/edoc/send/', edoc_send, name='edoc-send'),
    path('v1/edoc/cancel/', edoc_cancel, name='edoc-cancel'),
    path('v1/edoc/download-pdf/', edoc_download_pdf, name='edoc-download-pdf'),
    path('v1/edoc/download-xml/', edoc_download_xml, name='edoc-download-xml'),
    path("settings/", pavo_settings_view, name="settings"),
    path("settings/test-pairing/", pavo_terminal_pairing_test_view, name="terminal-pairing-test"),
    path("debug/connect", pavo_debug_connect, name="debug-connect"),
]
