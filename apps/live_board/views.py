import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

from apps.live_board.models import LiveBoardSettings, KitcoPriceCache
from apps.settings.models import StoreConfiguration
from apps.stores.models import Stores

logger = logging.getLogger('live_board')


# ============================================================================
# YARDIMCI FONKSIYONLAR
# ============================================================================

def _get_store_and_config(request):
    """Ortak mağaza + config çekme yardımcısı."""
    current_store = getattr(request.user, 'store', None)
    if not current_store:
        current_store = Stores.objects.filter(is_active=True, is_deleted=False).first()

    config = None
    if current_store:
        config = StoreConfiguration.objects.filter(store=current_store).first()

    return current_store, config


# ============================================================================
# CANLI PIYASALAR ANA SAYFA
# ============================================================================

@login_required(login_url='login')
def index_view(request):
    current_store, config = _get_store_and_config(request)

    display_name = ''
    custom_logo_url = ''

    lb_settings = None
    if current_store:
        lb_settings, _ = LiveBoardSettings.objects.get_or_create(store=current_store)

    if lb_settings and lb_settings.custom_board_name:
        display_name = lb_settings.custom_board_name
    elif current_store:
        display_name = current_store.title or ''
        if not display_name and current_store.company:
            display_name = current_store.company.title or ''

    if lb_settings and lb_settings.custom_board_logo:
        custom_logo_url = lb_settings.custom_board_logo.url

    context = {
        'title': 'Canli Piyasalar',
        'store': current_store,
        'store_display_name': display_name,
        'custom_logo_url': custom_logo_url,
        'store_id': str(current_store.id) if current_store else '',
        'is_superuser': request.user.is_superuser,
        # Görünürlük ayarları
        'show_custom_name': lb_settings.show_custom_name if lb_settings else True,
        'show_custom_logo': lb_settings.show_custom_logo if lb_settings else True,
        # Kitco paneli ayarları
        'show_kitco_section': lb_settings.show_kitco_section if lb_settings else True,
        'kitco_display_currency': lb_settings.kitco_display_currency if lb_settings else 'EUR',
        'kitco_display_unit': lb_settings.kitco_display_unit if lb_settings else 'GRAM',
    }
    return render(request, 'management/live_board/index.html', context)


# ============================================================================
# CANLI EKRAN AYARLARI SAYFASI
# ============================================================================

@login_required(login_url='login')
def board_settings_view(request):
    """Canlı ekran görsel ayarları sayfası."""
    current_store, config = _get_store_and_config(request)

    if not current_store:
        return redirect('live_board:index')

    lb_settings, _ = LiveBoardSettings.objects.get_or_create(store=current_store)

    active_mode = 'API Modu'
    active_mode_desc = 'Fiyatlar otomatik olarak Kitco spot verisinden çekilmektedir.'

    context = {
        'title': 'Canli Ekran Ayarlari',
        'store': current_store,
        'is_superuser': request.user.is_superuser,
        'lb_settings': lb_settings,
        'active_mode': active_mode,
        'active_mode_desc': active_mode_desc,
        'custom_logo_url': lb_settings.custom_board_logo.url if lb_settings.custom_board_logo else '',
    }
    return render(request, 'management/live_board/board_settings.html', context)


# ============================================================================
# CANLI EKRAN GORSEL AYARLARI API (GET / POST)
# ============================================================================

@login_required(login_url='login')
def live_board_settings_api(request):
    """
    Canlı ekran görsel ayarlarını oku (GET) veya güncelle (POST).

    POST Body (JSON):
    {
        "custom_board_name": "Juwelier AG",
        "show_custom_name": true,
        "show_custom_logo": true,
        "show_kitco_section": true,
        "kitco_display_currency": "EUR",
        "kitco_display_unit": "GRAM"
    }

    NOT: Logo dosyası multipart/form-data ile ayrı bir POST'ta gönderilir.
    """
    current_store, config = _get_store_and_config(request)

    if not current_store:
        return JsonResponse({'status': 'error', 'message': 'Magaza bulunamadi.'}, status=404)

    lb_settings, _ = LiveBoardSettings.objects.get_or_create(store=current_store)

    if request.method == 'GET':
        return JsonResponse({
            'status': 'success',
            'custom_board_name': lb_settings.custom_board_name,
            'custom_board_logo': lb_settings.custom_board_logo.url if lb_settings.custom_board_logo else '',
            'show_custom_name': lb_settings.show_custom_name,
            'show_custom_logo': lb_settings.show_custom_logo,
            'show_kitco_section': lb_settings.show_kitco_section,
            'kitco_display_currency': lb_settings.kitco_display_currency,
            'kitco_display_unit': lb_settings.kitco_display_unit,
        })

    content_type = request.content_type or ''

    if 'multipart/form-data' in content_type:
        if 'custom_board_logo' in request.FILES:
            lb_settings.custom_board_logo = request.FILES['custom_board_logo']
        if 'custom_board_name' in request.POST:
            lb_settings.custom_board_name = request.POST.get('custom_board_name', '').strip()
        if 'show_custom_name' in request.POST:
            lb_settings.show_custom_name = request.POST.get('show_custom_name') == 'true'
        if 'show_custom_logo' in request.POST:
            lb_settings.show_custom_logo = request.POST.get('show_custom_logo') == 'true'
        if 'show_kitco_section' in request.POST:
            lb_settings.show_kitco_section = request.POST.get('show_kitco_section') == 'true'
        if 'kitco_display_currency' in request.POST:
            val = (request.POST.get('kitco_display_currency') or '').strip().upper()
            if val in dict(LiveBoardSettings.KitcoDisplayCurrency.choices):
                lb_settings.kitco_display_currency = val
        if 'kitco_display_unit' in request.POST:
            val = (request.POST.get('kitco_display_unit') or '').strip().upper()
            if val in dict(LiveBoardSettings.KitcoDisplayUnit.choices):
                lb_settings.kitco_display_unit = val
    else:
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'status': 'error', 'message': 'Gecersiz JSON formati.'}, status=400)

        if 'custom_board_name' in body:
            lb_settings.custom_board_name = str(body['custom_board_name']).strip()
        if 'show_custom_name' in body:
            lb_settings.show_custom_name = bool(body['show_custom_name'])
        if 'show_custom_logo' in body:
            lb_settings.show_custom_logo = bool(body['show_custom_logo'])
        if 'show_kitco_section' in body:
            lb_settings.show_kitco_section = bool(body['show_kitco_section'])
        if 'kitco_display_currency' in body:
            val = str(body.get('kitco_display_currency') or '').strip().upper()
            if val in dict(LiveBoardSettings.KitcoDisplayCurrency.choices):
                lb_settings.kitco_display_currency = val
        if 'kitco_display_unit' in body:
            val = str(body.get('kitco_display_unit') or '').strip().upper()
            if val in dict(LiveBoardSettings.KitcoDisplayUnit.choices):
                lb_settings.kitco_display_unit = val

    lb_settings.save()

    logger.info(
        f"Canli ekran gorsel ayarlari guncellendi: user={request.user}, store={current_store}"
    )

    return JsonResponse({
        'status': 'success',
        'message': 'Ayarlar basariyla kaydedildi.',
    })


