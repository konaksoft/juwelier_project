from django.urls import path

from apps.definitions.rates.views import *

app_name = 'rates'

urlpatterns = [
    path('index', rates_view, name='index'),
    path('add', add_rate, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
]
