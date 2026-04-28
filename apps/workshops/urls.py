from django.urls import path

from apps.workshops.views import *

app_name = 'workshops'

urlpatterns = [
    path('index', workshops_view, name='index'),
    path('detail/<uuid:record_id>', workshop_detail, name='detail'),
    path('add', add_workshop, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),

    path('send-bulk-report', send_bulk_report_mail, name='send-bulk-report'),  # Maili tetikler
    path('public-report/<str:token>', public_workshop_report_view, name='public-report'),  # Linkin açıldığı yer

]
