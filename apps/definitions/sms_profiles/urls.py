from django.urls import path

from apps.definitions.sms_profiles.views import *

app_name = 'sms-profiles'

urlpatterns = [
    path('index', sms_profiles_view, name='index'),
    path('add', add_sms_profile, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
]
