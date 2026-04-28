from django.urls import path
from apps.accounts.views import *

app_name = 'accounts'

urlpatterns = [
    path('index', index_view, name='index'),
    path('error', access_error, name='error'),

    path('add', add_view, name='add'),
    path('detail/<int:user_id>/', employee_detail_view, name='detail'),

    path('profile', profile_view, name='profile'),
    path('login', login_view, name='login'),
    path('logout', logout_view, name='logout'),

    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),

    path('get-all', get_all, name='get-all'),
    path('get-all-accounts/<uuid:record_id>', get_all_accounts, name='get-all-accounts'),
    path('change-two-factor/<int:record_id>', change_two_factor, name='change-two-factor'),

    path('reset-password', reset_password_view, name='reset-password'),
    path('reset-password-confirm/<uidb64>/<token>/', reset_password_confirm_view, name='reset-password-confirm'),
    path('staff-management/', staff_management_view, name='staff_management'),
    path('<int:user_id>/verify/send', send_user_verification, name='user_verify_send'),
    path('<int:user_id>/verify/confirm', confirm_user_verification, name='user_verify_confirm'),

    path('users/<uuid:user_id>/verify-state', user_verify_state, name='user-verify-state'),
    path('staffs-index', staffs_index_view, name='staffs-index'),
    path('staffs-get-all', staffs_get_all, name='staffs-get-all'),
    path('staffs-add', staffs_add, name='staffs-add'),
    path('staffs-delete', staffs_delete, name='staffs-delete'),
    path('staffs-change-status', staffs_change_status, name='staffs-change-status'),
    path('staffs-detail/<int:user_id>', staffs_detail_view, name='staffs-detail'),

]
