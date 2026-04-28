from django.urls import path

from apps.whatsapp.views import (
    dashboard,
    conversation_detail,
    meta_whatsapp_webhook,
    send_meta_template_test,
)
from apps.whatsapp.api_views import *

app_name = "whatsapp"

urlpatterns = [
    # SAYFALAR
    path("dashboard/", dashboard, name="dashboard"),
    path("conversation/<int:pk>/", conversation_detail, name="conversation_detail"),

    # WEBHOOK & TEST
    path("meta/webhook/", meta_whatsapp_webhook, name="meta_whatsapp_webhook"),
    path("send-meta-template-test/", send_meta_template_test, name="send_meta_template_test"),

    # API’ler (tamamı admin-only json)
    path("api/store-list", api_store_list, name="api_store_list"),
    path("api/conversations/", api_conversations, name="api_conversations"),
    path("api/conversations/<int:conv_id>/messages/", api_messages, name="api_messages"),
    path("api/send-text", api_send_text, name="api_send_text"),
    path("api/usage", api_usage, name="api_usage"),
    path("api/templates/", api_templates, name="api_templates"),
    path("api/templates/save", api_templates_save, name="api_templates_save"),
    path("api/template-logs", api_template_logs, name="api_template_logs"),
    path("api/start-chat", api_start_chat, name="api_start_chat"),
    path("api/user-totals", api_user_totals, name="api_user_totals"),
    path("api/store-quotas/", api_store_quotas, name="api_store_quotas"),
    path("api/store-quotas/save", api_store_quotas_save, name="api_store_quotas_save"),
    path("api/credit-requests", api_credit_requests, name="api_credit_requests"),
    path("api/credit-requests/create", api_credit_request_create, name="api_credit_request_create"),
]
