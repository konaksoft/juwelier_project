from django.urls import path
from apps.crm.packages.views import *

app_name = 'packages'

urlpatterns = [
    # --- Paket (Packages) Yönetimi ---
    path('index', packages_index, name='index'),
    path('add', add_package, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
    path('<uuid:pk>/', package_detail, name='detail'),

    # --- SaaS Modül Yönetimi ---
    path('modules/', saas_module_index, name='module-index'),
    path('modules/get-all', saas_module_get_all, name='module-get-all'),
    path('modules/create', saas_module_create, name='module-create'),
    path('modules/<uuid:pk>/edit', saas_module_update, name='module-update'),
    path('modules/delete', saas_module_delete, name='module-delete'),
    path('modules/change-status', saas_module_change_status, name='module-change-status'),
]
