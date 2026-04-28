from django.urls import path
from apps.live_board.views import (
    index_view,
    get_live_data,
    board_settings_view,
    live_board_settings_api,
)

app_name = 'live_board'

urlpatterns = [
    # Canli piyasalar ana sayfasi
    path('index/', index_view, name='index'),

    # Canli fiyat verisi API (5sn polling)
    path('api/data/', get_live_data, name='api_data'),

    # Canli ekran ayarlari sayfasi (superuser)
    path('settings/', board_settings_view, name='board_settings'),

    # Canli ekran ayarlari API (GET / POST, superuser for POST)
    path('api/live-board-settings/', live_board_settings_api, name='live_board_settings_api'),
]
