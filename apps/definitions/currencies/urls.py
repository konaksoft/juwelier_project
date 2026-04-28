from django.urls import path
from apps.definitions.currencies.views import *

app_name = 'currencies'

urlpatterns = [
    path('index', currencies_view, name='index'),
    path('add', add_currency, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
]
