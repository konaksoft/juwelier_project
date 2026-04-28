from django.urls import path

from apps.customers.views import *

app_name = 'customers'

urlpatterns = [
    path('index', customers_view, name='index'),
    path('add', add_customer, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
    path('get-customers', get_customers, name='get-customers'),
    path('get-detail', get_customer_detail_json, name='get-detail-json'),
    path('detail/<uuid:customer_id>', customer_detail_view, name='detail'),
    path('<uuid:customer_id>/verify/state', customer_verify_state, name='customer-verify-state'),
    path('<uuid:customer_id>/verify/send', send_customer_verification, name='customer-verify-send'),
    path('<uuid:customer_id>/verify/confirm', confirm_customer_verification, name='customer-verify-confirm'),
    path('detail/<str:customer_id>/transactions', get_customer_transactions, name='detail-transactions'),
    path('save-debt-collection', save_debt_collection, name='save_debt_collection'),

]
