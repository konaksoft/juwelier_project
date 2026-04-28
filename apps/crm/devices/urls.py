# apps/devices/urls.py
from django.urls import path
from . import views

app_name = 'devices'

urlpatterns = [
    path('index', views.index, name='index'),
    path('get-all', views.get_all, name='get_all'),
    path('add', views.add, name='add'),
    path('delete', views.delete, name='delete'),
    path('get-device-info', views.get_device_info, name='get_device_info'),
]