from django.contrib import admin
from apps.crm.packages.models import SaaSModule, Packages, PackagePermissionMatrix


@admin.register(SaaSModule)
class SaaSModuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'price_monthly', 'price_yearly', 'is_core', 'is_active', 'order')
    list_filter = ('is_active', 'is_core')
    list_editable = ('order', 'is_active', 'is_core', 'price_monthly', 'price_yearly')
    search_fields = ('name', 'slug', 'description')
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ('dependencies', 'permissions')
    fieldsets = (
        (None, {
            'fields': ('name', 'slug', 'description', 'icon')
        }),
        ('Fiyatlandırma', {
            'fields': ('price_monthly', 'price_yearly')
        }),
        ('Yapılandırma', {
            'fields': ('is_core', 'is_active', 'order')
        }),
        ('Bağımlılıklar & Yetkiler', {
            'fields': ('dependencies', 'permissions'),
            'description': 'Bağımlılıklar: Bu modül seçildiğinde otomatik eklenen modüller. '
                           'Yetkiler: Modül aktifleştirildiğinde mağazaya verilecek Permission kayıtları.'
        }),
    )


@admin.register(Packages)
class PackagesAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'price_license', 'currency', 'is_active', 'is_recommended', 'order')
    list_filter = ('is_active', 'currency')
    list_editable = ('order', 'is_active')
    search_fields = ('name', 'code')


class PackagePermissionMatrixInline(admin.TabularInline):
    model = PackagePermissionMatrix
    extra = 1
