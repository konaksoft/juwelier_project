# apps/custody/views.py
import random
import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_date
from django.contrib.auth.decorators import login_required

from apps.roles.decorators import role_required
from apps.custody.models import CustomerCustodyLedger
from apps.customers.models import Customers
from apps.products.models import Products

from apps.stock_management.services.stock_service import StockService, InsufficientStockError
from apps.stock_management.models import StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.definitions.categories.models import Categories
from apps.scraps.models import Scraps
from apps.bracelets.models import Bracelets

logger = logging.getLogger(__name__)


def generate_custody_process_no():
    return 'CUS' + ''.join(random.choices('0123456789', k=10))


@login_required
@role_required('CUSTODY_CUSTODY_INDEX')
def custody_index(request):
    store = request.user.store
    qs = (CustomerCustodyLedger.objects
          .filter(store=store,
                  custody_type=CustomerCustodyLedger.CUSTODY_IN,
                  is_returned=False)
          .aggregate(total_hs=Coalesce(Sum('amount_hs'), Decimal(0))))

    # --- DÜZELTİLEN KISIM: Global ürünleri de dahil et ve gereksizleri gizle ---
    products = Products.objects.filter(
        Q(store=store) | Q(store__isnull=True),
        is_active=True,
        is_deleted=False,
        is_scrap=False,
        is_currency=False
    ).exclude(
        Q(category__name__iexact='Hurda') |
        Q(category__name__iexact='Bilezik') |
        Q(category__name__iexact='Döviz') |
        Q(category__name__icontains='Doviz') |
        Q(category__name__icontains='Döviz') |
        Q(name__endswith='TRY') |
        Q(name__in=['USD', 'EUR', 'GBP', 'CAD', 'QAR', 'CHF', 'JPY', 'SAR', 'AED', 'AUD', 'KWD', 'OMR', 'RUB', 'BGN', 'NOK', 'SEK', 'DKK', 'CNY', 'ILS', 'MAD', 'JOD','EUR/KG','onstry'])
    ).order_by('name')

    return render(request, 'management/custody/index.html', {
        'title': 'Emanet (Depo)',
        'customers': Customers.objects.filter(store=store, is_active=True, is_deleted=False),
        'products': products,
        'total_hs': qs['total_hs'],
    })


