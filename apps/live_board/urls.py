from django.urls import path
from apps.live_board.views import (
    index_view,
    board_settings_view,
    live_board_settings_api,
    api_get_live_kitco_data,
)

app_name = 'live_board'

urlpatterns = [
    # Canlı piyasalar ana sayfası
    path('index/', index_view, name='index'),

    # Canlı ekran ayarları sayfası
    path('settings/', board_settings_view, name='board_settings'),

    # Canlı ekran ayarları API (GET / POST)
    path('api/live-board-settings/', live_board_settings_api, name='live_board_settings_api'),

    # Kitco canlı spot fiyat API (juwelier_plus port) — KitcoPriceCache OKUR
    path('api/kitco/', api_get_live_kitco_data, name='api_kitco_data'),
]
