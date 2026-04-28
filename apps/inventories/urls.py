from django.urls import path
from apps.inventories.views import *

app_name = 'inventories'

urlpatterns = [
    path('get-all/', get_all, name='get_all'),
]
