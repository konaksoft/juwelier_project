# apps/orders/urls.py
from django.urls import path
from apps.orders.views import *

app_name = 'orders'

urlpatterns = [
    path('index', index, name='index'),
    path('detail/<uuid:pk>/', detail, name='detail'),
    path('api/list', api_orders_list, name='api_list'),
    path('api/decide', api_order_decide, name='api_decide'),
    path('api/bulk-decide', api_orders_bulk_decide, name='api_bulk_decide'),
    path('api/delete', api_orders_delete, name='api_delete'),
    path('api/set-paid', api_order_set_paid, name='api_set_paid'),
]