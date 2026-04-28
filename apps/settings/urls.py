from django.urls import path
from . import views

app_name = 'settings'

urlpatterns = [
    path('update-config', views.update_configuration, name='update_config'),
    path('update-label-settings', views.update_label_settings, name='update_label_settings'),
    path('update-store-display', views.update_store_display, name='update_store_display'),
]
