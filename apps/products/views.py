import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Sum, Q, IntegerField, DecimalField, F, Case, When, Value, BooleanField, Max
from django.db.models.functions import Coalesce, Cast
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone  # Eksik import eklendi

from apps.definitions.currencies.models import Currencies
from apps.helpers.image_resize import process_image
from apps.products.models import Products, Categories
from apps.products.tasks import update_products_from_api

# --- FAZ 3: StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import (
    StockService,
    _invalidate_dashboard_assets_cache,
)
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.roles.decorators import role_required
from apps.stores.services import compute_store_has_tl
from apps.settings.models import StoreConfiguration
from apps.suppliers.models import Suppliers
from apps.stores.models import Stores
from django.db.models import OuterRef, Subquery  # Bunlar get_all içindeki alt sorgular için gerekli
from apps.chambers.models import ChamberProductPrice


# Yardımcı Fonksiyon: Stok değişim logu
# FAZ 3: InventoryMovement yerine StockLedger'a kaydedilecek (StockService.adjustment üzerinden)
def log_inventory_change(user, product, old_pieces, new_pieces, old_weight, new_weight, store):
    """
    FAZ 3 REFACTORED: Artık InventoryMovement tablosuna yazmak yerine
    StockService.adjustment() kullanılıyor. Bu fonksiyon geriye dönük
    uyumluluk için tutulmuştur ancak artık no-op'tur çünkü
    StockService.adjustment() kendi ledger kaydını oluşturur.
    """
    pass


# Yardımcı Fonksiyon: Mağaza Stok Özeti
# FAZ 3: Inventories yerine StockSnapshot'tan oku
def all_stock_gold(store, user):
    if not user.is_authenticated or not user.role_id:
        return None

    # FAZ 19.1: Döviz ürünleri listede görünür ama stok ÖZET hesabından hariç tutulur.
    # Para birimi ürünlerinin (is_currency=True) stok miktarı anlamsızdır —
    # nakit bakiye Kasa Yönetimi'nden (Payment tablosundan) okunur.
    store_snapshots = StockSnapshot.objects.filter(
        store=store,
    ).select_related('product')

    fourteen_karat_total = Decimal('0.0')
    toplam_has_altin_degeri = Decimal('0.0')

    for snap in store_snapshots:
        product = snap.product

        # Döviz ürünlerini has altın değeri ve 14 ayar toplamından hariç tut
        if getattr(product, 'is_currency', False):
            continue

        # 14 Ayar Hesabı
        if "14 Ayar" in product.name and not product.is_scrap:
            fourteen_karat_total += Decimal(snap.stock_gram)

        # Has Altın Değeri Hesabı - FAZ 3: WAC kullan
        wac_hs = snap.weighted_avg_cost_hs or Decimal('0')
        if wac_hs <= 0:
            # Fallback: Products tablosundan
            wac_hs = Decimal(str(product.buy_price_hs or 0))

        if product.is_gram_bullion:
            qty = Decimal(snap.stock_gram)
        else:
            qty = Decimal(snap.stock_pieces)

        toplam_has_altin_degeri += qty * wac_hs

    # FAZ 16: store_cash_amount artık Stok Yönetimi'nden değil, Kasa Yönetimi'nden okunmalı.
    # Geriye dönük uyum için Payment tablosundan nakit bakiye hesaplanır.
    from apps.banking.models import BankAccount
    from apps.process.models import Payment
    from django.db.models import Q, Sum
    from django.db.models.functions import Coalesce

    _cash_balance = Decimal('0.0')
    _cash_accounts = BankAccount.objects.filter(
        store=store, account_type='CASH', is_deleted=False, is_active=True,
    )
    if _cash_accounts.exists():
        _cash_agg = Payment.objects.filter(
            bank_account__in=_cash_accounts,
            is_cancelled=False,
        ).aggregate(
            _in=Coalesce(
                Sum('amount', filter=Q(is_output=False)),
                Decimal('0'),
            ),
            _out=Coalesce(
                Sum('amount', filter=Q(is_output=True)),
                Decimal('0'),
            ),
        )
        _cash_balance = _cash_agg['_in'] - _cash_agg['_out']

    return {
        'fourteen_karat_total': fourteen_karat_total,
        'toplam_has_altin_degeri': toplam_has_altin_degeri,
        'store_cash_amount': _cash_balance,
    }


@login_required(login_url='login')
@role_required('PRODUCTS_PRODUCT_INDEX')
def product_index(request):
    # NOT: 14 Ayar / Has Altın / Nakit özet pill'leri Dashboard'a taşındı
    # (/dashboard/assets-summary endpoint'i). Bu sayfada artık ağır
    # all_stock_gold() çağrısı yapılmıyor — sayfa hızı iyileşir.
    context = {
        'title': 'Ürünler',
        'categories': Categories.objects.filter(is_deleted=False, is_active=True),
    }
    return render(request, 'management/products/index.html', context)