# ============================================================================
# KITCO CANLI PIYASA API (juwelier_plus port)
# ============================================================================
# IZOLASYON NOTU
# --------------
# Aşağıdaki view yalnızca `KitcoPriceCache` tablosunu OKUR.
# Products, StockSnapshot, Rates veya başka herhangi bir mantığa
# HIÇ DOKUNMAZ.

TROY_OZ_TO_GRAM = Decimal('31.1034768')


def api_get_live_kitco_data(request):
    """
    Kitco cache'indeki güncel uluslararası spot fiyatları JSON olarak döner.

    * Cache tablosu YALNIZCA OZ (Troy Ons) cinsinden kayıt tutar.
    * `?unit=GRAM` istenirse OZ fiyatı anlık olarak 31.1034768'e
      bölünerek response seviyesinde dönüştürülür (tabloya yazım yok).
    * Desteklenen para birimleri: USD, EUR, GBP, CAD, AUD, JPY, CHF.

    Query Parametreleri
    -------------------
    ?currency=EUR  (varsayilan: EUR)
    ?unit=OZ       (varsayilan: OZ) — OZ veya GRAM
    """
    currency = (request.GET.get('currency') or 'EUR').strip().upper()
    unit = (request.GET.get('unit') or 'OZ').strip().upper()

    valid_currencies = {c for c, _lbl in KitcoPriceCache.Currency.choices}
    valid_units = {u for u, _lbl in KitcoPriceCache.Unit.choices}

    if currency not in valid_currencies:
        currency = KitcoPriceCache.Currency.EUR
    if unit not in valid_units:
        unit = KitcoPriceCache.Unit.OZ

    metal_labels = dict(KitcoPriceCache.MetalType.choices)

    queryset = (
        KitcoPriceCache.objects
        .filter(currency=currency, unit=KitcoPriceCache.Unit.OZ)
        .order_by('metal_type')
    )

    want_gram = (unit == KitcoPriceCache.Unit.GRAM)
    data = []
    newest_db_update = None
    newest_source_time = None

    for row in queryset:
        bid_raw = Decimal(str(row.bid_price or 0))
        ask_raw = Decimal(str(row.ask_price or 0))

        if want_gram and TROY_OZ_TO_GRAM > 0:
            try:
                bid_out = (bid_raw / TROY_OZ_TO_GRAM).quantize(Decimal('0.0001'))
                ask_out = (ask_raw / TROY_OZ_TO_GRAM).quantize(Decimal('0.0001'))
            except Exception:
                bid_out = Decimal('0.0000')
                ask_out = Decimal('0.0000')
            out_unit = KitcoPriceCache.Unit.GRAM
        else:
            bid_out = bid_raw.quantize(Decimal('0.0001'))
            ask_out = ask_raw.quantize(Decimal('0.0001'))
            out_unit = KitcoPriceCache.Unit.OZ

        data.append({
            'metal_type': row.metal_type,
            'metal_label': str(metal_labels.get(row.metal_type, row.metal_type)),
            'currency': row.currency,
            'unit': out_unit,
            'bid_price': str(bid_out),
            'ask_price': str(ask_out),
            'last_updated': (
                row.last_updated.isoformat() if row.last_updated else None
            ),
            'source_timestamp': (
                row.source_timestamp.isoformat() if row.source_timestamp else None
            ),
        })
        if row.last_updated and (
            newest_db_update is None or row.last_updated > newest_db_update
        ):
            newest_db_update = row.last_updated
        if row.source_timestamp and (
            newest_source_time is None or row.source_timestamp > newest_source_time
        ):
            newest_source_time = row.source_timestamp

    return JsonResponse({
        'status': 'success',
        'data': data,
        'meta': {
            'currency': currency,
            'unit': unit,
            'record_count': len(data),
            'derived_from_oz': want_gram,
            'troy_oz_to_gram': str(TROY_OZ_TO_GRAM) if want_gram else None,
            'last_updated': (
                newest_db_update.isoformat() if newest_db_update else None
            ),
            'source_timestamp': (
                newest_source_time.isoformat() if newest_source_time else None
            ),
        },
    })