@login_required
@role_required('CUSTODY_ADD_CUSTODY')
@transaction.atomic
def add_custody(request):
    try:
        store = request.user.store
        cust = Customers.objects.get(id=request.POST.get('customer_id'))
        custody_id = request.POST.get('custody_id')
        ctype = request.POST.get('custody_type') or CustomerCustodyLedger.CUSTODY_IN

        emanet_turu = request.POST.get('emanet_turu')  # 'ziynet', 'hurda', 'bilezik'
        description = request.POST.get('description', '')

        # Genel Miktar Alımları
        qty_piece = int(request.POST.get('quantity_piece') or 0)
        qty_gram = Decimal(request.POST.get('quantity_gram', '0').replace(',', '.'))
        amount_hs = Decimal(request.POST.get('amount_hs', '0').replace(',', '.'))

        product = None  # Dinamik olarak belirlenecek

        # --- 1. KISIM: ÜRÜN BULMA VEYA YARATMA (TÜRE GÖRE) ---
        if emanet_turu == 'ziynet':
            prod_id = request.POST.get('product_id')
            if not prod_id:
                return JsonResponse({'result': False, 'error_msg': 'Ziynet ürünü seçilmelidir.'})
            product = Products.objects.get(id=prod_id)

            # Arayüzden gelen inputu al (Adet veya Gram olabilir)
            raw_val = request.POST.get('quantity_piece', '0').strip()
            if not raw_val:
                raw_val = '0'
            raw_val = raw_val.replace(',', '.')

            # Ürün gram bazlı (is_gram_bullion) ise gelen veriyi quantity_gram'a yaz
            if getattr(product, 'is_gram_bullion', False):
                qty_gram = Decimal(raw_val)
                qty_piece = 0
            else:
                qty_piece = int(float(raw_val))
                qty_gram = Decimal('0')

        elif emanet_turu == 'hurda':
            mileage = Decimal(request.POST.get('milyem', '0'))
            if mileage <= 0 or qty_gram <= 0:
                return JsonResponse(
                    {'result': False, 'error_msg': 'Hurda için Ayar seçimi ve Gram girilmesi zorunludur.'})

            category, _ = Categories.objects.get_or_create(name='Hurda')

            product = Products.objects.filter(store=store, category=category, product_mileage=mileage,
                                              is_scrap=True).first()

            # Eğer havuz yoksa yeni oluştur
            if not product:
                has_price = (mileage / Decimal('1000'))

                # --- YENİ GELİŞTİRME: Milyem değerini Ayar metnine dönüştürme ---
                mileage_str = str(int(mileage))
                karat_map = {
                    '995': '24 Ayar',
                    '916': '22 Ayar',
                    '875': '21 Ayar',
                    '750': '18 Ayar',
                    '585': '14 Ayar',
                    '333': '8 Ayar'
                }

                # Eşleşme bulursa '22 Ayar' döner, bulamazsa '916 Milyem' döner
                karat_name = karat_map.get(mileage_str, f"{mileage_str} Milyem")
                # -------------------------------------------------------------

                product = Products.objects.create(
                    store=store, category=category,
                    name=f"{karat_name} Hurda", gram=Decimal('0'),
                    product_mileage=mileage, buy_price_hs=has_price, sale_price_hs=has_price,
                    is_scrap=True, is_gram_bullion=True, is_active=True
                )
                Scraps.objects.create(store=store, product=product, created_by=request.user)

            qty_piece = 0  # Hurda gram bazlıdır, bu yüzden adet 0'dır

        elif emanet_turu == 'bilezik':
            milyem = Decimal(request.POST.get('milyem', '0'))
            b_name = request.POST.get('bracelet_name', '').strip() or f"{int(milyem)} Milyem Emanet Bilezik"
            if milyem <= 0 or qty_gram <= 0:
                return JsonResponse(
                    {'result': False, 'error_msg': 'Bilezik için Ayar seçimi ve Gram girilmesi zorunludur.'})

            category, _ = Categories.objects.get_or_create(name='Bilezik')
            has_fiyat = (milyem / Decimal('1000'))

            product = Products.objects.create(
                store=store, category=category, name=b_name, gram=qty_gram,
                product_mileage=milyem, buy_price_hs=has_fiyat, sale_price_hs=has_fiyat,
                is_gram_bullion=True, is_active=True
            )
            Bracelets.objects.create(store=store, product=product, created_by=request.user)
            qty_piece = 1  # 1 adet bilezik (ama gram üzerinden stok tutulur)

        # --- 2. KISIM: EMANET DEFTERİNE KAYIT ---
        if custody_id:
            # GÜNCELLEME (Stokla oynanmaz, sadece metinler güncellenir)
            row = CustomerCustodyLedger.objects.get(id=custody_id)
            row.description = description
            row.save()
        else:
            # YENİ EMANET
            process_no = generate_custody_process_no()
            row = CustomerCustodyLedger.objects.create(
                customer=cust, store=store, product=product,
                custody_type=ctype, quantity_piece=qty_piece, quantity_gram=qty_gram,
                amount_hs=amount_hs, process_no=process_no, description=description,
                is_returned=False, created_by=request.user, received_by=request.user
            )

            # --- 3. KISIM: FİZİKİ STOĞA GİRİŞ (StockService) ---
            if product and ctype == CustomerCustodyLedger.CUSTODY_IN:
                # WAC bozulmaması için o anki sanal maliyet hesaplanır
                try:
                    hs_data = PriceService.get_price('GOLD_24K')
                    current_has_buy_tl = Decimal(str(hs_data.get('buy_tl', '0')))
                except Exception:
                    current_has_buy_tl = Decimal('0')

                # Eğer ürünün kendi buy_price_hs değeri varsa onu kullan, yoksa milyemden hesapla
                unit_cost_hs = product.buy_price_hs if product.buy_price_hs else (
                        Decimal(str(product.product_mileage or '0')) / Decimal('1000'))
                unit_cost_tl = (unit_cost_hs * current_has_buy_tl).quantize(
                    Decimal('0.01')) if current_has_buy_tl > 0 else Decimal('0')

                StockService.record_entry(
                    product=product, store=store,
                    quantity_gram=qty_gram, quantity_pieces=qty_piece,
                    reason=StockLedger.Reason.CUSTODY_IN,
                    ref_type='custody_manual', ref_id=str(row.id),
                    unit_cost_hs=unit_cost_hs, unit_cost_tl=unit_cost_tl, hs_rate_tl=current_has_buy_tl,
                    user=request.user,
                    notes=f"Emanet Alındı ({emanet_turu.upper()}) - İşlem: {process_no}"
                )

        return JsonResponse({'result': True, 'row_id': str(row.id)})

    except Exception as e:
        logger.exception("Emanet ekleme hatası")
        return JsonResponse({'result': False, 'error_msg': f"Hata: {str(e)}"})


