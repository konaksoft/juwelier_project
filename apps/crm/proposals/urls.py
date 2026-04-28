# apps/proposals/urls.py
from django.urls import path
from apps.crm.proposals.views import *

app_name = 'proposals'

urlpatterns = [
    path('index', index, name='index'),
    path('get-all', get_all, name='get_all'),
    path('add', add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('update-status', update_status, name='update_status'),  # YENİ
    path('detail/<uuid:pk>', detail, name='detail'),
    path('get-package-info', get_package_info, name='get_package_info'),
    path('get-device-info', get_device_info, name='get_device_info'),
    path('package-details/<uuid:pk>', package_details, name='package_details'),
    path('history/<uuid:pk>/', history, name='history'),
]
