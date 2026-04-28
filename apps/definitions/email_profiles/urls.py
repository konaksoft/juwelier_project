from django.urls import path

from apps.definitions.email_profiles.views import *

app_name = 'email-profiles'

urlpatterns = [
    path('index', email_profiles_view, name='index'),
    path('add', add_email_profile, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
]