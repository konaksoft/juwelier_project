from django.urls import path
from apps.backups.views import *

app_name = 'backups'

urlpatterns = [
    path('api/get-all/', get_company_backups, name='get-all'),
    path('api/create/', create_backup, name='create'),
    path('api/restore/', restore_backup, name='restore'),  # Yeni eklenen
path('api/check-status/', check_backup_status, name='check_status'),
    path('download/<uuid:backup_id>/', download_backup, name='download'),
]
