# apps/definitions/locations/urls.py

from django.urls import path
from . import views

app_name = 'locations'  # Namespace hatasını çözen kısım burası

urlpatterns = [
    # Selector View (Opsiyonel)
    path('selector/', views.location_selector_view, name='selector'),

    # API Endpoints
    path('api/get-cities/', views.get_cities, name='get-cities'),
    path('api/get-districts/', views.get_districts, name='get-districts'),
    path('api/get-tax-offices/', views.get_tax_offices, name='get-tax_offices'),
]