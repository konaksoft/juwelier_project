from decimal import Decimal
from time import *

from django.contrib.auth.decorators import login_required
from django.db import models
from django.db.models import Q
from django.db.models import Sum, Value, IntegerField,Max,BooleanField, OuterRef, Subquery
from django.db.models.functions import Coalesce, Cast
from django.db.models.query import Prefetch
from django.http import JsonResponse
# --- FAZ 4: StockSnapshot entegrasyonu ---
from apps.stock_management.models import StockSnapshot
from django.shortcuts import render, get_object_or_404

from apps.accounts.models import *
from apps.activity_logs.views import write_log
from apps.products.models import *


@login_required(login_url='login')
def categories_index(request):
    category_types = Categories.objects.filter(is_deleted=False, is_active=True)
    context = {
        'category_types': category_types,
        'title': 'Kategoriler'
    }
    return render(request, 'management/definitions/categories/index.html', context)


@login_required()
def category_add(request):
    context = {
        'title': 'Kategori Ekle',
    }
    if request.POST:
        record_id = request.POST.get('record_id')
        if record_id:
            record = get_object_or_404(Categories, id=record_id)
            record.name = request.POST.get('name')
            record.order = request.POST.get('order')
            record.created_by_id = request.user.id
            record.created_on = timezone.now()
        else:
            record = Categories()
            record.name = request.POST.get('name')
            record.order = request.POST.get('order')
            record.created_by_id = request.user.id
            record.created_on = timezone.now()
        try:
            record.save()
            write_log(request, 'E-Posta Profilleri', 'E-Posta Profili Eklendi. ID= ' + str(record.id).upper())
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})
    return render(request, 'management/definitions/categories/index.html', context)


@login_required(login_url='login')
def get_all(request):
    draw = int(request.GET['draw'])
    length = int(request.GET['length'])
    start = int(request.GET['start'])
    search_value = request.GET['search[value]']
    order_column = request.GET['columns[' + request.GET['order[0][column]'] + '][data]']
    order = request.GET['order[0][dir]']

    if order_column is None:
        order_column = "created_on"

    if order == 'desc':
        order_column = '-' + order_column

    queryset = Categories.objects.filter(is_deleted=False).values('name',
                                                                  'order', 'id', 'is_active').order_by('order')

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(name__icontains=search_value)

    count = queryset.count()

    if str(length) == '-1':
        queryset = queryset.order_by(order_column)
    else:
        queryset = queryset.order_by(order_column)[start:start + length]

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": list(queryset)
    })