@login_required(login_url='login')
@role_required('PRODUCTS_PRODUCT_ADD')
def product_add(request):
    if request.method == 'POST':
        record_id = request.POST.get('record_id')

        if record_id:
            record = get_object_or_404(Products, id=record_id)
            # UPDATE cross-store + global product guard.
            # Bu view "Sarrafiye Ekle" özel ürün modal'ı için; mağaza kendi
            # ürününü düzenlemeli. record.store=None (global standart ürün)
            # veya başka mağaza kaydı buradan değiştirilemez. Aksi halde isim/
            # fiyat alanları tüm mağazalarda etkilenir.
            _user_store = request.user.store
            if not _user_store:
                return JsonResponse({
                    'error': True,
                    'error_msg': 'Mağaza atanmamış.'
                })
            if record.is_protected:
                return JsonResponse({
                    'error': True,
                    'error_msg': f"'{record.name}' korumalı bir sistem ürünüdür; düzenlenemez."
                })
            if record.store_id is None:
                return JsonResponse({
                    'error': True,
                    'error_msg': (
                        f"'{record.name}' tüm mağazalarda paylaşılan standart bir "
                        f"üründür; düzenlenemez."
                    )
                })
            if record.store_id != _user_store.id:
                return JsonResponse({
                    'error': True,
                    'error_msg': f"'{record.name}' başka bir mağazaya ait, düzenlenemez."
                })
        else:
            record = Products()

        record.name = request.POST.get('name')
        record.category_id = request.POST.get('category_id')
        record.jewelry_type = request.POST.get('jewelry_type')
        record.brand = request.POST.get('brand')
        record.retail_lower_limit = request.POST.get('retail_lower_limit')
        record.retail_top_limit = request.POST.get('retail_top_limit')
        record.wholesale_lower_limit = request.POST.get('wholesale_lower_limit')
        record.wholesale_top_limit = request.POST.get('wholesale_top_limit')
        record.height = request.POST.get('height')
        record.order = request.POST.get('order')
        record.gold_rate = request.POST.get('gold_rate')
        record.is_scrap = bool(request.POST.get('is_scrap'))
        record.is_gram_bullion = bool(request.POST.get('is_gram_bullion'))
        record.workmanship_type = bool(request.POST.get('workmanship_type'))
        record.product_mileage = request.POST.get('product_mileage')
        record.labor_mileage = request.POST.get('labor_mileage')
        record.description = request.POST.get('description')
        record.currency = request.POST.get('currency')
        record.gram = request.POST.get('gram')
        record.certificate = request.POST.get('certificate')
        record.gender = request.POST.get('gender')
        record.sale_price_hs = request.POST.get('sale_price_hs')
        record.sale_price_eur = request.POST.get('sale_price_eur')
        record.buy_price_hs = request.POST.get('buy_price_hs')
        record.buy_price_eur = request.POST.get('buy_price_eur')
        record.profit = request.POST.get('profit')
        record.barcode = request.POST.get('barcode')

        fixed_labor = request.POST.get('fixed_labor_amount')
        if fixed_labor:
            record.fixed_labor_amount = fixed_labor.replace(',', '.')
        else:
            record.fixed_labor_amount = 0

        # Cross-store data leak guard:
        # POST'tan gelen `store_id` güvenli değil — kullanıcı manipüle edebilir
        # ya da boş gönderebilir, sonuç: store=NULL → ürün TÜM mağazalardan görünür
        # (get_all() içindeki `Q(store__isnull=True)` global sistem ürünleri için).
        # Yeni ürünlerde store her zaman authenticated user'ın mağazasıdır.
        # UPDATE durumunda mevcut store korunur — `is_protected=True, store=None`
        # global sistem ürünleri (Has Altın, USDTRY) yanlışlıkla mağazaya bağlanmasın.
        if not record_id:
            record.store = request.user.store
        record.created_by_id = request.user.id

        if not record_id:
            record.created_on = timezone.now()

        image = request.FILES.get('image')
        if image:
            filename, processed = process_image(image)
            record.image.save(filename, processed, save=False)

        # ─── UAT V3 P3 (2026-04-29): Adet bazlı ürünlerde gram validasyonu ────
        # is_gram_bullion=False (Adet Bazlı, örn. Reşat) ürünlerde `gram` (birim
        # ağırlık) > 0 olmak zorundadır. Aksi halde stok hesabı yanlış işler:
        # WAC için quantity_gram=0, snapshot.stock_gram=0 → satışta SALE
        # ledger'ı doğru maliyet üretemez. Frontend modal validation yapsa da
        # backend'de de zorla — bypass koruması.
        if not record.is_gram_bullion:
            try:
                _g = Decimal(str(record.gram or 0))
            except (InvalidOperation, TypeError, ValueError):
                _g = Decimal('0')
            if _g <= 0:
                return JsonResponse({
                    'error': True,
                    'error_msg': "Adet bazlı ürünlerde 'Birim Ağırlık (gram)' 0'dan büyük olmalıdır."
                })

        try:
            record.save()

            stock_pieces = int(request.POST.get('stock_pieces', 0) or 0)
            try:
                stock_weight = Decimal(str(request.POST.get('stock_weight', 0) or 0))
            except (InvalidOperation, TypeError, ValueError):
                stock_weight = Decimal('0')

            # ─── UAT V3 P3 (2026-04-29): is_gram_bullion=False için otomatik gram ─
            # Adet bazlı (Reşat, Cumhuriyet vb.) ürünlerde modal sadece adet
            # alır; toplam gram = adet × birim_ağırlık × (milyem/1000) formülüyle
            # otomatik türetilir. Bu has-gram eşdeğeri değil "fiziksel gram"dır;
            # WAC formülü gram bazlı çalışır, has dönüşümü unit_cost_hs üzerinden
            # gerçekleşir. SALE çıkışlarında StockLedger aynı formülle quantity
            # üretir (FAZ S1: çoklu maden + bullion senaryolarıyla simetrik).
            if not record.is_gram_bullion and stock_pieces > 0:
                try:
                    gram_per_piece = Decimal(str(record.gram or 0))
                    mileage = Decimal(str(record.product_mileage or 1000))
                except (InvalidOperation, TypeError, ValueError):
                    gram_per_piece = Decimal('0')
                    mileage = Decimal('1000')
                if gram_per_piece > 0:
                    # Fiziksel gram (milyem dahil değil) — snapshot.stock_gram'a yazılır.
                    # Has-gram dönüşümü unit_cost_hs ile WAC formülünde uygulanır.
                    stock_weight = (Decimal(stock_pieces) * gram_per_piece).quantize(Decimal('0.0001'))

            # ─── UAT V3 P3: WAC'lı INITIAL kaydı (yeni ürün için) ────────────
            # Yeni ürün eklerken (record_id boşsa) açılış stoğunu INITIAL reason
            # ile StockService.record_entry üzerinden işliyoruz; bu WAC'ı
            # kullanıcının girdiği buy_price_hs / buy_price_eur ile doğru başlatır.
            # Mevcut ürün güncellemelerinde adjustment davranışı korunuyor (sayım
            # düzeltmesi semantiği — WAC tekrar hesaplanmaz, mevcut maliyet kalır).
            #
            # Append-only / SSOT etkisi:
            #   - INITIAL reason direction=IN → ledger append-only kuralına uygun.
            #   - record_entry WAC formülü `quantity_gram > 0` şartı arar; adet
            #     bazlı ürünlerde yukarıda hesapladığımız stock_weight bu şartı
            #     karşılar. Sadece quantity_pieces > 0 (gram=0) durumunda WAC
            #     hesaplanmaz; o senaryo için fallback olarak adjustment kullan.
            try:
                buy_hs_val = Decimal(str(record.buy_price_hs or 0))
            except (InvalidOperation, TypeError, ValueError):
                buy_hs_val = Decimal('0')
            try:
                buy_tl_val = Decimal(str(record.buy_price_eur or 0))
            except (InvalidOperation, TypeError, ValueError):
                buy_tl_val = Decimal('0')

            has_any_quantity = (stock_weight > 0) or (stock_pieces > 0)

            if not record_id and has_any_quantity and stock_weight > 0:
                # Yeni ürün + miktar var + gram > 0 → INITIAL ledger + WAC init
                StockService.record_entry(
                    product=record,
                    store=request.user.store,
                    quantity_gram=stock_weight,
                    quantity_pieces=stock_pieces,
                    reason=StockLedger.Reason.INITIAL,
                    ref_type='initial',
                    ref_id=f"product_add_{record.id}",
                    unit_cost_hs=buy_hs_val,
                    unit_cost_eur=buy_tl_val,
                    hs_rate_eur=Decimal('0'),
                    user=request.user,
                    notes="Açılış stoğu (yeni özel ürün)",
                )
            else:
                # Mevcut ürün güncellemesi VEYA gram=0 (sadece adet) edge case →
                # adjustment fallback'i (sayım semantiği). Adet bazlı ürünlerde
                # birim ağırlık 0 olamaz (yukarıda blokladık) ama defensive.
                StockService.adjustment(
                    product=record,
                    store=request.user.store,
                    actual_gram=stock_weight,
                    actual_pieces=stock_pieces,
                    ref_id=f"product_add_{record.id}",
                    user=request.user,
                    notes="Ürün ekleme/güncelleme stok ayarı",
                    is_initial=(not record_id),
                )

            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})

    return product_index(request)


