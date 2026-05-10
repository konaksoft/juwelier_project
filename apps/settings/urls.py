from django.urls import path
from . import views

app_name = 'settings'

urlpatterns = [
    path('update-config', views.update_configuration, name='update_config'),
    path('update-label-settings', views.update_label_settings, name='update_label_settings'),
    path('update-store-display', views.update_store_display, name='update_store_display'),
    # T3 (2026-04-29): Manuel Kur (Döviz) toplu güncelleme
    path('update-manual-currency-rates', views.update_manual_currency_rates,
         name='update_manual_currency_rates'),
]
