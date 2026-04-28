from django.urls import path

from apps.definitions.brands.views import *

app_name = 'brands'

urlpatterns = [
    path('index', brands_view, name='index'),
    path('add', add_brand, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
]