@login_required(login_url='login')
@role_required('PRODUCTS_GET_ALL')
def get_all(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '')
    category_name = request.GET.get('category', None)
    ordering = ['display_order', 'name']

    user_store = request.user.store

    # Mağazanın canlı kurlarını al
    store_buy_tl, store_sale_tl = compute_store_has_tl(user_store)
    store_buy_tl = store_buy_tl if store_buy_tl > 0 else Decimal('1')
    store_sale_tl = store_sale_tl if store_sale_tl > 0 else Decimal('1')

    # Mağaza config'i (geriye uyumluluk için tutuluyor; chamber/manual-has kaldırıldı)
    config = StoreConfiguration.objects.filter(store=user_store).first()
    is_manual_active = False
    active_chamber_id = None

    # FAZ 19.1: Döviz ürünleri (USDTRY, EURTRY vb.) listede GÖRÜNMELİDİR.
    # Fiyatları güncellenmeli ve patronlar kurları görebilmelidir.
    # Stok düzenleme koruması update_inventory_ajax/bulk_ajax içinde yapılır (UI'da readonly).
    queryset = Products.objects.filter(is_deleted=False, is_active=True)

    if user_store:
        queryset = queryset.filter(Q(store=user_store) | Q(store__isnull=True))

    # --- SUBQUERY İLE DERNEK FİYATLARINI ÇEKME ---
    # Eğer mağazanın bağlı olduğu bir referans dernek varsa fiyatları hazırlıyoruz
    chamber_buy_sq = ChamberProductPrice.objects.filter(chamber_id=active_chamber_id, product=OuterRef('pk')).values(
        'buy_price_hs')[:1]
    chamber_sale_sq = ChamberProductPrice.objects.filter(chamber_id=active_chamber_id, product=OuterRef('pk')).values(
        'sale_price_hs')[:1]
    chamber_labor_sq = ChamberProductPrice.objects.filter(chamber_id=active_chamber_id, product=OuterRef('pk')).values(
        'fixed_labor_amount')[:1]

    # FAZ 3: StockSnapshot üzerinden stok ve fiyat annotation'ları
    queryset = queryset.annotate(
        total_stock_pieces=Coalesce(
            Sum('stock_snapshots__stock_pieces', filter=Q(stock_snapshots__store=user_store)), 0,
            output_field=IntegerField()),
        total_stock_weight=Coalesce(
            Sum('stock_snapshots__stock_gram', filter=Q(stock_snapshots__store=user_store)), Decimal(0),
            output_field=DecimalField(max_digits=10, decimal_places=3)),
        incoming_stock_pieces=Coalesce(
            Sum('stock_snapshots__incoming_stock_pieces', filter=Q(stock_snapshots__store=user_store)), 0,
            output_field=IntegerField()),
        incoming_stock_weight=Coalesce(
            Sum('stock_snapshots__incoming_stock_gram', filter=Q(stock_snapshots__store=user_store)),
            Decimal(0), output_field=DecimalField(max_digits=10, decimal_places=3)),

        # Mağaza Özel Fiyatları (StockSnapshot üzerinden)
        inv_use_custom_int=Coalesce(Max(Cast('stock_snapshots__use_custom_pricing', output_field=IntegerField()),
                                        filter=Q(stock_snapshots__store=user_store)), 0,
                                    output_field=IntegerField()),
        inv_buy_hs=Max('stock_snapshots__custom_buy_price_hs', filter=Q(stock_snapshots__store=user_store)),
        inv_sale_hs=Max('stock_snapshots__custom_sale_price_hs', filter=Q(stock_snapshots__store=user_store)),
        inv_labor=Max('stock_snapshots__custom_fixed_labor', filter=Q(stock_snapshots__store=user_store)),

        # Dernek Özel Fiyatları (Subquery üzerinden)
        chamber_buy_hs=Subquery(chamber_buy_sq),
        chamber_sale_hs=Subquery(chamber_sale_sq),
        chamber_labor=Subquery(chamber_labor_sq),
    )

    if category_name == "Eksik":
        queryset = queryset.filter(Q(incoming_stock_pieces__gt=0) | Q(incoming_stock_weight__gt=0))
    elif category_name and category_name != "Bekleyen":
        try:
            category = Categories.objects.get(name=category_name)
            queryset = queryset.filter(category=category.id)
        except Categories.DoesNotExist:
            return JsonResponse({"draw": draw, "recordsFiltered": 0, "recordsTotal": 0, "data": []})

    total = queryset.count()
    if search_value:
        queryset = queryset.filter(name__icontains=search_value)
    count = queryset.count()

    if str(length) == '-1':
        queryset = queryset.order_by(*ordering)
    else:
        queryset = queryset.order_by(*ordering)[start:start + length]

    raw_data = list(queryset.values(
        'id', 'name', 'image', 'is_gram_bullion', 'is_currency', 'is_protected', 'category__name',
        'sale_price_eur', 'buy_price_eur', 'buy_price_hs', 'sale_price_hs', 'fixed_labor_amount',
        'total_stock_pieces', 'total_stock_weight', 'incoming_stock_pieces', 'incoming_stock_weight',
        'inv_use_custom_int', 'inv_buy_hs', 'inv_sale_hs', 'inv_labor',
        'chamber_buy_hs', 'chamber_sale_hs', 'chamber_labor',
        'display_order',  # T2: drag-drop sıralama için
    ))

    data = []

    for row in raw_data:
        use_custom_store = bool(row['inv_use_custom_int'])

        # --- 3 KATMANLI FİYAT HİYERARŞİSİ (Global -> Dernek -> Mağaza) ---

        # Katman 3: Global (Sistemdeki varsayılan ürün fiyatları)
        effective_buy_hs = row['buy_price_hs'] if row['buy_price_hs'] is not None else Decimal(0)
        effective_sale_hs = row['sale_price_hs'] if row['sale_price_hs'] is not None else Decimal(0)
        effective_labor = row['fixed_labor_amount'] if row['fixed_labor_amount'] is not None else Decimal(0)

        # Katman 2: Dernek Fiyatı (Eğer mağaza dernek referansı seçmişse ve derneğin fiyatı varsa)
        if active_chamber_id:
            if row['chamber_buy_hs'] is not None:
                effective_buy_hs = row['chamber_buy_hs']
            if row['chamber_sale_hs'] is not None:
                effective_sale_hs = row['chamber_sale_hs']
            if row['chamber_labor'] is not None:
                effective_labor = row['chamber_labor']

        # Katman 1a: Mağaza Manuel Has Fiyatı (Manuel Has modu açıksa VE ürün için özel fiyat girildiyse)
        # Not: Sadece Has fiyatları (buy_price_hs / sale_price_hs) bu gate'in arkasındadır.
        if is_manual_active and use_custom_store:
            if row['inv_buy_hs'] is not None and row['inv_buy_hs'] > 0:
                effective_buy_hs = row['inv_buy_hs']
            if row['inv_sale_hs'] is not None and row['inv_sale_hs'] > 0:
                effective_sale_hs = row['inv_sale_hs']

        # Katman 1b: Mağaza Özel İşçilik (Manuel Has modundan BAĞIMSIZ okunur)
        # İşçilik override'ı use_average_labor ayarıyla kontrol edilir,
        # use_manual_has_calculation ile değil.
        if use_custom_store and row['inv_labor'] is not None and row['inv_labor'] > 0:
            effective_labor = row['inv_labor']

        # -----------------------------------------------------------------

        # Döviz ürünleri (is_currency=True) için buy_price_eur API kurudur.
        # has×kur çarpımı yapılmaz; çünkü buy_price_hs = USDTRY/ALTINTRY şeklinde
        # hesaplandığından altın fiyatı değiştikçe yanlış sonuç üretir.
        if row.get('is_currency'):
            final_buy_tl = Decimal(str(row.get('buy_price_eur') or 0))
            final_sale_tl = Decimal(str(row.get('sale_price_eur') or 0))
        else:
            final_buy_tl = (Decimal(effective_buy_hs) * store_buy_tl)
            final_sale_tl = (Decimal(effective_sale_hs) * store_sale_tl) + Decimal(effective_labor)

        if row['is_gram_bullion']:
            row['total_stock_pieces'] = row['total_stock_weight']
            row['incoming_stock_pieces'] = row['incoming_stock_weight']

        data.append({
            'id': row['id'],
            'name': row['name'],
            'image': row['image'],
            'total_stock_pieces': row['total_stock_pieces'],
            'total_stock_weight': row['total_stock_weight'],
            'incoming_stock_pieces': row['incoming_stock_pieces'],
            'incoming_stock_weight': row['incoming_stock_weight'],
            'buy_price_hs': str(effective_buy_hs),
            'sale_price_hs': str(effective_sale_hs),
            'fixed_labor_amount': str(effective_labor),
            'buy_price_eur': float(round(final_buy_tl, 2)),
            'sale_price_eur': float(round(final_sale_tl, 2)),
            'is_gram_bullion': row['is_gram_bullion'],
            'is_currency': row.get('is_currency', False),  # FAZ 19.1: UI'da stok input koruması için
            'is_protected': bool(row.get('is_protected')),  # UAT V3 P2: silme buton koşulu için
            'display_order': int(row.get('display_order') or 0),  # T2: drag-drop sıralama
        })

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": data
    })


