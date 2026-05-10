from decimal import Decimal

from django.contrib import admin
from django.utils.html import format_html

from apps.stock_management.models import (
    StockSnapshot,
    StockLedger,
    PriceProvider,
    PriceQuote,
    PriceProviderMapping,
)


# ============================================================================
# STOCK SNAPSHOT ADMIN
# ============================================================================

@admin.register(StockSnapshot)
class StockSnapshotAdmin(admin.ModelAdmin):
    list_display = [
        'product',
        'store',
        'display_stock_gram',
        'stock_pieces',
        'display_wac_hs',
        'display_total_value',
        'updated_on',
    ]
    list_filter = [
        'store',
    ]
    search_fields = [
        'product__name',
        'product__barcode',
    ]
    readonly_fields = [
        'product',
        'store',
        'stock_gram',
        'stock_pieces',
        'weighted_avg_cost_hs',
        'weighted_avg_cost_eur',
        'incoming_stock_gram',
        'incoming_stock_pieces',
        'updated_on',
        'created_on',
    ]
    list_per_page = 50
    ordering = ['-updated_on']

    def display_stock_gram(self, obj):
        gram = obj.stock_gram or Decimal('0')
        if gram > 0:
            return format_html('<span style="color: green;">{} g</span>', gram)
        elif gram == 0:
            return format_html('<span style="color: gray;">0 g</span>')
        else:
            return format_html('<span style="color: red;">{} g</span>', gram)
    display_stock_gram.short_description = 'Stok (Gram)'
    display_stock_gram.admin_order_field = 'stock_gram'

    def display_wac_hs(self, obj):
        return f"{obj.weighted_avg_cost_hs} HS"
    display_wac_hs.short_description = 'WAC (Has)'
    display_wac_hs.admin_order_field = 'weighted_avg_cost_hs'

    def display_total_value(self, obj):
        return f"{obj.total_value_hs} HS"
    display_total_value.short_description = 'Toplam Deger (Has)'

    def has_add_permission(self, request):
        """StockSnapshot SADECE servis katmanindan olusturulabilir."""
        return False

    def has_change_permission(self, request, obj=None):
        """StockSnapshot SADECE servis katmanindan guncellenebilir."""
        return False

    def has_delete_permission(self, request, obj=None):
        """StockSnapshot dogrudan silinemez."""
        return False


# ============================================================================
# STOCK LEDGER ADMIN
# ============================================================================

@admin.register(StockLedger)
class StockLedgerAdmin(admin.ModelAdmin):
    list_display = [
        'display_direction',
        'product',
        'store',
        'display_quantity',
        'display_reason',
        'display_ref',
        'display_cost',
        'created_by',
        'created_on',
    ]
    list_filter = [
        'direction',
        'reason',
        'store',
        'ref_type',
    ]
    search_fields = [
        'product__name',
        'product__barcode',
        'ref_id',
        'notes',
    ]
    readonly_fields = [
        'id',
        'product',
        'store',
        'direction',
        'reason',
        'quantity_gram',
        'quantity_pieces',
        'unit_cost_hs',
        'unit_cost_eur',
        'hs_rate_eur',
        'ref_type',
        'ref_id',
        'paired_entry',
        'notes',
        'created_by',
        'created_on',
    ]
    list_per_page = 100
    ordering = ['-created_on']
    date_hierarchy = 'created_on'

    def display_direction(self, obj):
        if obj.direction == StockLedger.Direction.IN:
            return format_html('<span style="color: green; font-weight: bold;">▲ GIRIS</span>')
        else:
            return format_html('<span style="color: red; font-weight: bold;">▼ CIKIS</span>')
    display_direction.short_description = 'Yon'
    display_direction.admin_order_field = 'direction'

    def display_quantity(self, obj):
        parts = []
        if obj.quantity_gram > 0:
            parts.append(f"{obj.quantity_gram}g")
        if obj.quantity_pieces > 0:
            parts.append(f"{obj.quantity_pieces} ad")
        return ' / '.join(parts) if parts else '-'
    display_quantity.short_description = 'Miktar'

    def display_reason(self, obj):
        return obj.get_reason_display()
    display_reason.short_description = 'Sebep'
    display_reason.admin_order_field = 'reason'

    def display_ref(self, obj):
        return f"{obj.ref_type}: {obj.ref_id[:12]}..."
    display_ref.short_description = 'Referans'

    def display_cost(self, obj):
        return f"{obj.unit_cost_hs} HS"
    display_cost.short_description = 'Birim Maliyet'

    def has_add_permission(self, request):
        """Ledger SADECE servis katmanindan olusturulabilir."""
        return False

    def has_change_permission(self, request, obj=None):
        """Ledger ASLA degistirilemez (immutable)."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Ledger ASLA silinemez (immutable)."""
        return False


