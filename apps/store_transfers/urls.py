from django.urls import path

from apps.store_transfers.views import (
    transfer_index,
    transfer_get_all,
    transfer_destination_stores,
    transfer_source_accounts,
    transfer_source_products,
    transfer_create_action,
    transfer_detail,
    transfer_dispatch_action,
    transfer_accept_action,
    transfer_reject_action,
    transfer_cancel_action,
)

app_name = 'store_transfers'

urlpatterns = [
    path('', transfer_index, name='index'),
    path('get-all', transfer_get_all, name='get_all'),

    # Modal helper'ları
    path('destination-stores', transfer_destination_stores, name='destination_stores'),
    path('source-accounts', transfer_source_accounts, name='source_accounts'),
    path('source-products', transfer_source_products, name='source_products'),  # FAZ 47

    # CRUD
    path('create', transfer_create_action, name='create'),
    path('<uuid:transfer_id>/detail', transfer_detail, name='detail'),
    path('<uuid:transfer_id>/dispatch', transfer_dispatch_action, name='dispatch'),
    path('<uuid:transfer_id>/accept', transfer_accept_action, name='accept'),
    path('<uuid:transfer_id>/reject', transfer_reject_action, name='reject'),
    path('<uuid:transfer_id>/cancel', transfer_cancel_action, name='cancel'),
]
