from django.urls import path

from apps.transactions_board.views import *

app_name = 'transactions-board'

urlpatterns = [
    path('fast-index', fast_index_view, name='fast-index'),
    path('retail-index', retail_index_view, name='retail-index'),
    path('wholesale-index', wholesale_index_view, name='wholesale-index'),
    path("operations", operations_index, name="operations-index"),
]
