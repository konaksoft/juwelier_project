from django.urls import path
from apps.chambers.views import (
    index_view, add_chamber, delete_chamber, change_status, get_all,
    detail_view, get_companies, get_chamber_products, update_chamber_prices,
    get_available_companies, add_company_to_chamber, remove_company_from_chamber,
    chamber_dashboard_view, chamber_dashboard_stores,
    chamber_dashboard_products, chamber_dashboard_save_prices,
    get_available_president_users, assign_president_user, quick_create_president,
)

app_name = 'chambers'

urlpatterns = [
    # --- Admin Dernek Yönetimi (mevcut) ---
    path('index', index_view, name='index'),
    path('add', add_chamber, name='add'),
    path('delete', delete_chamber, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
    path('detail/<uuid:record_id>/', detail_view, name='detail'),
    path('get-companies/<uuid:record_id>/', get_companies, name='get-companies'),
    path('get-chamber-products/<uuid:record_id>', get_chamber_products, name='get-chamber-products'),
    path('update-chamber-prices/', update_chamber_prices, name='update-chamber-prices'),
    path('get-available-companies/<uuid:record_id>', get_available_companies, name='get-available-companies'),
    path('add-company', add_company_to_chamber, name='add-company'),
    path('remove-company', remove_company_from_chamber, name='remove-company'),

    # --- Başkan Atama Endpoint'leri (Admin) ---
    path('get-available-presidents/<uuid:record_id>', get_available_president_users, name='get-available-presidents'),
    path('assign-president', assign_president_user, name='assign-president'),
    path('quick-create-president', quick_create_president, name='quick-create-president'),

    # --- Dernek Başkanı Paneli ---
    path('dashboard/', chamber_dashboard_view, name='dashboard'),
    path('dashboard/api/stores/', chamber_dashboard_stores, name='dashboard-stores'),
    path('dashboard/api/products/', chamber_dashboard_products, name='dashboard-products'),
    path('dashboard/api/save-prices/', chamber_dashboard_save_prices, name='dashboard-save-prices'),
]