@login_required(login_url='login')
@role_required('PRODUCTS_DELETE')
def delete(request):
    response_data = {'result': False, 'error': True}
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        if not ids:
            response_data['error_msg'] = "Silinecek kayıt seçilmedi."
            return JsonResponse(response_data)

        # Cross-store + global product guard (change_status ile aynı gerekçe).
        # delete_single zaten cross-store guard'a sahip; bulk delete'e simetri
        # kazandırılıyor — başka mağazanın ürününü ya da global standart ürünü
        # toplu seçimle silmek mümkün olmasın.
        user_store = request.user.store
        if not user_store:
            response_data['error_msg'] = 'Mağaza atanmamış.'
            return JsonResponse(response_data)

        try:
            records = Products.objects.filter(id__in=ids)
            for record in records:
                if record.is_protected:
                    response_data['error_msg'] = f"{record.name} korunduğu için silinemez."
                    return JsonResponse(response_data)
                if record.store_id is None:
                    response_data['error_msg'] = (
                        f"'{record.name}' tüm mağazalarda paylaşılan standart bir "
                        f"üründür; silinemez."
                    )
                    return JsonResponse(response_data)
                if record.store_id != user_store.id:
                    response_data['error_msg'] = (
                        f"'{record.name}' başka bir mağazaya ait, silinemez."
                    )
                    return JsonResponse(response_data)

            records.update(is_deleted=True)

            # FAZ 34 (2026-05-01): Toplu soft-delete sonrasi Dashboard cache'ini
            # invalide et. Etkilenen tum store'lar icin tek tek silinir; cogu
            # senaryoda kullanici tek magazaya bagli oldugu icin set kucuktur.
            try:
                _store_ids = set(
                    records.exclude(store__isnull=True)
                           .values_list('store_id', flat=True)
                )
                if request.user.store_id:
                    _store_ids.add(request.user.store_id)
                for _sid in _store_ids:
                    _stub = type('S', (), {'id': _sid})()
                    transaction.on_commit(
                        lambda s=_stub: _invalidate_dashboard_assets_cache(s)
                    )
            except Exception:
                pass  # cache invalidation hatasi islemi patlatmamali

            response_data.update({'result': True, 'error': False})
            return JsonResponse(response_data)
        except Exception as e:
            response_data['error_msg'] = str(e)
            return JsonResponse(response_data)
    response_data['error_msg'] = 'Geçersiz istek.'
    return JsonResponse(response_data)


# ============================================================================
# UAT V3 P2 (2026-04-29): GÜVENLİ SİLME — deletion_check + delete_single
# ============================================================================
# Sarrafiye modal'ından eklenen özel ürünler için Ziynet DataTable satırında
# "Sil" ikonu çıkar. Frontend önce GET /products/<uuid>/deletion-check ile
# mevcut durumu öğrenir; sonra POST /products/<uuid>/delete-single ile silme
# komutunu yollar. Append-only / SSOT kurallarına sadık kalmak için:
#   - is_protected=True ürünler ASLA silinemez (Has Altın, USDTRY vb.).
#   - Cross-store guard: kullanıcı kendi mağazasının ürününü silebilir.
#   - StockLedger kaydı VEYA stok > 0 → soft-delete (is_deleted=True).
#     Append-only kuralı korunur, ledger geçmişi silinmez.
#   - Hiç ledger yok ve stok = 0 → güvenli soft-delete (orphan).
#     Fiziksel DELETE yapılmaz; soft-delete WAC/raporlar/ledger zincirini
#     bozmaz, çağrı yapan kod istisna almaz.
# ============================================================================

@login_required(login_url='login')
@role_required('PRODUCTS_DELETE')
def deletion_check(request, product_id):
    """
    Bir ürünün silinebilirlik durumunu döner. Frontend bu cevaba göre
    kullanıcıya doğru onay metnini gösterir.
    """
    user_store = request.user.store
    if not user_store:
        return JsonResponse({'error': True, 'message': 'Mağaza atanmamış.'}, status=403)

    try:
        product = Products.objects.get(id=product_id, is_deleted=False)
    except Products.DoesNotExist:
        return JsonResponse({'error': True, 'message': 'Ürün bulunamadı.'}, status=404)

    # Cross-store + protected guard
    if product.is_protected:
        return JsonResponse({
            'error': False,
            'can_delete': False,
            'reason': 'protected',
            'message': f"'{product.name}' korumalı bir sistem ürünüdür, silinemez."
        })
    if product.store_id and product.store_id != user_store.id:
        return JsonResponse({
            'error': False,
            'can_delete': False,
            'reason': 'cross_store',
            'message': "Bu ürün başka bir mağazaya ait, silinemez."
        })

    # Ledger varlığı + stok kontrolü
    has_ledger = StockLedger.objects.filter(product_id=product_id).exists()

    snapshot = StockSnapshot.objects.filter(
        product_id=product_id, store=user_store
    ).first()
    has_stock = bool(
        snapshot and (
            (snapshot.stock_pieces or 0) > 0
            or Decimal(str(snapshot.stock_gram or 0)) > Decimal('0')
        )
    )

    return JsonResponse({
        'error': False,
        'can_delete': True,
        'product_name': product.name,
        'has_ledger': has_ledger,
        'has_stock': has_stock,
        # has_ledger veya has_stock varsa soft-delete; yoksa orphan (yine soft-delete)
        # Fiziksel DELETE hiçbir senaryoda yapılmaz (append-only ledger entegrasyonu).
        'mode': 'soft_with_history' if (has_ledger or has_stock) else 'soft_orphan',
    })


