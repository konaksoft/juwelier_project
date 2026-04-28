from apps.roles.decorators import role_required
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.core.paginator import Paginator
from decimal import Decimal

from apps.products.models import Products

# --- FAZ 4: StockSnapshot ve StockLedger entegrasyonu ---
from apps.stock_management.models import StockSnapshot, StockLedger


@login_required(login_url='login')
@role_required('INVENTORIES_GET_ALL')
def get_all(request):
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 10))
    search_value = request.GET.get('search[value]', '')
    category = request.GET.get('category')
    user_store = request.user.store

    snapshots_with_stock = StockSnapshot.objects.filter(
        Q(store=user_store) | Q(product__is_protected=True)
    ).values('product').annotate(
        total_stock_pieces=Coalesce(Sum('stock_pieces'), 0),
        total_stock_weight=Coalesce(Sum('stock_gram'), Decimal('0.000'))
    )

    product_ids = [item['product'] for item in snapshots_with_stock]

    products_with_stock = Products.objects.filter(id__in=product_ids, is_deleted=False)

    if search_value:
        products_with_stock = products_with_stock.filter(
            Q(name__icontains=search_value) |
            Q(category__name__icontains=search_value)
        )

    total = products_with_stock.count()

    paginator = Paginator(products_with_stock, length)
    page_number = (start // length) + 1
    page = paginator.get_page(page_number)

    data = []
    for product in page.object_list:
        snapshot_data = next((item for item in snapshots_with_stock if item['product'] == product.id),
                             {'total_stock_pieces': 0, 'total_stock_weight': 0})

        # Son stok hareketini StockLedger'dan oku
        latest_ledger = StockLedger.objects.filter(
            product=product, store=user_store
        ).order_by('-created_on').first()
        created_on = latest_ledger.created_on.strftime(
            '%Y-%m-%d %H:%M:%S') if latest_ledger and latest_ledger.created_on else ''

        if category:
            if product.category.name != category:
                continue

        data.append({
            'id': product.id,
            'product__name': product.name,
            'gold_dry': product.gold_dry,
            'stock_pieces': snapshot_data['total_stock_pieces'],
            'stock_weight': float(snapshot_data['total_stock_weight']),
            'created_on': created_on,
            'category': product.category.name if product.category else '',
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data
    })
