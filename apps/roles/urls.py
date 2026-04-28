from django.urls import path

from apps.roles.views import *

app_name = 'roles'

urlpatterns = [
    path('index', roles_view, name='index'),
    path('add', add_role, name='add'),
    path('detail/<uuid:record_id>', detail_role, name='detail'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),

    path('get-all', get_all, name='get-all'),
    path('get-users-role/<uuid:record_id>', get_users_role, name='get-users-role'),
]
