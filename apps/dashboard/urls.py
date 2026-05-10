from django.urls import path

from apps.dashboard.views import *

app_name = 'dashboard'

urlpatterns = [
    path('index', index_view, name='index'),
    path('data/', dashboard_data, name='dashboard_data'),
    path('summary_data/', get_summary_data, name='summary_data'),
    path('assets-summary/', get_assets_summary, name='assets_summary'),
    # FAZ 26 (2026-05-01): Patron Odaklı Dashboard — TAB 1 (Mağaza Varlıkları)
    path('assets-v2/', assets_v2_view, name='assets_v2'),
    path('top_customers_by_sales/', get_top_customers_by_sales, name='get_top_customers_by_sales'),
    path('generate-report', generate_report, name='generate-report'),
    path("generate-currency-report", generate_currency_report, name="dashboard_generate_currency_report"),
    path("generate-current-stock-report", generate_current_stock_report, name="generate_current_stock_report"),
    path("generate-bank-balance-report", generate_bank_balance_report, name="generate_bank_balance_report"),
    path("generate-profit-report", generate_profit_report, name="generate_profit_report"),
    path("generate-customer-report", generate_customer_report, name="generate_customer_report"),
    path("generate-customer-detail-report", generate_customer_detail_report, name="generate_customer_detail_report"),
    path("get-customers-for-report", get_customers_for_report, name="get_customers_for_report"),
    path("queue-report", queue_report, name="queue-report"),
    path("check-report-status/<str:task_id>", check_report_status, name="check-report-status"),

    # ── FAZ R-2 / R-4 / R-5: Yeni Rapor API Endpoint'leri ──
    path("api/dashboard-kpi", api_dashboard_kpi, name="api_dashboard_kpi"),
    path("api/inventory-value", api_inventory_value, name="api_inventory_value"),
    path("api/employee-performance", api_employee_performance, name="api_employee_performance"),
    path("api/supplier-ledger", api_supplier_ledger, name="api_supplier_ledger"),
    path("api/date-range-summary", api_date_range_summary, name="api_date_range_summary"),
]
