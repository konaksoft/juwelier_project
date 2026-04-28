from django.urls import path

from apps.custody.views import *

app_name = 'custody'

urlpatterns = [
    path('', custody_index, name='index'),
    path('get-all', custody_get_all, name='get_all'),
    path('add', add_custody, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change_status'),
]