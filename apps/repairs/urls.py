from django.urls import path
from apps.repairs.views import *

app_name = 'repairs'

urlpatterns = [
    path('index', repair_index, name='index'),
    path('add', repair_add, name='add'),
    path('get-all', get_all, name='get_all'),
    path('get-workshop-product', get_workshop_product, name='get-workshop-product'),
    path('get-repair-details/<uuid:repair_id>/', get_repair_details, name='get-repair-details'),
    path("detail/<str:token>", public_detail, name="public-detail"),

    path('delete', delete_repairs, name='delete'),
    path('change-status', change_repair_status, name='change-status'),
    path('change-status-product', change_status, name='change-status-product'),
    path("receipt/<uuid:repair_id>/", repair_receipt_view, name="repair-receipt"),
    path("change-status-bulk", change_status_bulk, name="change-status-bulk"),

]