@login_required(login_url='login')
@role_required('PRODUCTS_DELETE')
def delete_single(request, product_id):
    """
    Tek ürün için soft-delete (is_deleted=True). is_protected=True veya cross-store
    ürünleri reddeder. StockLedger ve StockSnapshot kayıtları aynen kalır
    (append-only kuralı). Soft-deleted ürün listelerden çıkar ama geçmiş
    raporlarda ürün adıyla görünmeye devam eder.
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'message': 'Sadece POST.'}, status=405)

    user_store = request.user.store
    if not user_store:
        return JsonResponse({'error': True, 'message': 'Mağaza atanmamış.'}, status=403)

    try:
        product = Products.objects.get(id=product_id, is_deleted=False)
    except Products.DoesNotExist:
        return JsonResponse({'error': True, 'message': 'Ürün bulunamadı.'}, status=404)

    if product.is_protected:
        return JsonResponse({
            'error': True,
            'message': f"'{product.name}' korumalı bir sistem ürünüdür, silinemez."
        }, status=400)
    if product.store_id and product.store_id != user_store.id:
        return JsonResponse({
            'error': True,
            'message': 'Bu ürün başka bir mağazaya ait, silinemez.'
        }, status=403)

    product.is_deleted = True
    product.save(update_fields=['is_deleted'])

    # FAZ 34 (2026-05-01): Soft-delete sonrasi Dashboard cache'ini invalide et.
    # Aksi halde silinmis urun en fazla 5 dakika boyunca TAB 1 (assets-v2)
    # tablosunda gorunmeye devam eder; WAC anomalileri ve hayalet stok
    # kayitlari raporda kalir. _invalidate_dashboard_assets_cache hem
    # dashboard_assets_summary hem de dashboard_assets_v2 key'lerini siler.
    transaction.on_commit(lambda: _invalidate_dashboard_assets_cache(user_store))

    return JsonResponse({
        'error': False,
        'result': True,
        'message': f"'{product.name}' silindi (soft-delete).",
    })


@login_required(login_url='login')
@role_required('PRODUCTS_CHANGE_STATUS')
def change_status(request):
    response_data = {'result': False, 'error': True}
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        if not ids:
            response_data['error_msg'] = "Kayıt seçilmedi."
            return JsonResponse(response_data)

        # Cross-store + global product guard.
        # Geçmişte bu view'de yalnızca `is_protected` kontrolü vardı; standart
        # sarrafiye/döviz ürünleri (store=None, is_protected=False — eski DB veya
        # fixture'dan kalan kayıtlar) tüm mağazalarda görünüyordu ve herhangi bir
        # mağazadan "Pasif Yap" tıklayan kullanıcı "Eski Tam" / "22 Ayar Gram"
        # gibi paylaşılan ürünleri TÜM mağazalardan kaldırabiliyordu.
        user_store = request.user.store
        if not user_store:
            response_data['error_msg'] = 'Mağaza atanmamış.'
            return JsonResponse(response_data)

        try:
            records = Products.objects.filter(id__in=ids)
            for record in records:
                if record.is_protected:
                    response_data['error_msg'] = f"{record.name} korunduğu için değiştirilemez."
                    return JsonResponse(response_data)
                if record.store_id is None:
                    response_data['error_msg'] = (
                        f"'{record.name}' tüm mağazalarda paylaşılan standart bir "
                        f"üründür; pasif/aktif durumu değiştirilemez."
                    )
                    return JsonResponse(response_data)
                if record.store_id != user_store.id:
                    response_data['error_msg'] = (
                        f"'{record.name}' başka bir mağazaya ait, değiştirilemez."
                    )
                    return JsonResponse(response_data)
                record.is_active = not record.is_active
                record.save()

            response_data.update({'result': True, 'error': False})
            return JsonResponse(response_data)
        except Exception as e:
            response_data['error_msg'] = str(e)
            return JsonResponse(response_data)
    response_data['error_msg'] = 'Geçersiz istek.'
    return JsonResponse(response_data)


def update_inventory_ajax(request):
    """
    FAZ 3 REFACTORED: Inventories yerine StockSnapshot + StockService.adjustment() kullanır.
    Mağaza özel fiyatları StockSnapshot'a yazılır.
    """
    product_id = request.POST.get('product_id')
    updated_data_json = request.POST.get('updated_data', '{}')

    try:
        payload = json.loads(updated_data_json or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Geçersiz JSON.'}, status=400)

    product = get_object_or_404(Products, id=product_id, is_deleted=False)

    # FAZ 19: Para birimi ürünleri (TRY, USDTRY vb.) stok düzenlemesine kapalıdır.
    # Bu ürünler Kasa Yönetimi'nden yönetilir, Stok Yönetimi'nden değil.
    if getattr(product, 'is_currency', False):
        return JsonResponse({
            'status': 'error',
            'message': 'Para birimi ürünleri stok düzenlemesine kapalıdır. Kasa Yönetimi\'ni kullanın.'
        }, status=400)

    # FAZ 3: StockSnapshot ile çalış
    snap, created = StockSnapshot.objects.get_or_create(
        product=product,
        store=request.user.store,
        defaults={
            'stock_gram': Decimal('0.0000'),
            'stock_pieces': 0,
            'weighted_avg_cost_hs': Decimal('0.0000'),
            'weighted_avg_cost_eur': Decimal('0.00'),
        }
    )

    pricing_changed = False

    if 'fixed_labor_amount' in payload:
        try:
            val = str(payload['fixed_labor_amount']).replace(',', '.')
            if hasattr(snap, 'custom_fixed_labor'):
                snap.custom_fixed_labor = Decimal(val) if val else Decimal(0)
                pricing_changed = True
        except:
            pass

    if 'buy_price_hs' in payload:
        try:
            val = str(payload['buy_price_hs']).replace(',', '.')
            if hasattr(snap, 'custom_buy_price_hs'):
                snap.custom_buy_price_hs = Decimal(val) if val else Decimal(0)
                pricing_changed = True
        except:
            pass

    if 'sale_price_hs' in payload:
        try:
            val = str(payload['sale_price_hs']).replace(',', '.')
            if hasattr(snap, 'custom_sale_price_hs'):
                snap.custom_sale_price_hs = Decimal(val) if val else Decimal(0)
                pricing_changed = True
        except:
            pass

    if pricing_changed and hasattr(snap, 'use_custom_pricing'):
        snap.use_custom_pricing = True

    # Stok değişikliği kontrolü
    field_map = {
        'total_stock_pieces': 'stock_pieces',
        'total_stock_weight': 'stock_gram',
        'incoming_stock_pieces': 'incoming_stock_pieces',
        'incoming_stock_weight': 'incoming_stock_gram',
    }

    has_stock_change = False
    new_pieces = snap.stock_pieces
    new_gram = snap.stock_gram
    errors = {}

    for key, val in payload.items():
        if key in ['fixed_labor_amount', 'buy_price_hs', 'sale_price_hs']:
            continue

        target_field = field_map.get(key)
        if not target_field:
            continue

        if product.is_gram_bullion:
            if target_field == 'stock_pieces':
                target_field = 'stock_gram'
            elif target_field == 'incoming_stock_pieces':
                target_field = 'incoming_stock_gram'

        try:
            if target_field.endswith('gram'):
                val_str = str(val).replace(',', '.')
                parsed_val = Decimal(val_str)
                if target_field == 'stock_gram':
                    new_gram = parsed_val
                else:
                    setattr(snap, target_field, parsed_val)
                has_stock_change = True
            else:
                parsed_val = int(val)
                if target_field == 'stock_pieces':
                    new_pieces = parsed_val
                else:
                    setattr(snap, target_field, parsed_val)
                has_stock_change = True
        except:
            errors[key] = 'Format hatası'

    if errors:
        return JsonResponse({'status': 'error', 'errors': errors}, status=400)

    # Önce fiyat değişikliklerini kaydet
    if pricing_changed:
        snap.save()

    # Stok değişikliği varsa StockService.adjustment() kullan
    if has_stock_change:
        StockService.adjustment(
            product=product,
            store=request.user.store,
            actual_gram=new_gram,
            actual_pieces=new_pieces,
            ref_id=f"manual_adj_{product_id}",
            user=request.user,
            notes="Manuel Stok Düzenleme",
        )

    return JsonResponse({'status': 'success'})


def update_inventory_bulk_ajax(request):
    """
    FAZ 3 REFACTORED: Toplu stok düzenleme. StockSnapshot + StockService.adjustment() kullanır.
    """
    try:
        items = json.loads(request.POST.get('items', '[]'))
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Geçersiz JSON.'}, status=400)

    field_map = {
        'total_stock_pieces': 'stock_pieces',
        'total_stock_weight': 'stock_gram',
        'incoming_stock_pieces': 'incoming_stock_pieces',
        'incoming_stock_weight': 'incoming_stock_gram',
    }

    updated = 0
    failed = []

    with transaction.atomic():
        for item in items:
            pid = item.get('product_id')
            data = item.get('updated_data', {})
            try:
                product = Products.objects.get(id=pid, is_deleted=False)

                # FAZ 19: Para birimi ürünlerini toplu düzenlemeden hariç tut
                if getattr(product, 'is_currency', False):
                    failed.append({'product_id': pid, 'reason': 'Para birimi ürünü — stok düzenlemesi yapılamaz.'})
                    continue

                snap, _ = StockSnapshot.objects.get_or_create(
                    product=product,
                    store=request.user.store,
                    defaults={
                        'stock_gram': Decimal('0.0000'),
                        'stock_pieces': 0,
                        'weighted_avg_cost_hs': Decimal('0.0000'),
                        'weighted_avg_cost_eur': Decimal('0.00'),
                    }
                )

                pricing_changed = False
                if 'fixed_labor_amount' in data:
                    if hasattr(snap, 'custom_fixed_labor'):
                        snap.custom_fixed_labor = Decimal(str(data['fixed_labor_amount']).replace(',', '.'))
                        pricing_changed = True
                if 'buy_price_hs' in data:
                    if hasattr(snap, 'custom_buy_price_hs'):
                        snap.custom_buy_price_hs = Decimal(str(data['buy_price_hs']).replace(',', '.'))
                        pricing_changed = True
                if 'sale_price_hs' in data:
                    if hasattr(snap, 'custom_sale_price_hs'):
                        snap.custom_sale_price_hs = Decimal(str(data['sale_price_hs']).replace(',', '.'))
                        pricing_changed = True

                if pricing_changed and hasattr(snap, 'use_custom_pricing'):
                    snap.use_custom_pricing = True

                has_stock_change = False
                new_pieces = snap.stock_pieces
                new_gram = snap.stock_gram

                for key, val in data.items():
                    if key in ['fixed_labor_amount', 'buy_price_hs', 'sale_price_hs']:
                        continue

                    target_field = field_map.get(key)
                    if not target_field:
                        continue

                    if product.is_gram_bullion:
                        if target_field == 'stock_pieces':
                            target_field = 'stock_gram'
                        elif target_field == 'incoming_stock_pieces':
                            target_field = 'incoming_stock_gram'

                    if target_field.endswith('gram'):
                        parsed_val = Decimal(str(val).replace(',', '.'))
                        if target_field == 'stock_gram':
                            new_gram = parsed_val
                        else:
                            setattr(snap, target_field, parsed_val)
                    else:
                        parsed_val = int(val)
                        if target_field == 'stock_pieces':
                            new_pieces = parsed_val
                        else:
                            setattr(snap, target_field, parsed_val)
                    has_stock_change = True

                # Fiyat değişikliklerini kaydet
                if pricing_changed:
                    snap.save()

                # Stok değişikliği varsa adjustment kullan
                if has_stock_change:
                    StockService.adjustment(
                        product=product,
                        store=request.user.store,
                        actual_gram=new_gram,
                        actual_pieces=new_pieces,
                        ref_id=f"bulk_adj_{pid}",
                        user=request.user,
                        notes="Toplu Manuel Stok Düzenleme",
                    )

                updated += 1
            except Exception as e:
                failed.append({'product_id': pid, 'reason': str(e)})

    status = 'success' if not failed else 'partial'
    return JsonResponse({'status': status, 'updated': updated, 'failed': failed})


@login_required(login_url='login')
def get_has_gold_prices(request):
    """
    FAZ 7: Robust fiyat servisi — hiçbir koşulda 500 döndürmez.
    Öncelik: PriceService (Redis/DB) → compute_store_has_tl → Decimal('0')
    """
    # 1. PriceService (Redis cache → DB fallback)
    try:
        hs_data = PriceService.get_price('GOLD_24K')
        buy_hs_tl = float(hs_data.get('buy_tl', 0))
        sale_hs_tl = float(hs_data.get('sell_tl', 0))
        if buy_hs_tl > 0 and sale_hs_tl > 0:
            return JsonResponse({
                "buy_hs_tl": buy_hs_tl,
                "sale_hs_tl": sale_hs_tl
            })
    except Exception:
        pass

    # 2. Fallback: Eski compute_store_has_tl fonksiyonu
    try:
        store = request.user.store
        buy_hs_tl, sale_hs_tl = compute_store_has_tl(store)
        return JsonResponse({
            "buy_hs_tl": float(buy_hs_tl),
            "sale_hs_tl": float(sale_hs_tl)
        })
    except Exception:
        pass

    # 3. Son çare: sıfır döndür, 500 verme
    return JsonResponse({
        "buy_hs_tl": 0,
        "sale_hs_tl": 0
    })


@login_required(login_url='login')
def get_stock_movements_log(request):
    """
    FAZ 3 REFACTORED: InventoryMovement yerine StockLedger'dan oku.
    """
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 10))
    search_value = request.GET.get('search[value]', '')

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    movement_type_filter = request.GET.get('movement_type', 'all')

    store = request.user.store

    queryset = StockLedger.objects.filter(store=store).exclude(
        product__name__icontains="TRY"
    ).select_related('product', 'created_by').order_by('-created_on')

    if date_from:
        try:
            df = datetime.strptime(date_from, "%d/%m/%Y").date()
            queryset = queryset.filter(created_on__date__gte=df)
        except ValueError:
            pass

    if date_to:
        try:
            dt_Obj = datetime.strptime(date_to, "%d/%m/%Y").date()
            queryset = queryset.filter(created_on__date__lte=dt_Obj)
        except ValueError:
            pass

    # Direction filtreleme (entry/exit/update → IN/OUT)
    if movement_type_filter == 'entry':
        queryset = queryset.filter(direction=StockLedger.Direction.IN)
    elif movement_type_filter == 'exit':
        queryset = queryset.filter(direction=StockLedger.Direction.OUT)
    elif movement_type_filter == 'update':
        queryset = queryset.filter(reason__in=[StockLedger.Reason.ADJUSTMENT_PLUS, StockLedger.Reason.ADJUSTMENT_MINUS])

    if search_value:
        queryset = queryset.filter(
            Q(product__name__icontains=search_value) |
            Q(ref_id__icontains=search_value) |
            Q(created_by__first_name__icontains=search_value)
        )

    total = queryset.count()
    data_page = queryset[start:start + length]

    data = []
    for ledger in data_page:
        user_name = f"{ledger.created_by.first_name} {ledger.created_by.last_name}" if ledger.created_by else "-"
        direction_display = "Giriş" if ledger.direction == StockLedger.Direction.IN else "Çıkış"
        reason_display = ledger.get_reason_display() if hasattr(ledger, 'get_reason_display') else str(ledger.reason)
        date_str = ledger.created_on.strftime('%d.%m.%Y %H:%M')

        is_weight_based = False
        if ledger.quantity_gram and ledger.quantity_gram != 0:
            is_weight_based = True
        elif ledger.product and (ledger.product.is_gram_bullion or 'Altın' in (
                ledger.product.category.name if ledger.product.category else '')):
            is_weight_based = True

        qty_gram = float(ledger.quantity_gram or 0)
        qty_pieces = int(ledger.quantity_pieces or 0)

        # Stok before/after bilgisi StockLedger'da yoktur - sadece quantity gösterilir
        if is_weight_based:
            before_stock_str = "-"
            after_stock_str = f"{qty_gram:.3f} gr"
        else:
            before_stock_str = "-"
            after_stock_str = f"{qty_pieces} ad"

        movement_key = 'entry' if ledger.direction == StockLedger.Direction.IN else 'exit'

        data.append({
            'created_on': date_str,
            'product_name': ledger.product.name if ledger.product else "-",
            'process_no': ledger.ref_id or 'Manuel',
            'movement_type': f"{direction_display} ({reason_display})",
            'movement_key': movement_key,
            'stock_before': before_stock_str,
            'stock_after': after_stock_str,
            'quantity_pieces': qty_pieces,
            'quantity_weight': qty_gram,
            'user': user_name,
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data
    })


@role_required('PRODUCTS_GET_PRODUCT_DETAILS')
def get_product_details(request):
    user_store = request.user.store
    categories_data = []

    # 1. Mağazanın Canlı Has Kurlarını Al
    store_has_buy_eur, store_has_sale_eur = compute_store_has_tl(user_store)

    # Mağaza config'i (margin/chamber/manual-has kaldırıldı; geriye uyumluluk için)
    try:
        config = user_store.config
    except ObjectDoesNotExist:
        config = None
    store_margin_percent = Decimal('0.00')
    use_manual_has = False
    active_chamber = None

    # FAZ 3: Inventories yerine StockSnapshot'tan oku
    snapshots_map = {snap.product_id: snap for snap in StockSnapshot.objects.filter(store=user_store)}

    # ─── YOL 2 (SSOT Refactor): Döviz ürünleri için FX bakiyelerini bir kez topla ───
    # N+1 önlemi: tüm FX kasaların döviz toplamları sayfa başına TEK seferde okunur.
    from apps.banking.services import FXBalanceReader, get_currency_code_from_product
    try:
        fx_balances_map = FXBalanceReader.get_all_balances(user_store)
    except Exception:
        fx_balances_map = {}

    # FAZ 7: Bilezik ürün ID'lerini önceden çek (is_bracelet tespiti için)
    try:
        from apps.bracelets.models import Bracelets
        bracelet_product_ids = set(
            Bracelets.objects.filter(store=user_store).values_list('product_id', flat=True)
        )
    except ImportError:
        bracelet_product_ids = set()

    # Performans İçin: Seçili derneğin ürün fiyatlarını tek seferde çekip sözlüğe alıyoruz
    chamber_prices_map = {}
    if active_chamber:
        chamber_prices_map = {cp.product_id: cp for cp in ChamberProductPrice.objects.filter(chamber=active_chamber)}

    # Kategorileri Çek
    categories = Categories.objects.filter(is_active=True, is_deleted=False).order_by('order')
    ALLOWED = {'Ziynet', 'Döviz', 'Altın'}

    for category in categories:
        if category.name not in ALLOWED:
            continue

        # FAZ 17: TRY baz para birimi olduğu için satış ürünü olamaz.
        # "TRY - Türk Lirası" ürünü Hızlı İşlem ürün kataloğundan çıkarılır.
        # Dolar (USDTRY) ve Euro (EURTRY) döviz bozma mantığı için kalır.
        # ─── T2 (2026-04-29): Custom sorting ───
        # Eski `.order_by('order')` CharField (lexicographic) sıralamayı
        # IntegerField `display_order` üzerinden sayısal sıralamaya çevirdik.
        # Tie-breaker: aynı display_order'a sahip ürünler ada göre sıralanır.
        # ─── Cross-store leak fix (2026-05-07) ─────────────────────────────
        # Önceki yorumda "products zaten store ait ürünleri filtreler" diyordu
        # ama queryset'te store filtresi YOKTU → başka mağazaların custom ürünleri
        # (örn. "22 ayar gramm") Hızlı İşlem'e sızıyordu. get_all (Ürün Yönetimi)
        # ve get_categories_with_products (Perakende) ile aynı pattern uygulanıyor:
        #   Q(store=user_store): mağazanın kendi ürünleri
        #   Q(store__isnull=True): Has Altın, USDTRY, 22 Ayar Gram gibi global
        #     sistem ürünleri (tasks.py + populate scripts ile yaratılmış,
        #     UAT V3 P1-B kararıyla `is_protected=False` olanlar dahil)
        # ───────────────────────────────────────────────────────────────────
        products_qs = Products.objects.filter(
            category=category, is_active=True, is_deleted=False
        ).exclude(
            name__icontains="TRY - Türk Lirası"
        )
        if user_store:
            products_qs = products_qs.filter(
                Q(store=user_store) | Q(store__isnull=True)
            )
        products = products_qs.order_by('display_order', 'name')

        product_list = []

        for p in products:
            # Sözlüklerden (map) ilgili kaydı getir
            snapshot = snapshots_map.get(p.id)
            chamber_price = chamber_prices_map.get(p.id)

            # --- 3 KATMANLI FİYAT HİYERARŞİSİ ---

            # Katman 3: Global (Ortak)
            effective_buy_hs = p.buy_price_hs or Decimal(0)
            effective_sale_hs = p.sale_price_hs or Decimal(0)
            effective_labor = p.fixed_labor_amount or Decimal(0)

            # Katman 2: Dernek Fiyatı
            if active_chamber and chamber_price:
                if chamber_price.buy_price_hs is not None:
                    effective_buy_hs = chamber_price.buy_price_hs
                if chamber_price.sale_price_hs is not None:
                    effective_sale_hs = chamber_price.sale_price_hs
                if chamber_price.fixed_labor_amount is not None:
                    effective_labor = chamber_price.fixed_labor_amount

            # Katman 1a: Mağaza Manuel Has Fiyatı (Manuel Has modu açık VE özel fiyat girilmişse)
            # FAZ 3: StockSnapshot'tan oku
            if use_manual_has and snapshot and getattr(snapshot, 'use_custom_pricing', False):
                custom_buy = getattr(snapshot, 'custom_buy_price_hs', None)
                if custom_buy is not None and custom_buy > 0:
                    effective_buy_hs = custom_buy
                custom_sale = getattr(snapshot, 'custom_sale_price_hs', None)
                if custom_sale is not None and custom_sale > 0:
                    effective_sale_hs = custom_sale

            # Katman 1b: Mağaza Özel İşçilik (Manuel Has modundan BAĞIMSIZ)
            # İşçilik override'ı use_manual_has_calculation'a bağlı değildir.
            if snapshot and getattr(snapshot, 'use_custom_pricing', False):
                custom_labor = getattr(snapshot, 'custom_fixed_labor', None)
                if custom_labor is not None and custom_labor > 0:
                    effective_labor = custom_labor

            # ------------------------------------

            # Döviz ürünleri (is_currency=True) için buy_price_eur API kurudur.
            # has×kur çarpımı yapılmaz; altın fiyatı değiştikçe stale buy_price_hs
            # ile yanlış sonuç üretir.
            if getattr(p, 'is_currency', False):
                raw_buy_tl = Decimal(str(p.buy_price_eur or 0))
                raw_sale_tl = Decimal(str(p.sale_price_eur or 0))
            else:
                # EUR Fiyatlarını Hesapla (Kur * Has + İşçilik)
                raw_buy_tl = (effective_buy_hs * store_has_buy_eur)
                raw_sale_tl = (effective_sale_hs * store_has_sale_eur) + effective_labor

            final_sale_tl = raw_sale_tl

            # ─── YOL 2 (SSOT Refactor): Döviz ürün stoğu = FX kasa Payment bakiyesi ───
            # is_currency=True ürünler için stock_pieces / stock_gram artık SSOT değildir.
            # Frontend yeni `fx_balance` + `fx_currency` alanlarını okumalıdır.
            _is_currency = bool(getattr(p, 'is_currency', False))
            _fx_currency = get_currency_code_from_product(p) if _is_currency else None
            _fx_balance = fx_balances_map.get(_fx_currency, Decimal('0')) if _fx_currency else Decimal('0')

            if _is_currency:
                # Döviz: snapshot okunmaz; bakiye Payment'tan gelir
                _stock_pieces_out = 0
                _stock_gram_out = 0.0
            else:
                _stock_pieces_out = int(snapshot.stock_pieces if snapshot else 0)
                _stock_gram_out = float(snapshot.stock_gram if snapshot else 0)

            product_list.append({
                "id": str(p.id),
                "name": p.name,
                "buy_price_eur": float(round(raw_buy_tl, 2)),
                "sale_price_eur": float(round(final_sale_tl, 2)),
                "buy_price_hs": str(effective_buy_hs),
                "sale_price_hs": str(effective_sale_hs),
                "fixed_labor": str(effective_labor),
                "is_gram_bullion": p.is_gram_bullion,
                "is_currency": _is_currency,
                "stock_gram": _stock_gram_out,
                "stock_pieces": _stock_pieces_out,
                # YOL 2: Döviz ürünler için yeni SSOT alanları
                "fx_currency": _fx_currency or "",
                "fx_balance": float(_fx_balance),
                "category_name": p.category.name,
                "is_scrap": bool(p.is_scrap),
                "is_bracelet": p.id in bracelet_product_ids,
                "product_mileage": float(p.product_mileage or 0),
                # T2: drag-drop sıralama için frontend'e mevcut sırayı gönder
                "display_order": int(p.display_order or 0),
            })

        categories_data.append({
            "id": str(category.id),
            "name": category.name,
            "products": product_list
        })

    return JsonResponse({"data": categories_data})


# ============================================================================
# FAZ S5 (PIVOT 2026-04-23): Çoklu Maden Satış için FX Rate Endpoint
# ============================================================================
# WATCH/DIAMOND ürünlerin satış fiyatı döviz cinsindedir (USD/EUR/GBP/CHF).
# Frontend bu kuru çekip TL karşılığını hesaplar.
#
# Kaynak: Mevcut "USDTRY", "EURTRY", "GBPTRY", "CHFTRY" Products kayıtlarının
# sale_price_eur alanı kuru veriyor. Bu kayıtlar zaten döviz bozma akışında
# kullanılıyor (fast_views._get_exchange_rate_for_currency ile birebir uyum).
#
# Kuyumcu odası (dernek) veya canlı API entegrasyonu KULLANILMAZ — kuyumcu
# kuru kendi marjına göre tabeladan girer (Q2 kararı doğrultusunda).
#
# Yanıt formatı:
#   GET /products/get-fx-rates/?currencies=USD,EUR,GBP,CHF
#   {
#     "rates": {"USD": 33.50, "EUR": 36.20, "GBP": 42.10, "CHF": 37.85},
#     "missing": ["GBP"]   # Products kaydı yoksa veya 0 ise listede
#   }
#
# Default currencies: USD, EUR, GBP, CHF, TRY (TRY her zaman 1.0).
# Hiçbir koşulda 500 dönmez — eksik kur 0 olarak yansıtılır + missing listesi.
# ============================================================================

@login_required(login_url='login')
def get_fx_rates(request):
    """
    Saat ve Pırlanta satış ekranı için döviz/TL kurlarını döndürür.

    Kur kaynağı: "USDTRY", "EURTRY" gibi adlandırılmış Products kayıtlarının
    sale_price_eur alanı. Eğer ürün yoksa veya 0 ise rate 0 ve missing listede.

    Args (GET):
        currencies: virgülle ayrılmış kod listesi. Örn: "USD,EUR,GBP,CHF".
                    Boş ise default ['USD', 'EUR', 'GBP', 'CHF'].

    Returns:
        JsonResponse: {
            "rates":   {"USD": 33.50, "EUR": 36.20, ...},
            "missing": ["GBP"]
        }
    """
    DEFAULT_CCYS = ['USD', 'EUR', 'GBP', 'CHF']

    raw = (request.GET.get('currencies') or '').strip()
    if raw:
        requested = [c.strip().upper() for c in raw.split(',') if c.strip()]
    else:
        requested = DEFAULT_CCYS

    rates = {}
    missing = []

    for ccy in requested:
        # TRY her zaman 1.0 (baz para birimi)
        if ccy == 'TRY':
            rates['TRY'] = 1.0
            continue

        try:
            # "USDTRY", "EURTRY" gibi pattern ile arama
            ccy_product = (
                Products.objects
                .filter(
                    name__icontains=f'{ccy}TRY',
                    is_active=True,
                    is_deleted=False,
                )
                .only('sale_price_eur', 'name')
                .first()
            )
            if ccy_product and ccy_product.sale_price_eur and float(ccy_product.sale_price_eur) > 0:
                rates[ccy] = float(ccy_product.sale_price_eur)
            else:
                rates[ccy] = 0.0
                missing.append(ccy)
        except Exception:
            # Sessizce 0 — endpoint hiçbir koşulda 500 vermez
            rates[ccy] = 0.0
            missing.append(ccy)

    return JsonResponse({
        'rates': rates,
        'missing': missing,
    })


# ============================================================================
# T2 (2026-04-29): ÖZEL ÜRÜN SIRALAMASI — product_reorder
# ============================================================================
# Kullanıcı Ziynet (veya Döviz) tablosunda drag-drop ile sıralamayı değiştirdiğinde
# bu endpoint'e POST atılır. Frontend, yeni sırayı `[{"product_id": "...",
# "display_order": 0}, ...]` formatında gönderir; backend tek transaction'da
# bulk_update yapar.
#
# Güvenlik:
#   - Kullanıcının mağazasına ait ürünler VEYA is_protected=True global ürünler
#     güncellenir. Diğer mağazaların özel (store-specific) ürünleri skip edilir.
#   - is_deleted=False kontrolü.
#   - Atomic transaction: kısmi güncelleme olmaz.
#
# UAT V3 P1-A (2026-04-29):
#   Eski filter sadece `store=user_store` kontrolü yapıyordu; Has Altın, USDTRY,
#   22 Ayar Gram gibi `is_protected=True` (store=None veya farklı mağaza) global
#   ürünler skip ediliyordu. Drag-drop UI başarılı toast atıyor ama global
#   ürünlerin display_order'ı 0 kalıyordu. Filtre `Q(store=user_store) |
#   Q(is_protected=True)` olarak genişletildi. `display_order` global bir alan
#   olduğundan birden çok mağaza farklı sıra verirse son yazan kazanır — tek
#   mağaza kullanımı için sorun değil; çoklu mağaza tek deployment'ta sorun
#   olursa per-store display_order için ayrı tablo (StockSnapshot benzeri)
#   refactor'u gerekir.
# ============================================================================

@login_required(login_url='login')
@role_required('PRODUCTS_PRODUCT_INDEX')
def product_reorder(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Sadece POST desteklenir.'}, status=405)

    try:
        raw = request.POST.get('items') or request.body.decode('utf-8')
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            raise ValueError("items bir liste olmalı")
    except (ValueError, json.JSONDecodeError) as e:
        return JsonResponse({'status': 'error', 'message': f'Geçersiz istek: {e}'}, status=400)

    user_store = request.user.store
    if not user_store:
        return JsonResponse({'status': 'error', 'message': 'Mağaza atanmamış.'}, status=403)

    # UAT V3 P1-A: Kullanıcının mağazasına ait ürünler VEYA is_protected=True
    # global ürünler güncellenebilir. Diğer mağazaların özel ürünleri skip.
    requested_ids = [str(it.get('product_id')) for it in items if it.get('product_id')]
    products_qs = Products.objects.filter(
        id__in=requested_ids,
        is_deleted=False,
    ).filter(Q(store=user_store) | Q(is_protected=True))
    products_map = {str(p.id): p for p in products_qs}

    updated = []
    skipped = []

    with transaction.atomic():
        for it in items:
            pid = str(it.get('product_id') or '')
            try:
                new_order = int(it.get('display_order', 0))
            except (TypeError, ValueError):
                skipped.append(pid)
                continue
            p = products_map.get(pid)
            if not p:
                skipped.append(pid)
                continue
            p.display_order = new_order
            updated.append(p)

        if updated:
            Products.objects.bulk_update(updated, ['display_order'])

    return JsonResponse({
        'status': 'success',
        'updated_count': len(updated),
        'skipped': skipped,
    })
