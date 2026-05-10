from django.contrib import admin

from apps.products.models import Products, DiamondDetail, WatchDetail


# ============================================================================
# FAZ B2 / ONARIM FAZI 1: Products Admin - material_type Readonly Koruma
# ============================================================================
# material_type alani olusturma ekraninda serbest, duzenleme ekraninda
# readonly'dir. Bu sadece bir UX korumasidir; esas backend zorunlulugu
# Products.clean() metodunda (ValidationError) tanimlanmistir.
# ============================================================================


class DiamondDetailInline(admin.StackedInline):
    model = DiamondDetail
    extra = 0
    can_delete = False
    verbose_name = "Pirlanta Detayi"
    verbose_name_plural = "Pirlanta Detaylari"


class WatchDetailInline(admin.StackedInline):
    model = WatchDetail
    extra = 0
    can_delete = False
    verbose_name = "Saat Detayi"
    verbose_name_plural = "Saat Detaylari"


@admin.register(Products)
class ProductsAdmin(admin.ModelAdmin):
    """
    Products yonetim admin arayuzu.

    Duzenleme (change) ekraninda 'material_type' READONLY gosterilir.
    Olusturma (add) ekraninda ise kullanici serbestce secim yapabilir.
    """
    list_display = (
        'name', 'barcode', 'material_type',
        'gram', 'sale_price_eur', 'is_active',
    )
    list_filter = (
        'material_type', 'is_active', 'is_scrap',
        'is_currency', 'store',
    )
    search_fields = (
        'name', 'barcode', 'brand', 'jewelry_type',
    )
    inlines = [WatchDetailInline, DiamondDetailInline]

    def get_readonly_fields(self, request, obj=None):
        """
        material_type:
          - add (obj=None)    -> duzenlenebilir
          - change (obj!=None)-> readonly (IMMUTABILITY)
        """
        base = list(super().get_readonly_fields(request, obj) or ())
        if obj is not None:
            # Degistirme ekraninda material_type'i kilitle
            if 'material_type' not in base:
                base.append('material_type')
        return tuple(base)


@admin.register(DiamondDetail)
class DiamondDetailAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'carat_weight', 'shape',
        'color_grade', 'clarity_grade', 'cut_grade',
        'certificate_lab', 'certificate_no',
    )
    list_filter = (
        'shape', 'color_grade', 'clarity_grade',
        'cut_grade', 'certificate_lab',
    )
    search_fields = ('certificate_no', 'product__name', 'product__barcode')
    raw_id_fields = ('product',)


@admin.register(WatchDetail)
class WatchDetailAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'brand', 'model_name', 'reference_no',
        'serial_no', 'condition', 'box_papers',
    )
    list_filter = ('brand', 'condition', 'movement_type', 'case_material')
    search_fields = (
        'brand', 'model_name', 'reference_no', 'serial_no',
        'product__name', 'product__barcode',
    )
    raw_id_fields = ('product',)