@login_required
@role_required('CUSTODY_DELETE')
def delete(request):
    ids = request.POST.getlist('ids[]')
    CustomerCustodyLedger.objects.filter(id__in=ids).delete()
    return JsonResponse({'result': True})


@login_required
@role_required('CUSTODY_CHANGE_STATUS')
@transaction.atomic
def change_status(request):
    """
    Emanet Kısmi/Tam İade (Teslim) İşlemi.
    Bu işlemde müşteri emanetini geri alır ve kasanın fiziksel havuzundan altın düşer.
    """
    obj_id = request.POST.get('id')
    transfer_val = request.POST.get('transfer_amount')

    if not obj_id:
        ids = request.POST.getlist('ids[]')
        if not ids:
            return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'})
        rows = CustomerCustodyLedger.objects.filter(id__in=ids, store=request.user.store)
        for r in rows:
            _toggle_record_simple(request, r)
        return JsonResponse({'result': True})

    try:
        row = CustomerCustodyLedger.objects.select_for_update().get(id=obj_id, store=request.user.store)
    except CustomerCustodyLedger.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt bulunamadı.'})

    # Eğer miktar girilmemişse (direkt teslim butonuna tıklandıysa)
    if not transfer_val:
        _toggle_record_simple(request, row)
        return JsonResponse({'result': True})

    try:
        transfer_amount = Decimal(transfer_val.replace(',', '.'))
    except:
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz miktar.'})

    if transfer_amount <= 0:
        return JsonResponse({'result': False, 'error_msg': 'Miktar 0 dan büyük olmalı.'})

    is_gram_based = row.quantity_gram > 0
    total_amount = row.quantity_gram if is_gram_based else Decimal(row.quantity_piece)

    if transfer_amount > total_amount:
        return JsonResponse({'result': False, 'error_msg': 'Girilen miktar mevcut miktardan fazla olamaz.'})

    # Eğer istenen miktar, mevcutla aynıysa tam teslim yap
    if abs(transfer_amount - total_amount) < Decimal('0.0001'):
        _toggle_record_simple(request, row)
        return JsonResponse({'result': True})

    # Kısmi teslimat oranı hesabı
    ratio = transfer_amount / total_amount

    new_row = CustomerCustodyLedger.objects.get(id=obj_id)
    new_row.pk = None
    new_row.is_returned = True
    new_row.custody_type = CustomerCustodyLedger.CUSTODY_OUT
    new_row.delivered_by = request.user

    if is_gram_based:
        new_row.quantity_gram = transfer_amount
        new_row.quantity_piece = int(row.quantity_piece * float(ratio))
    else:
        new_row.quantity_piece = int(transfer_amount)
        new_row.quantity_gram = row.quantity_gram * ratio

    new_row.amount_hs = row.amount_hs * ratio
    new_row.save()

    # Asıl kayıttan düş (Kalan miktar güncelleniyor)
    row.quantity_piece -= new_row.quantity_piece
    row.quantity_gram -= new_row.quantity_gram
    row.amount_hs -= new_row.amount_hs
    row.save()

    # --- KISMİ EMANET ÇIKIŞI: STOK HAVUZUNDAN DÜŞÜLÜYOR ---
    if new_row.product:
        try:
            StockService.record_exit(
                product=new_row.product,
                store=request.user.store,
                quantity_gram=new_row.quantity_gram,
                quantity_pieces=new_row.quantity_piece,
                reason=StockLedger.Reason.CUSTODY_OUT,
                ref_type='custody_return',
                ref_id=str(new_row.id),
                user=request.user,
                notes=f"Emanet Kısmi Teslim - Müşteri: {row.customer.first_name}"
            )
        except InsufficientStockError:
            raise Exception(f"Emaneti teslim etmek için havuzda yeterli '{new_row.product.name}' bulunmuyor!")

    return JsonResponse({'result': True})


