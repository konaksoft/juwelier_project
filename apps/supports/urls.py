from django.urls import path

from apps.supports.views import *

app_name = 'supports'

urlpatterns = [
    path('index', supports_view, name='index'),
    path('index-admin', supports_admin_view, name='index-admin'),
    path('index-detail/<uuid:id>', supports_detail, name='index-detail'),
    path('add', add_supports, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
    path('get-all-admin', get_all_admin, name='get-all-admin'),
    path('get-personels', get_personels, name='get-personels'),
    path('get-customers', get_customers, name='get-customers'),
    path('set-personel', set_personel, name='set_personel'),
    path('get-message-detail', get_message_detail, name='get-message-detail'),
    path('send-message', send_message, name='send-message'),
    path('support-end', support_end, name='suppoert_end'),

    path('<uuid:customer_id>/verify/confirm', confirm_customer_verification, name='customer-verify-confirm'),
    path('detail/<str:customer_id>/transactions', get_customer_transactions, name='detail-transactions'),
    path('toggle-status', toggle_support_status, name='toggle-status'),
    path('reactivate', reactivate_support, name='reactivate'),

    path('add-training-video', add_training_video, name='add-training-video'),
    path('delete-training-video', delete_training_video, name='delete-training-video'),
]
