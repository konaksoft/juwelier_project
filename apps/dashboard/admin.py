from django.contrib import admin
from apps.dashboard.models import DailyStoreReport, DailyEmployeeReport, GeneratedReports


@admin.register(DailyStoreReport)
class DailyStoreReportAdmin(admin.ModelAdmin):
    list_display = (
        'store', 'report_date', 'sale_count', 'total_sales_eur',
        'total_gross_profit', 'transaction_count', 'computed_at',
    )
    list_filter = ('store', 'report_date')
    search_fields = ('store__name',)
    date_hierarchy = 'report_date'
    readonly_fields = ('computed_at',)
    ordering = ('-report_date',)


@admin.register(DailyEmployeeReport)
class DailyEmployeeReportAdmin(admin.ModelAdmin):
    list_display = (
        'store', 'employee', 'report_date', 'sale_count',
        'total_sales_eur', 'total_gross_profit', 'transaction_count',
    )
    list_filter = ('store', 'report_date')
    search_fields = ('employee__first_name', 'employee__last_name')
    date_hierarchy = 'report_date'
    readonly_fields = ('computed_at',)
    ordering = ('-report_date',)


@admin.register(GeneratedReports)
class GeneratedReportsAdmin(admin.ModelAdmin):
    list_display = ('task_id', 'report_type', 'status', 'created_at')
    list_filter = ('status', 'report_type')
    search_fields = ('task_id',)
    ordering = ('-created_at',)
