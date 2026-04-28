from django.urls import path
from apps.bracelets.views import *

app_name = 'bracelets'

urlpatterns = [
    path('index', bracelet_index, name='index'),
    path('add', bracelet_add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),

    # Havuz (pool) yönetimi — 2026-04-21
    path('get-pool-sources', get_pool_sources, name='get-pool-sources'),
    path('get-pool-contents', get_pool_contents, name='get-pool-contents'),
]
