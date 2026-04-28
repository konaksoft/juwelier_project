from django.urls import path
from apps.invoices.views import *
from apps.invoices.api_views import *
from apps.invoices.esurec_views import *

app_name = 'invoices'

urlpatterns = [
    # --- Sayfalar (Pages) ---
    path('index', invoices_index, name='index'),
    path('dashboard', dashboard_index, name='dashboard'),
    path('dashboard-metrics', dashboard_metrics, name='dashboard-metrics'),

    # Satış Faturası Detayı
    path('detail/<uuid:record_id>', invoice_detail_page, name='detail'),

    # Gider Pusulası Detayı (Alış İşlemleri İçin)
    path('expense-note/<uuid:record_id>', expense_note_detail_page, name='expense-note-detail'),

    # --- API / İşlemler (JSON) ---
    path('get-all', get_all, name='get-all'),
    path('detail-json/<uuid:record_id>', invoice_detail_json, name='detail-json'),
    path('add', add_invoice, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),

    # Kalem İşlemleri (Proforma Düzenleme İçin Gerekli)
    path('<uuid:invoice_id>/item', add_or_update_item, name='item-add-or-update'),
    path('<uuid:invoice_id>/item/<int:item_id>/delete', delete_item, name='item-delete'),
    path('product-brief', product_brief, name='product-brief'),

    # Ödeme Tahsisi
    path('allocate', allocate_payment, name='allocate'),
    path('<uuid:record_id>/allocations', get_invoice_allocations, name='allocations'),

    # --- Process Entegrasyonu ---
    path('create-from-process-group', create_from_process_group_view, name='create-from-process-group'),

    # --- Serbest (Bağımsız) Fatura ---
    path('create-free', create_free_invoice_view, name='create-free'),
    path('save-free', save_free_invoice, name='save-free'),

    # --- Çıktı ve Entegrasyon ---
    path("<uuid:record_id>/download", invoice_pdf_download, name="download"),

    # PERF-04: Asenkron PDF üretimi (Celery) — sync endpoint korunuyor, frontend aşamalı geçiş yapacak.
    path("<uuid:record_id>/pdf/async", invoice_pdf_async_request, name="pdf-async-request"),
    path("pdf/status/<str:task_id>", invoice_pdf_async_status, name="pdf-async-status"),
    path("<uuid:record_id>/pdf/result", invoice_pdf_async_result, name="pdf-async-result"),

    # e-Fatura API
    path("api/einvoice/quota", api_einvoice_quota, name="api_einvoice_quota"),
    path("api/einvoice/requests", api_einvoice_requests, name="api_einvoice_requests"),
    path("api/einvoice/requests/create", api_einvoice_request_create, name="api_einvoice_request_create"),

    # e-Süreç API (E-Fatura)
    path("api/esurec/send/", esurec_send_invoice, name="esurec-send"),
    path("api/esurec/send-to-gib/", esurec_send_to_gib, name="esurec-send-to-gib"),
    path("api/esurec/status/", esurec_check_status, name="esurec-status"),
    path("api/esurec/queue-status/", esurec_queue_status, name="esurec-queue-status"),
    path("api/esurec/pdf/", esurec_get_pdf, name="esurec-pdf"),
    path("api/esurec/xml/", esurec_get_xml, name="esurec-xml"),
    path("api/esurec/cancel/", esurec_cancel_invoice, name="esurec-cancel"),
    path("api/esurec/reset-to-draft/", esurec_reset_to_draft, name="esurec-reset-to-draft"),
    path("api/esurec/gib-check/", esurec_check_gib_user, name="esurec-gib-check"),

    # e-Gider Pusulası API (2026-04-18: genişletildi)
    path("api/esurec/send-expense/", esurec_send_expense, name="esurec-send-expense"),
    path("api/esurec/send-expense-to-gib/", esurec_send_expense_to_gib, name="esurec-send-expense-to-gib"),
    path("api/esurec/expense-status/", esurec_expense_status, name="esurec-expense-status"),
    path("api/esurec/cancel-expense/", esurec_cancel_expense, name="esurec-cancel-expense"),
    path("api/esurec/reset-expense-to-draft/", esurec_reset_expense_to_draft, name="esurec-reset-expense-to-draft"),
    path("api/esurec/expense-async-send/", esurec_expense_async_send, name="esurec-expense-async-send"),

]