def _toggle_record_simple(request, r):
    """ Emanetin Tamamını Teslim Etme (Çıkış) veya Teslim İptali (Geri Giriş) İşlemi """
    was_returned = r.is_returned
    r.is_returned = not was_returned
    r.custody_type = CustomerCustodyLedger.CUSTODY_IN if was_returned else CustomerCustodyLedger.CUSTODY_OUT
    r.delivered_by = None if was_returned else request.user
    r.save(update_fields=['is_returned', 'custody_type', 'delivered_by'])

    if r.product:
        try:
            if r.custody_type == CustomerCustodyLedger.CUSTODY_OUT:
                # Teslim Edildi -> Stok Havuzundan Çıkar
                StockService.record_exit(
                    product=r.product, store=request.user.store,
                    quantity_gram=r.quantity_gram, quantity_pieces=r.quantity_piece,
                    reason=StockLedger.Reason.CUSTODY_OUT, ref_type='custody_return',
                    ref_id=str(r.id), user=request.user, notes="Emanet Tam Teslim"
                )
            else:
                # Teslimden Geri Alındı (İptal) -> Stok Havuzuna Geri Ekle
                StockService.record_entry(
                    product=r.product, store=request.user.store,
                    quantity_gram=r.quantity_gram, quantity_pieces=r.quantity_piece,
                    reason=StockLedger.Reason.CUSTODY_IN, ref_type='custody_return_undo',
                    ref_id=str(r.id), user=request.user, notes="Emanet Teslimi İptal Edildi (Geri Alındı)"
                )
        except InsufficientStockError:
            raise Exception("Müşteriye teslim edilecek yeterli stok (havuzda) bulunmuyor!")


@login_required
@role_required('CUSTODY_CUSTODY_GET_ALL')
def custody_get_all(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))

    ctype = request.GET.get('custody_type', '')
    is_returned = request.GET.get('is_returned', '')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    search_txt = request.GET.get('search[value]', '')

    qs = (CustomerCustodyLedger.objects
          .filter(store=request.user.store)
          .select_related('customer', 'product', 'created_by'))

    customer_id = request.GET.get("customer_id")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)

    if ctype:
        qs = qs.filter(custody_type=ctype)
    if is_returned != '':
        qs = qs.filter(is_returned=(is_returned.lower() == 'true'))
    if date_from:
        qs = qs.filter(created_on__date__gte=parse_date(date_from))
    if date_to:
        qs = qs.filter(created_on__date__lte=parse_date(date_to))
    if search_txt:
        qs = qs.filter(
            Q(customer__first_name__icontains=search_txt) |
            Q(customer__last_name__icontains=search_txt) |
            Q(process_no__icontains=search_txt) |
            Q(description__icontains=search_txt) |
            Q(product__name__icontains=search_txt)
        )

    total = qs.count()
    columns = [None, "customer__first_name", "process_no", "created_by__first_name", "product__name", "quantity_piece",
               "quantity_gram", "amount_hs", "created_on", "is_returned", None]
    col_idx = int(request.GET.get('order[0][column]', 9))
    order_dir = request.GET.get('order[0][dir]', 'desc')
    order_by_field = columns[col_idx] if 0 <= col_idx < len(columns) else "created_on"
    if not order_by_field: order_by_field = "created_on"
    order_by = f"-{order_by_field}" if order_dir == 'desc' else order_by_field

    qs = qs.order_by(order_by)[start:start + length] if length != -1 else qs.order_by(order_by)

    def _nm(u):
        return f"{u.first_name} {u.last_name}".strip() if u else "-"

    data = [{
        "id": str(r.id),
        "customer_full": f"{r.customer.first_name} {r.customer.last_name}",
        "customer_id": r.customer.id,
        "product": r.product.name if r.product else "-",
        "product_id": r.product.id if r.product else None,
        "quantity_piece": r.quantity_piece,
        "quantity_gram": f"{r.quantity_gram:.3f}",
        "amount_hs": f"{r.amount_hs:.3f}",
        "process_no": r.process_no or "-",
        "staff_received": _nm(r.received_by),
        "staff_delivered": _nm(r.delivered_by),
        "staff": f"{r.created_by.first_name} {r.created_by.last_name}" if r.created_by else "-",
        "description": r.description or "",
        "created_on": r.created_on.strftime("%d/%m/%Y %H:%M"),
        "is_returned": r.is_returned,
    } for r in qs]

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data,
    })
