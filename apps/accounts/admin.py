from django.contrib import admin
from apps.accounts.models import Company
from apps.backups.services import BackupService
from django.contrib import messages


@admin.action(description='Seçili Firmaların Tam Yedeğini Al')
def create_company_full_backup(modeladmin, request, queryset):
    success_count = 0
    for company in queryset:
        service = BackupService(company.id)
        backup = service.create_backup(note="Admin Panelinden Manuel Yedek", user=request.user)
        if backup:
            success_count += 1

    modeladmin.message_user(request, f"{success_count} firmanın yedeği başarıyla alındı.", messages.SUCCESS)


class CompanyAdmin(admin.ModelAdmin):
    list_display = ['title', 'tax_number', 'is_active']
    actions = [create_company_full_backup]


admin.site.register(Company, CompanyAdmin)
