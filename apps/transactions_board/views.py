from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from apps.process.models import Process
from apps.products.models import Products
from apps.customers.models import Customers
from apps.settings.models import StoreConfiguration  # Bu importu eklediğinizden emin olun
from apps.roles.decorators import role_required


def _get_common_context(request):
    """
    Tüm işlem sayfaları için ortak ve TAZE verileri hazırlar.
    """
    user = request.user

    # Mağazayı user üzerinden değil, ID üzerinden taze çekelim (Cache sorununu önler)
    store = getattr(user, 'store', None)
    if not store:
        return None, "Mağaza bilgisi bulunamadı."

    # --- KRİTİK GÜNCELLEME: Konfigürasyonu Taze Çek ---
    # get_or_create kullanarak ayar yoksa bile oluşturulmasını ve hata vermemesini sağlıyoruz.
    store_config, created = StoreConfiguration.objects.get_or_create(store=store)

    # Has Altın Fiyatlarını Getir
    # NOT: Fiyatlar 'Has Altın 24 Ayar' adli kayda yazilir. Stale 'Has Altın'
    # kaydini degil, taze kaydi okumaliyiz.
    has_product = Products.objects.filter(name='Has Altın 24 Ayar').first()
    buy_hs_tl = has_product.buy_price_eur if has_product else 0
    sale_hs_tl = has_product.sale_price_eur if has_product else 0

    # Son işlem numarası
    last_process = Process.objects.filter(store=store, is_status='IN_PROGRESS').first()
    process_no = last_process.process_no if last_process else ""

    customers = Customers.objects.filter(store=store, is_deleted=False)

    # Mağazanın birincil para birimi (UI sembolleri, kasa, fiyat etiketleri için)
    primary_currency = store_config.primary_currency or 'EUR'
    primary_currency_symbol = store_config.PRIMARY_CURRENCY_SYMBOLS.get(
        primary_currency, primary_currency
    )

    return {
        'store': store,
        'store_config': store_config,  # Şablona direkt bu objeyi gönderiyoruz
        # juwelier_plus port: Kitco JS polling icin spot currency/unit
        'store_base_spot_currency': store_config.base_spot_currency or 'EUR',
        'store_base_spot_unit': store_config.base_spot_unit or 'OZ',
        # Mağaza birincil para birimi — UI sembolleri ve hesaplamalar için SSOT
        'store_primary_currency': primary_currency,
        'store_currency_symbol': primary_currency_symbol,
        'customers': customers,
        'process_no': process_no,
        'buy_hs_tl': buy_hs_tl,
        'sale_hs_tl': sale_hs_tl,
    }, None


@login_required
@role_required('TRANSACTIONS_BOARD_FAST_INDEX_VIEW')
def fast_index_view(request):
    context, error = _get_common_context(request)
    if error: return HttpResponseForbidden(error)

    context['title'] = 'Hızlı İşlem'
    return render(request, 'management/transactions_board/fast_index.html', context)


@login_required
@role_required('TRANSACTIONS_BOARD_RETAIL_INDEX_VIEW')
def retail_index_view(request):
    context, error = _get_common_context(request)
    if error: return HttpResponseForbidden(error)

    context['title'] = 'Perakende İşlem'
    return render(request, 'management/transactions_board/retail_index.html', context)


@login_required
@role_required('TRANSACTIONS_BOARD_WHOLESALE_INDEX_VIEW')
def wholesale_index_view(request):
    context, error = _get_common_context(request)
    if error: return HttpResponseForbidden(error)

    context['title'] = 'Toptan İşlem'

    # FAZ 21: Toptancı ekranına kasa listesi ekle (Kasa Seçimi dropdown için)
    import json
    from apps.banking.models import BankAccount
    store = context.get('store')
    bank_accounts_list = []
    if store:
        ba_qs = BankAccount.objects.filter(
            store=store, is_deleted=False, is_active=True,
        ).values('id', 'name', 'account_type', 'currency')
        bank_accounts_list = list(ba_qs)
        # UUID'leri string'e çevir
        for ba in bank_accounts_list:
            ba['id'] = str(ba['id'])
    context['bank_accounts_json'] = json.dumps(bank_accounts_list)

    return render(request, 'management/transactions_board/wholesale_index.html', context)


@login_required
@role_required('TRANSACTIONS_BOARD_OPERATIONS_INDEX')
def operations_index(request):
    context, error = _get_common_context(request)
    if error: return HttpResponseForbidden(error)

    context['title'] = 'Operasyonlar'
    return render(request, 'management/transactions_board/operations_index.html', context)