# ============================================================================
# PRICE PROVIDER ADMIN
# ============================================================================

class PriceProviderMappingInline(admin.TabularInline):
    model = PriceProviderMapping
    extra = 1
    fields = [
        'source_code',
        'target_metal_type',
        'buy_field_name',
        'sell_field_name',
        'is_active',
    ]


@admin.register(PriceProvider)
class PriceProviderAdmin(admin.ModelAdmin):
    list_display = [
        'display_name',
        'name',
        'display_status',
        'priority',
        'poll_interval_seconds',
        'cache_ttl_seconds',
        'consecutive_errors',
        'last_success_at',
        'is_active',
    ]
    list_filter = [
        'status',
        'is_active',
        'provider_type',
    ]
    search_fields = [
        'name',
        'display_name',
    ]
    list_editable = [
        'priority',
        'is_active',
    ]
    ordering = ['priority']
    inlines = [PriceProviderMappingInline]

    fieldsets = (
        ('Temel Bilgiler', {
            'fields': (
                'name', 'display_name', 'provider_type',
                'status', 'is_active', 'priority',
            )
        }),
        ('API Baglanti', {
            'fields': (
                'base_url', 'api_key_setting', 'api_secret_setting',
                'extra_headers', 'extra_config',
            ),
            'classes': ('collapse',),
        }),
        ('Zamanlama', {
            'fields': (
                'poll_interval_seconds', 'cache_ttl_seconds', 'timeout_seconds',
            )
        }),
        ('Durum Takibi', {
            'fields': (
                'last_success_at', 'last_error_at', 'last_error_message',
                'consecutive_errors', 'max_consecutive_errors',
            ),
            'classes': ('collapse',),
        }),
    )

    def display_status(self, obj):
        colors = {
            'ACTIVE': 'green',
            'INACTIVE': 'gray',
            'ERROR': 'red',
            'MAINTENANCE': 'orange',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )
    display_status.short_description = 'Durum'
    display_status.admin_order_field = 'status'


# ============================================================================
# PRICE QUOTE ADMIN
# ============================================================================

@admin.register(PriceQuote)
class PriceQuoteAdmin(admin.ModelAdmin):
    list_display = [
        'display_metal',
        'display_provider',
        'buy_price_eur',
        'sell_price_eur',
        'spread_eur',
        'change_rate',
        'quoted_at',
    ]
    list_filter = [
        'metal_type',
        'provider',
        'quote_type',
    ]
    search_fields = [
        'currency_code',
        'provider__name',
    ]
    readonly_fields = [
        'id',
        'provider',
        'metal_type',
        'currency_code',
        'quote_type',
        'buy_price_eur',
        'sell_price_eur',
        'buy_price_hs',
        'sell_price_hs',
        'spread_eur',
        'change_rate',
        'raw_data',
        'quoted_at',
        'created_on',
    ]
    list_per_page = 100
    ordering = ['-quoted_at']
    date_hierarchy = 'quoted_at'

    def display_metal(self, obj):
        return obj.get_metal_type_display()
    display_metal.short_description = 'Metal/Doviz'
    display_metal.admin_order_field = 'metal_type'

    def display_provider(self, obj):
        return obj.provider.display_name
    display_provider.short_description = 'Saglayici'

    def has_add_permission(self, request):
        """Fiyatlar SADECE API/servis katmanindan eklenir."""
        return False

    def has_change_permission(self, request, obj=None):
        """Fiyat kayitlari degistirilemez."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Superuser eski kayitlari temizleyebilir."""
        return request.user.is_superuser


# ============================================================================
# PRICE PROVIDER MAPPING ADMIN
# ============================================================================

@admin.register(PriceProviderMapping)
class PriceProviderMappingAdmin(admin.ModelAdmin):
    list_display = [
        'provider',
        'source_code',
        'target_metal_type',
        'buy_field_name',
        'sell_field_name',
        'is_active',
    ]
    list_filter = [
        'provider',
        'target_metal_type',
        'is_active',
    ]
    list_editable = [
        'is_active',
    ]
    ordering = ['provider', 'source_code']