@login_required(login_url='login')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Categories.objects.filter(id__in=ids)
            for record in records:
                record.is_deleted = True
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
def change_status(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Categories.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


# ============================================================================
# FAZ S1 (PIVOT 2026-04-23): ÇOKLU MADEN/ÜRÜN — JSON GENİŞLETMESİ
# ============================================================================
# Yardımcı: WatchDetail / DiamondDetail uzantı kayıtlarını OneToOne reverse
# erişimden okur ve JSON'a eklenecek alanları üretir. Hiçbir mevcut alan
# değişmez; sadece yeni alanlar eklenir → eski JS'ler etkilenmez.
#
# Kullanım: product_list comprehension içinde **_get_material_extras(p)
# şeklinde dict spread ile spread edilir.
# ============================================================================

def _get_material_extras(product):
    """
    Bir Products instance'ından satış ekranı için ek alanları döndürür.

    Dönen anahtarlar (her zaman var, GOLD/SILVER için None/0 olabilir):
        material_type        : 'GOLD' | 'SILVER' | 'WATCH' | 'DIAMOND'
        sale_currency        : 'USD' | 'EUR' | 'GBP' | 'CHF' | 'TRY' | None
        sale_price_foreign   : Decimal (döviz cinsinden satış fiyatı) | 0
        mount_karat          : '8K' | '14K' | '18K' | ... | None  (sadece DIAMOND)
        mount_gram           : Decimal | 0  (sadece DIAMOND)
        mount_metal          : 'GOLD_YELLOW' | ... | None  (sadece DIAMOND)
        watch_brand          : str | None  (sadece WATCH — etiket gösterimi için)
        watch_reference_no   : str | None  (sadece WATCH)

    Önemli: Try/except koruması altında — uzantı kaydı yoksa veya
    OneToOne reverse erişimi DoesNotExist atarsa, default'larla döner.
    Bu sayede mevcut altın/gümüş ürünleri etkilenmez.
    """
    mat_type = getattr(product, 'material_type', 'GOLD') or 'GOLD'
    extras = {
        'material_type':      mat_type,
        'sale_currency':      None,
        'sale_price_foreign': 0,
        'mount_karat':        None,
        'mount_gram':         0,
        'mount_metal':        None,
        'watch_brand':        None,
        'watch_reference_no': None,
    }

    if mat_type == 'WATCH':
        try:
            wd = product.watch_detail  # OneToOne reverse
            extras['sale_currency']      = wd.sale_currency or 'USD'
            extras['sale_price_foreign'] = float(wd.sale_price or 0)
            extras['watch_brand']        = wd.brand
            extras['watch_reference_no'] = wd.reference_no
        except Exception:
            # WatchDetail yoksa default'larla geç
            pass

    elif mat_type == 'DIAMOND':
        try:
            dd = product.diamond_detail  # OneToOne reverse
            extras['sale_currency']      = dd.sale_currency or 'USD'
            extras['sale_price_foreign'] = float(dd.sale_price or 0)
            extras['mount_karat']        = dd.mount_karat
            extras['mount_gram']         = float(dd.mount_gram or 0)
            extras['mount_metal']        = dd.mount_metal
        except Exception:
            pass

    return extras


def get_categories_with_products(request):
    store = request.user.store

    # ─── YOL 2 (SSOT) HOTFIX (2026-04-27): Perakende ürün gridi için FX bakiyeleri ───
    # get_product_details (Hızlı İşlem) ile birebir simetri sağlanıyor:
    # is_currency=True ürünlerin StockSnapshot.stock_pieces değeri ölü veridir,
    # bakiye Payment SSOT (FXBalanceReader) üzerinden okunmalıdır. Bu blok eksikti
    # ve Perakende sayfası 0.00 USD gösteriyordu (Hızlı İşlem doğru gösterirken).
    # N+1 önlemi: tüm FX kasaların döviz toplamları sayfa başına TEK seferde okunur.
    from apps.banking.services import FXBalanceReader, get_currency_code_from_product
    try:
        fx_balances_map = FXBalanceReader.get_all_balances(store)
    except Exception:
        fx_balances_map = {}

    use_pricing_subquery = StockSnapshot.objects.filter(
        product=OuterRef('pk'),
        store=store
    ).values('use_custom_pricing')[:1]
    # Ürünler için tek bir prefetch queryset'i: stok anotasyonları + sayısal order + stabil sıralama
    # FAZ 17: TRY baz para birimi olduğu için satış ürünü olamaz.
    # "TRY - Türk Lirası" ürünü Perakende ürün kataloğundan çıkarılır.
    # Dolar (USDTRY) ve Euro (EURTRY) döviz bozma mantığı için kalır.
    #
    # FAZ S1 (PIVOT): WATCH/DIAMOND için OneToOne uzantı kayıtlarını N+1 önlemek
    # amacıyla select_related ile birlikte çekiyoruz. select_related reverse
    # OneToOne ilişkilerinde Django'nun standart davranışıdır.
    products_qs = (
        Products.objects
        .filter(is_active=True, is_deleted=False)
        .exclude(name__icontains="TRY - Türk Lirası")
        .order_by('order')
        .filter(Q(is_protected=True) | Q(store=store))
        .select_related('watch_detail', 'diamond_detail')  # FAZ S1: N+1 koruması
        .annotate(
            total_stock_pieces=Coalesce(
                Sum('stock_snapshots__stock_pieces',
                    filter=models.Q(stock_snapshots__store=store)), 0
            ),
            total_stock_gram=Coalesce(
                Sum('stock_snapshots__stock_gram',
                    filter=models.Q(stock_snapshots__store=store)),
                Decimal('0.000')
            ),
            avg_cost_hs=Coalesce(
                Max('stock_snapshots__weighted_avg_cost_hs',
                    filter=models.Q(stock_snapshots__store=store)),
                Decimal('0.000')
            ),
            avg_cost_tl=Coalesce(
                Max('stock_snapshots__weighted_avg_cost_tl',
                    filter=models.Q(stock_snapshots__store=store)),
                Decimal('0.00')
            ),
            sale_cost_hs=Coalesce(
                Max('stock_snapshots__custom_sale_price_hs',
                    filter=models.Q(stock_snapshots__store=store)),
                Decimal('0.000')
            ),
            buy_cost_hs=Coalesce(
                Max('stock_snapshots__custom_buy_price_hs',
                    filter=models.Q(stock_snapshots__store=store)),
                Decimal('0.000')
            ),
            custom_fixed_labor=Coalesce(
                Max('stock_snapshots__custom_fixed_labor',
                    filter=models.Q(stock_snapshots__store=store)),
                Decimal('0.000')
            ),
            use_cost_pri=Coalesce(
                Subquery(use_pricing_subquery, output_field=BooleanField()),
                False
            )

        )
    )

    categories = (
        Categories.objects
        .filter(is_active=True, is_deleted=False)
        .order_by('order')
        .prefetch_related(
            Prefetch('products', queryset=products_qs, to_attr='pref_products')
        )
    )

    categories_data = []
    for category in categories:
        # ÖNEMLİ: Artık category.products.all() DEĞİL, prefetched listeyi kullan!
        products = getattr(category, 'pref_products', [])

        product_list = []
        for p in products:
            # ─── YOL 2 (SSOT) HOTFIX (2026-04-27): Döviz ürünleri için Payment SSOT ───
            # is_currency=True ürünlerde stock_pieces / stock_gram artık SSOT değildir
            # (StockSnapshot ölü veri). Frontend (retail_index.html ~satır 4130, 4937)
            # `is_currency`, `fx_currency`, `fx_balance` alanlarını okuyarak gerçek
            # FX kasa bakiyesini gösterir. get_product_details (products/views.py:944)
            # ile birebir simetri.
            _is_currency = bool(getattr(p, 'is_currency', False))
            _fx_currency = get_currency_code_from_product(p) if _is_currency else None
            _fx_balance = fx_balances_map.get(_fx_currency, Decimal('0')) if _fx_currency else Decimal('0')

            product_list.append({
                "id": str(p.id),
                "name": p.name,
                "jewelry_type": p.jewelry_type,
                "brand": p.brand,
                "barcode": p.barcode,
                "retail_lower_limit": p.retail_lower_limit,
                "retail_top_limit": p.retail_top_limit,
                "wholesale_lower_limit": p.wholesale_lower_limit,
                "wholesale_top_limit": p.wholesale_top_limit,
                "height": p.height,
                "profit": p.profit,
                "order": p.order,
                "gold_dry": p.gold_dry,
                "is_scrap": p.is_scrap,
                "is_gram_bullion": p.is_gram_bullion,
                "workmanship_type": p.workmanship_type,
                "product_mileage": p.product_mileage,
                "labor_mileage": p.labor_mileage,
                "description": p.description,
                "currency": p.currency,
                "fixed_labor_amount": p.fixed_labor_amount,
                "stock_pieces": p.total_stock_pieces,
                "stock_gram": float(p.total_stock_gram),
                "category_name": p.category.name if p.category_id else "",
                "buy_price_hs": p.buy_price_hs,
                "buy_price_tl": p.buy_price_tl,
                "sale_price_hs": p.sale_price_hs,
                "sale_price_tl": p.sale_price_tl,
                "gram": p.gram,
                "certificate": p.certificate,
                "gender": p.gender,
                "is_active": p.is_active,
                "is_deleted": p.is_deleted,
                "is_completed": p.is_completed,
                "popupId": "popup" + str(p.id),
                "weighted_buy_price_hs": float(p.avg_cost_hs),
                "custom_sale_price_hs": str(p.sale_cost_hs),
                "custom_buy_price_hs": str(p.buy_cost_hs),
                "use_custom_pricing": p.use_cost_pri,
                "custom_fixed_labor": str(p.custom_fixed_labor),
                # ─── FAZ S1 (PIVOT): Çoklu Maden Genişletmesi ───
                # WATCH/DIAMOND için kâr/maliyet hesabı TL bazlıdır (HS=0 olduğu için).
                "weighted_buy_price_tl": float(p.avg_cost_tl),
                # ─── YOL 2 (SSOT): Döviz SSOT alanları ───
                "is_currency": _is_currency,
                "fx_currency": _fx_currency or "",
                "fx_balance": float(_fx_balance),
                **_get_material_extras(p),
            })

        categories_data.append({
            "id": str(category.id),
            "name": category.name,
            "products": product_list
        })

    return JsonResponse({"data": categories_data}, safe=False)


def get_categories_with_products_wholesale(request):
    store = request.user.store
    categories = Categories.objects.filter(
        is_active=True,
        is_deleted=False,
        name__in=[ 'Ziynet', 'Döviz', 'Hurda','Bilezik']
    ).order_by('order').prefetch_related(
        Prefetch(
            'products',
            queryset=Products.objects.filter(
                is_active=True,
                is_deleted=False
            ).order_by('order')
            .select_related('watch_detail', 'diamond_detail')  # FAZ S1: N+1 koruması
            .annotate(
                total_stock_pieces=Coalesce(
                    Sum('stock_snapshots__stock_pieces',
                        filter=models.Q(stock_snapshots__store=store)), 0
                ),
                total_stock_gram=Coalesce(
                    Sum('stock_snapshots__stock_gram',
                        filter=models.Q(stock_snapshots__store=store)),
                    Decimal('0.000')
                ),
                avg_cost_hs=Coalesce(
                    Max('stock_snapshots__weighted_avg_cost_hs',
                        filter=models.Q(stock_snapshots__store=store)),
                    Decimal('0.000')
                ),
                avg_cost_tl=Coalesce(
                    Max('stock_snapshots__weighted_avg_cost_tl',
                        filter=models.Q(stock_snapshots__store=store)),
                    Decimal('0.00')
                ),
                sale_cost_hs=Coalesce(
                    Max('stock_snapshots__custom_sale_price_hs',
                        filter=models.Q(stock_snapshots__store=store)),
                    Decimal('0.000')
                ),
                buy_cost_hs=Coalesce(
                    Max('stock_snapshots__custom_buy_price_hs',
                        filter=models.Q(stock_snapshots__store=store)),
                    Decimal('0.000')
                ),
                custom_fixed_labor=Coalesce(
                    Max('stock_snapshots__custom_fixed_labor',
                        filter=models.Q(stock_snapshots__store=store)),
                    Decimal('0.000')
                )
            ).filter(
                # ── Veri Izolasyonu (HATA 8 Fix v3 - Final) ──────────────────────
                # Magazaya ait urunler (Hurda, Bilezik gibi store-specific) VEYA
                # Global meta veri urunleri (Ziynet, Doviz gibi is_protected=True).
                # is_protected=True olanlarin stogu 0 olsa bile listelenmesi gerekir
                # cunku toptan alis (giris) yapilabilmesi icin listede gorunmelidir.
                models.Q(store=store) | models.Q(is_protected=True)
            ).filter(
                # ── Hurda ek filtresi: stogu 0 olan hurdalari gizle ──
                (
                    models.Q(category__name='Hurda', is_scrap=True)
                    & ~models.Q(total_stock_gram=Decimal('0.000'))
                )
                | ~models.Q(category__name='Hurda')
            )
        )
    )

    categories_data = []
    for category in categories:
        product_list = [
            {
                "id": str(product.id),
                "name": product.name,
                "jewelry_type": product.jewelry_type,
                "brand": product.brand,
                "barcode": product.barcode,
                "retail_lower_limit": product.retail_lower_limit,
                "retail_top_limit": product.retail_top_limit,
                "wholesale_lower_limit": product.wholesale_lower_limit,
                "wholesale_top_limit": product.wholesale_top_limit,
                "height": product.height,
                "profit": product.profit,
                "order": product.order,
                "gold_dry": product.gold_dry,
                "is_scrap": product.is_scrap,
                "is_gram_bullion": product.is_gram_bullion,
                "workmanship_type": product.workmanship_type,
                "product_mileage": product.product_mileage,
                "labor_mileage": product.labor_mileage,
                "description": product.description,
                "currency": product.currency,
                "stock_pieces": product.total_stock_pieces,
                "stock_gram": float(product.total_stock_gram),
                "buy_price_hs": product.buy_price_hs,
                "buy_price_tl": product.buy_price_tl,
                "sale_price_hs": product.sale_price_hs,
                "sale_price_tl": product.sale_price_tl,
                "category_name": product.category.name,
                "gram": product.gram,
                "certificate": product.certificate,
                "gender": product.gender,
                "is_active": product.is_active,
                "is_deleted": product.is_deleted,
                "is_completed": product.is_completed,
                "popupId": "popup" + str(product.id),
                "weighted_buy_price_hs": float(product.avg_cost_hs),
                "custom_sale_price_hs": str(product.sale_cost_hs),
                "custom_buy_price_hs": str(product.buy_cost_hs),

                "custom_fixed_labor": str(product.custom_fixed_labor),
                # ─── FAZ S1 (PIVOT): Çoklu Maden Genişletmesi ───
                "weighted_buy_price_tl": float(product.avg_cost_tl),
                **_get_material_extras(product),
            }
            for product in category.products.all()
        ]
        categories_data.append({
            "id": str(category.id),
            "name": category.name,
            "products": product_list
        })

    return JsonResponse({"data": categories_data}, safe=False)
