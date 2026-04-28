# apps/testimonials/urls.py
from django.urls import path
from apps.testimonials.views import index, add, delete, change_status, get_all

app_name = 'testimonials'

urlpatterns = [
    path('index', index, name='index'),
    path('add', add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),
]