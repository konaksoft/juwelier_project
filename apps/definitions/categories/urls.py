from django.urls import path
from apps.definitions.categories.views import *

app_name = 'categories'

urlpatterns = [
    path('index', categories_index, name='index'),
    path('add', category_add, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),
    path('get-categories', get_categories_with_products, name='get-categories'),
    path('get-categories-wholesale', get_categories_with_products_wholesale, name='get-categories-wholesale'),

]
