from django.urls import path
from apps.activity_logs.views import *

app_name = 'activity-logs'

urlpatterns = [
    path('index', activity_logs_view, name='index'),
    path('get-all', get_all, name='get-all'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
]
