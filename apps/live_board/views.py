import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.core.cache import cache

from apps.chambers.models import Chambers, ChamberProductPrice
from apps.live_board.models import LiveBoardSettings
from apps.products.models import Products
from apps.settings.models import StoreConfiguration
from apps.stock_management.models import StockSnapshot
from apps.stores.models import Stores

logger = logging.getLogger('live_board')

# Canli fiyat verisi Redis TTL (saniye). 5 saniyede bir poll yapiliyor;
# 4 saniyelik cache ile her sorgu DB'ye degil Redis'e gider.
_LIVE_DATA_CACHE_TTL = 4


# ============================================================================
# YARDIMCI FONKSIYONLAR
# ============================================================================

def _get_price_metadata():
    """
    PriceProvider durumunu kontrol et ve metadata dondur.
    Sessiz failover: API cokerse son fiyat Products'ta kalir,
    hicbir uyari gosterilmez. Bu metadata sadece dahili log icindir.
    """
    try:
        from apps.stock_management.models import PriceProvider
        provider = PriceProvider.objects.filter(
            is_active=True,
        ).order_by('priority').first()

        if provider and provider.last_success_at:
            return {
                'source': 'api',
                'provider_name': provider.display_name,
                'last_updated': provider.last_success_at.isoformat(),
                'api_healthy': provider.status == PriceProvider.ProviderStatus.ACTIVE,
            }
    except Exception:
        pass

    return {
        'source': 'manual',
        'provider_name': '',
        'last_updated': '',
        'api_healthy': False,
    }


def _get_has_altin_tl():
    """Has Altin TL fiyatini Products'tan veya PriceService'ten al."""
    has_buy_tl = Decimal('0')
    has_sell_tl = Decimal('0')

    has_product = Products.objects.filter(
        name='Has Altın 24 Ayar', is_deleted=False
    ).values('buy_price_tl', 'sale_price_tl').first()

    if has_product:
        has_buy_tl = Decimal(str(has_product['buy_price_tl'] or 0))
        has_sell_tl = Decimal(str(has_product['sale_price_tl'] or 0))

    if has_buy_tl <= 0 or has_sell_tl <= 0:
        try:
            from apps.stock_management.services.price_service import PriceService
            ps_buy, ps_sell = PriceService.get_has_altin_tl()
            if ps_buy > 0:
                has_buy_tl = ps_buy
            if ps_sell > 0:
                has_sell_tl = ps_sell
        except Exception:
            pass

    return has_buy_tl, has_sell_tl


def _get_store_and_config(request):
    """Ortak magaza + config cekme yardimcisi."""
    current_store = getattr(request.user, 'store', None)
    if not current_store:
        current_store = Stores.objects.filter(is_active=True, is_deleted=False).first()

    config = None
    if current_store:
        config = StoreConfiguration.objects.filter(store=current_store).select_related(
            'active_pricing_chamber'
        ).first()

    return current_store, config


# ============================================================================
# CANLI PIYASALAR ANA SAYFA
# ============================================================================

@login_required(login_url='login')
def index_view(request):
    current_store, config = _get_store_and_config(request)

    chamber = None
    chamber_name = ''

    if config and config.active_pricing_chamber:
        chamber = config.active_pricing_chamber
        chamber_name = chamber.name

    # Magaza ekran adi: once LiveBoardSettings custom_board_name, sonra store title
    display_name = ''
    custom_logo_url = ''

    # LiveBoardSettings
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

    # Manuel mod kontrolu
    use_manual = False
    if config:
        use_manual = config.use_manual_has_calculation

    # Dernek modu: chamber varsa → dernek modu aktif (kullanici manuel kapatamaz)
    is_chamber_mode = bool(chamber)

    context = {
        'title': 'Canli Piyasalar',
        'store': current_store,
        'store_display_name': display_name,
        'custom_logo_url': custom_logo_url,
        'chamber_name': chamber_name,
        'chamber_id': str(chamber.id) if chamber else '',
        'store_id': str(current_store.id) if current_store else '',
        'is_superuser': request.user.is_superuser,
        'use_manual_mode': use_manual,
        'is_chamber_mode': is_chamber_mode,
        # Gorunurluk ayarlari
        'show_custom_name': lb_settings.show_custom_name if lb_settings else True,
        'show_custom_logo': lb_settings.show_custom_logo if lb_settings else True,
        'show_currency_section': lb_settings.show_currency_section if lb_settings else True,
        'show_sarrafiye_section': lb_settings.show_sarrafiye_section if lb_settings else True,
    }
    return render(request, 'management/live_board/index.html', context)


# ============================================================================
# CANLI EKRAN AYARLARI SAYFASI (Superuser)
# ============================================================================

@login_required(login_url='login')
def board_settings_view(request):
    """
    Canli ekran gorsel ayarlari sayfasi.
    Sadece is_superuser=True kullanicilar erisebilir.
    """
    if not request.user.is_superuser:
        return HttpResponseForbidden('Bu sayfaya erişim yetkiniz yok.')

    current_store, config = _get_store_and_config(request)

    if not current_store:
        return redirect('live_board:index')

    lb_settings, _ = LiveBoardSettings.objects.get_or_create(store=current_store)

    # Dernek bilgisi
    chamber_name = ''
    is_chamber_mode = False
    if config and config.active_pricing_chamber:
        chamber_name = config.active_pricing_chamber.name
        is_chamber_mode = True

    # Manuel mod bilgisi
    use_manual = False
    if config:
        use_manual = config.use_manual_has_calculation

    # Aktif mod tespiti
    if is_chamber_mode:
        active_mode = 'Dernek Modu'
        active_mode_desc = 'Fiyatlar dernek tarafindan belirlenmektedir.'
    elif use_manual:
        active_mode = 'Manuel Has Modu'
        active_mode_desc = 'Fiyatlar urun bazinda tanimli has degerleri ile hesaplanmaktadir.'
    else:
        active_mode = 'API Modu'
        active_mode_desc = 'Fiyatlar otomatik olarak piyasa verisinden cekilmektedir.'

    context = {
        'title': 'Canli Ekran Ayarlari',
        'store': current_store,
        'is_superuser': request.user.is_superuser,
        'lb_settings': lb_settings,
        'chamber_name': chamber_name,
        'is_chamber_mode': is_chamber_mode,
        'use_manual_mode': use_manual,
        'active_mode': active_mode,
        'active_mode_desc': active_mode_desc,
        'custom_logo_url': lb_settings.custom_board_logo.url if lb_settings.custom_board_logo else '',
    }
    return render(request, 'management/live_board/board_settings.html', context)


# ============================================================================
# CANLI FIYAT VERISI API
# ============================================================================

def get_live_data(request):
    """
    Canli fiyat verisi endpoint'i.

    Mod mantigi:
    1. Dernek Modu (chamber var):
       Kullanicinin manuel has degerleri YOK SAYILIR.
       TL_buy  = chamber_buy_hs  * global_has_altin_buy_tl
       TL_sale = chamber_sale_hs * global_has_altin_sale_tl + chamber_labor

    2. Manuel Mod (chamber yok + use_manual_has_calculation=True):
       MAGAZAYA AIT Products kaydindaki buy_price_hs / sale_price_hs degerleri
       guncel Has Altin TL fiyati ile carpilarak hesaplanir.
       TL_buy  = store_product.buy_price_hs  * has_altin_buy_tl
       TL_sale = store_product.sale_price_hs * has_altin_sale_tl

    3. API Modu (chamber yok + use_manual=False):
       Products tablosundaki TL fiyatlari dogrudan kullanilir.

    ONEMLI: Products tablosunda her magaza icin ayri kayitlar vardir
    (store ForeignKey). Manuel has hesaplamasi icin MUTLAKA ilgili
    magazanin urunleri cekilmelidir, aksi halde yanlis has degerleri
    (baska magazanin veya global varsayilan) kullanilir.
    """

    store_id = request.GET.get('store_id', '') or ''

    # -----------------------------------------------------------------------
    # Redis Cache Kontrolü
    # Her (store_id, kullanici) kombinasyonu icin ayri cache anahtari kullanilir.
    # Magaza verisi izole edilmis; store_id bos ise anonim anahtar kullanilir.
    # -----------------------------------------------------------------------
    cache_key = f'live_data:{store_id or "global"}'
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return JsonResponse(cached)
    except Exception as cache_err:
        # Redis erisilemediyse sessizce devam et (DB'den cek)
        logger.debug(f"Cache okuma hatasi (kritik degil): {cache_err}")

    # --- Mod tespiti ---
    store = None
    chamber = None
    chamber_price_map = {}
    use_manual = False
    active_mode = 'api'

    # Gorunurluk ayarlari (JS tarafina gonderilecek)
    show_settings = {
        'show_custom_name': True,
        'show_custom_logo': True,
        'show_currency_section': True,
        'show_sarrafiye_section': True,
    }

    if store_id:
        try:
            store = Stores.objects.get(id=store_id)
            config = StoreConfiguration.objects.filter(store=store).select_related(
                'active_pricing_chamber'
            ).first()

            if config and config.active_pricing_chamber:
                chamber = config.active_pricing_chamber

            if config:
                use_manual = config.use_manual_has_calculation

            # LiveBoardSettings gorsel ayarlari
            try:
                lb_settings = LiveBoardSettings.objects.get(store=store)
                show_settings = {
                    'show_custom_name': lb_settings.show_custom_name,
                    'show_custom_logo': lb_settings.show_custom_logo,
                    'show_currency_section': lb_settings.show_currency_section,
                    'show_sarrafiye_section': lb_settings.show_sarrafiye_section,
                }
            except LiveBoardSettings.DoesNotExist:
                pass

        except (Stores.DoesNotExist, Exception):
            pass

    # Mod belirleme: Dernek her zaman oncelikli
    if chamber:
        active_mode = 'chamber'
        for cp in ChamberProductPrice.objects.filter(chamber=chamber).select_related('product'):
            chamber_price_map[cp.product.name] = cp
    elif use_manual:
        active_mode = 'manual'
    else:
        active_mode = 'api'

    # --- Urunleri cek ---
    gold_targets = ['Has Altın 24 Ayar', 'Gram Altın', '22 Ayar Gram', 'Ons']

    coin_targets = [
        'Yeni Çeyrek', 'Eski Çeyrek',
        'Yeni Yarım', 'Eski Yarım',
        'Yeni Tam', 'Eski Tam',
        'Yeni Ata', 'Eski Ata'
    ]

    currency_map = {
        'USDTRY': 'USD',
        'EURTRY': 'EUR',
        'GBPTRY': 'GBP',
        'CHFTRY': 'CHF'
    }

    other_targets = ['Gümüş', 'EUR/USD']
    all_targets = gold_targets + coin_targets + list(currency_map.keys()) + other_targets

    # ------------------------------------------------------------------
    # URUN SORGULAMA STRATEJISI
    # ------------------------------------------------------------------
    # Products tablosunda her magaza icin ayri kayitlar var (store FK).
    # Ornegin "Eski Ceyrek" icin:
    #   - Magaza A'nin kaydi: store=A, sale_price_hs=9.00 (kullanici girdi)
    #   - Global/baska magaza kaydi: store=None, sale_price_hs=1.701 (API)
    #
    # API modunda: Celery task'inin guncelledigi GLOBAL urunleri kullan.
    # Magaza bazli eski/stale kayitlari ATLAYARAK dogrudan en guncel API
    # fiyatlarina ulas. Magaza bazli kayitlar sadece chamber/manual modda
    # anlamlidir.
    #
    # Chamber/Manuel modda:
    # 1. Once MAGAZAYA AIT urunleri cek (store=current_store)
    # 2. Magazada bulunmayan urunler icin GLOBAL'e fallback yap
    # 3. Bu sayede kullanicinin girdigi has degerleri MUTLAKA kullanilir
    # ------------------------------------------------------------------

    product_values_fields = (
        'name', 'buy_price_tl', 'sale_price_tl', 'profit', 'description',
        'buy_price_hs', 'sale_price_hs', 'fixed_labor_amount'
    )

    # Adim 1: Magazaya ait urunleri cek (SADECE chamber/manual modda)
    # API modunda bu adim atlanir — stale magaza kayitlarini onlemek icin.
    store_product_map = {}
    if store and active_mode in ('chamber', 'manual'):
        store_products_qs = Products.objects.filter(
            store=store,
            name__in=all_targets,
            is_deleted=False
        ).values(*product_values_fields)

        for sp in store_products_qs:
            store_product_map[sp['name']] = sp

    # Adim 2: Eksik urunler icin global fallback
    # API modunda store_product_map bos oldugu icin TUM urunler buradan gelir
    # ve Celery task'inin guncelledigi en taze fiyatlar kullanilir.
    found_names = set(store_product_map.keys())
    missing_names = [n for n in all_targets if n not in found_names]

    fallback_product_map = {}
    if missing_names:
        fallback_qs = Products.objects.filter(
            name__in=missing_names,
            is_deleted=False
        ).values(*product_values_fields)

        for fp in fallback_qs:
            # Her isimden sadece ilk bulunani al (ayni isimde birden fazla olabilir)
            if fp['name'] not in fallback_product_map:
                fallback_product_map[fp['name']] = fp

    # Adim 3: Birlestir — magaza urunleri ONCELIKLI (chamber/manual),
    # API modunda tamamen global urunler kullanilir
    product_list = []
    for name in all_targets:
        if name in store_product_map:
            product_list.append(store_product_map[name])
        elif name in fallback_product_map:
            product_list.append(fallback_product_map[name])

    # --- Has Altin TL kurunu al ---
    # TUM modlarda gerekli: chamber/manual overlay hesaplamasi ve
    # tepe kart (summary bar) gosterimi icin.
    has_altin_buy_tl = Decimal('0')
    has_altin_sale_tl = Decimal('0')

    has_product = next((p for p in product_list if p['name'] == 'Has Altın 24 Ayar'), None)
    if has_product:
        has_altin_buy_tl = Decimal(str(has_product['buy_price_tl'] or 0))
        has_altin_sale_tl = Decimal(str(has_product['sale_price_tl'] or 0))

    # PriceService fallback — Products tablosunda deger yoksa
    if has_altin_buy_tl <= 0 or has_altin_sale_tl <= 0:
        try:
            from apps.stock_management.services.price_service import PriceService
            ps_buy, ps_sell = PriceService.get_has_altin_tl()
            if ps_buy > 0:
                has_altin_buy_tl = ps_buy
            if ps_sell > 0:
                has_altin_sale_tl = ps_sell
        except Exception as e:
            logger.warning(f"PriceService fallback basarisiz: {e}")

    # ------------------------------------------------------------------
    # Manuel mod icin MAGAZAYA AIT has degerlerini ayri bir haritada tut.
    # Bu harita, apply_manual_overlay() icinde kullanilacak.
    #
    # KRITIK BILGI:
    # Kullanicinin girdigi ozel has degerleri (ornegin "Eski Ceyrek" = 9.00)
    # Products tablosunda DEGIL, StockSnapshot tablosunda saklanir:
    #   - StockSnapshot.custom_buy_price_hs
    #   - StockSnapshot.custom_sale_price_hs
    #   - StockSnapshot.use_custom_pricing = True
    #
    # Products tablosundaki "Eski Ceyrek" kaydi global'dir (store=None)
    # ve API'den gelen varsayilan has degerini tutar (ornegin 1.701).
    # Bu nedenle Products.objects.filter(store=magaza) ile sorgulayinca
    # 0 sonuc doner!
    #
    # Dogru yaklasim:
    # 1. StockSnapshot'tan use_custom_pricing=True olan kayitlari cek
    # 2. custom_buy_price_hs / custom_sale_price_hs degerlerini kullan
    # 3. StockSnapshot'ta bulunmayan urunler icin Products fallback
    # ------------------------------------------------------------------
    store_has_map = {}
    if active_mode == 'manual' and store:
        # BIRINCIL KAYNAK: StockSnapshot ozel fiyatlari
        # Kullanici Stok Yonetimi ekranindan has degerlerini girdiginde
        # bu tabloya yazilir (update_inventory_ajax → snap.custom_sale_price_hs)
        snapshot_qs = StockSnapshot.objects.filter(
            store=store,
            use_custom_pricing=True,
            product__name__in=all_targets,
            product__is_deleted=False,
        ).select_related('product').values(
            'product__name',
            'custom_buy_price_hs',
            'custom_sale_price_hs',
        )

        for snap in snapshot_qs:
            p_name = snap['product__name']
            buy_hs = Decimal(str(snap['custom_buy_price_hs'] or 0))
            sale_hs = Decimal(str(snap['custom_sale_price_hs'] or 0))
            if buy_hs > 0 or sale_hs > 0:
                store_has_map[p_name] = {
                    'buy_price_hs': buy_hs,
                    'sale_price_hs': sale_hs,
                }

        # IKINCIL KAYNAK (fallback): Magazaya ait Products kayitlari
        # Bazi urunler dogrudan store FK ile olusturulmus olabilir
        # (ornegin hurda/bilezik icin retail_views → Products.objects.create(store=...))
        # StockSnapshot'ta olmayan urunler icin bunlari da kontrol et
        snapshot_names = set(store_has_map.keys())
        remaining_targets = [n for n in all_targets if n not in snapshot_names]

        if remaining_targets:
            store_products_qs = Products.objects.filter(
                store=store,
                name__in=remaining_targets,
                is_deleted=False,
            ).values('name', 'buy_price_hs', 'sale_price_hs')

            for sp in store_products_qs:
                buy_hs = Decimal(str(sp['buy_price_hs'] or 0))
                sale_hs = Decimal(str(sp['sale_price_hs'] or 0))
                if buy_hs > 0 or sale_hs > 0:
                    store_has_map[sp['name']] = {
                        'buy_price_hs': buy_hs,
                        'sale_price_hs': sale_hs,
                    }

        logger.info(
            f"Manuel has haritasi yuklendi: store={store}, "
            f"urun_sayisi={len(store_has_map)}, "
            f"urunler={list(store_has_map.keys())}"
        )

    # --- Dernek fiyat overlay fonksiyonu ---
    def apply_chamber_overlay(item):
        """Eger dernek bu urun icin fiyat belirlemisse TL'yi yeniden hesapla."""
        cp = chamber_price_map.get(item['name'])
        if not cp:
            return item

        if has_altin_buy_tl <= 0 or has_altin_sale_tl <= 0:
            return item

        result = item.copy()

        if cp.buy_price_hs is not None:
            result['buy_price_tl'] = float(
                (Decimal(str(cp.buy_price_hs)) * has_altin_buy_tl).quantize(Decimal('0.01'))
            )
        if cp.sale_price_hs is not None:
            labor = Decimal(str(cp.fixed_labor_amount or 0))
            result['sale_price_tl'] = float(
                (Decimal(str(cp.sale_price_hs)) * has_altin_sale_tl + labor).quantize(Decimal('0.01'))
            )

        return result

    # --- Manuel has overlay fonksiyonu ---
    def apply_manual_overlay(item):
        """
        Manuel mod aktifse, MAGAZAYA AIT Products kaydindaki
        buy_price_hs / sale_price_hs degerleri guncel Has Altin TL
        fiyati ile carpilarak TL fiyati hesaplanir.

        Oncelik sirasi:
        1. store_has_map'te (magazanin kendi urunu) has degeri varsa → ONU KULLAN
        2. Yoksa item'in kendi buy_price_hs/sale_price_hs degerini kontrol et
        3. Hicbiri yoksa (her ikisi de 0) → overlay yapma, orijinal TL fiyatini koru
        """
        if has_altin_buy_tl <= 0 or has_altin_sale_tl <= 0:
            return item

        # Oncelik 1: Magazanin kendi has degerleri (store_has_map)
        store_has = store_has_map.get(item['name'])
        if store_has:
            buy_hs = store_has['buy_price_hs']
            sale_hs = store_has['sale_price_hs']
        else:
            # Oncelik 2: Item'in kendi has degerleri (fallback)
            buy_hs = Decimal(str(item.get('buy_price_hs') or 0))
            sale_hs = Decimal(str(item.get('sale_price_hs') or 0))

        # Has degeri girilmemisse overlay yapma, orijinal TL fiyatini koru
        if buy_hs <= 0 and sale_hs <= 0:
            return item

        result = item.copy()

        if buy_hs > 0:
            result['buy_price_tl'] = float(
                (buy_hs * has_altin_buy_tl).quantize(Decimal('0.01'))
            )
        if sale_hs > 0:
            result['sale_price_tl'] = float(
                (sale_hs * has_altin_sale_tl).quantize(Decimal('0.01'))
            )

        return result

    # --- Genel overlay uygulayici ---
    def apply_price_overlay(item):
        if active_mode == 'chamber':
            return apply_chamber_overlay(item)
        elif active_mode == 'manual':
            return apply_manual_overlay(item)
        return item

    def filter_and_sort(target_list):
        result = []
        for name in target_list:
            item = next((p for p in product_list if p['name'] == name), None)
            if item:
                item = apply_price_overlay(item)
                display_name = item['name']

                # Has Altin 24 Ayar → Has Altin (tepe kart ve tablo gosterimi)
                if display_name == 'Has Altın 24 Ayar':
                    display_name = 'Has Altin'

                if display_name == 'Ons':
                    display_name = 'ONS'

                if 'Yeni' in display_name:
                    display_name = display_name.replace('Yeni ', '') + '(Yeni)'
                if 'Eski' in display_name:
                    display_name = display_name.replace('Eski ', '') + '(Eski)'

                new_item = item.copy()
                new_item['display_name'] = display_name

                # JS tarafinda find() eslesmesi icin name alanini da donustur
                if item['name'] == 'Has Altın 24 Ayar':
                    new_item['name'] = 'Has Altin'
                elif item['name'] == 'Ons':
                    new_item['name'] = 'ONS'

                result.append(new_item)
        return result

    def filter_currency():
        result = []
        for db_name, display_name in currency_map.items():
            item = next((p for p in product_list if p['name'] == db_name), None)
            if item:
                item = apply_price_overlay(item)
                new_item = item.copy()
                new_item['display_name'] = display_name
                new_item['name'] = display_name
                result.append(new_item)
        return result

    data = {
        'gold': filter_and_sort(gold_targets),
        'coins': filter_and_sort(coin_targets),
        'currency': filter_currency(),
        'others': filter_and_sort(other_targets),
    }

    # Dahili metadata (sessiz — musteriye uyari gosterilmez)
    metadata = _get_price_metadata()
    metadata['mode'] = active_mode
    if active_mode == 'manual':
        metadata['store_has_count'] = len(store_has_map)

    response_payload = {
        'status': 'success',
        'data': data,
        'meta': metadata,
        'show_settings': show_settings,
    }

    # -----------------------------------------------------------------------
    # Redis Cache: hesaplanan sonucu TTL sureyle sakla.
    # Cache anahtari: magaza ID + mod kombinasyonu. Her magazanin fiyatlari
    # izole edilmis sekilde cache'lenir; biri diger magazanin verisini gormez.
    # -----------------------------------------------------------------------
    try:
        cache.set(cache_key, response_payload, timeout=_LIVE_DATA_CACHE_TTL)
    except Exception as cache_err:
        logger.debug(f"Cache yazma hatasi (kritik degil): {cache_err}")

    return JsonResponse(response_payload)


# ============================================================================
# CANLI EKRAN GORSEL AYARLARI API (GET / POST)
# ============================================================================

@login_required(login_url='login')
def live_board_settings_api(request):
    """
    Canli ekran GORSEL ayarlarini oku (GET) veya guncelle (POST).
    Sadece is_superuser=True kullanicilar POST yapabilir.

    GET: Mevcut gorsel ayarlari dondurur.
    POST Body (JSON):
    {
        "custom_board_name": "Kuyumculuk AS",
        "show_custom_name": true,
        "show_custom_logo": true,
        "show_currency_section": false,
        "show_sarrafiye_section": true
    }

    NOT: Logo dosyasi multipart/form-data ile ayri bir POST'ta gonderilir.
    """
    # Yazma islemleri (POST) sadece superuser'a acik
    if request.method != 'GET' and not request.user.is_superuser:
        return JsonResponse({'status': 'error', 'message': 'Bu islemi yapmaya yetkiniz yok.'}, status=403)

    current_store, config = _get_store_and_config(request)

    if not current_store:
        return JsonResponse({'status': 'error', 'message': 'Magaza bulunamadi.'}, status=404)

    lb_settings, _ = LiveBoardSettings.objects.get_or_create(store=current_store)

    # GET — mevcut ayarlari dondur
    if request.method == 'GET':
        return JsonResponse({
            'status': 'success',
            'custom_board_name': lb_settings.custom_board_name,
            'custom_board_logo': lb_settings.custom_board_logo.url if lb_settings.custom_board_logo else '',
            'show_custom_name': lb_settings.show_custom_name,
            'show_custom_logo': lb_settings.show_custom_logo,
            'show_currency_section': lb_settings.show_currency_section,
            'show_sarrafiye_section': lb_settings.show_sarrafiye_section,
        })


    # Multipart form-data (logo upload) veya JSON
    content_type = request.content_type or ''

    if 'multipart/form-data' in content_type:
        # Form-data: logo dosyasi + diger alanlar
        if 'custom_board_logo' in request.FILES:
            lb_settings.custom_board_logo = request.FILES['custom_board_logo']
        if 'custom_board_name' in request.POST:
            lb_settings.custom_board_name = request.POST.get('custom_board_name', '').strip()
        if 'show_custom_name' in request.POST:
            lb_settings.show_custom_name = request.POST.get('show_custom_name') == 'true'
        if 'show_custom_logo' in request.POST:
            lb_settings.show_custom_logo = request.POST.get('show_custom_logo') == 'true'
        if 'show_currency_section' in request.POST:
            lb_settings.show_currency_section = request.POST.get('show_currency_section') == 'true'
        if 'show_sarrafiye_section' in request.POST:
            lb_settings.show_sarrafiye_section = request.POST.get('show_sarrafiye_section') == 'true'
    else:
        # JSON body
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
        if 'show_currency_section' in body:
            lb_settings.show_currency_section = bool(body['show_currency_section'])
        if 'show_sarrafiye_section' in body:
            lb_settings.show_sarrafiye_section = bool(body['show_sarrafiye_section'])

    lb_settings.save()

    logger.info(
        f"Canli ekran gorsel ayarlari guncellendi: user={request.user}, store={current_store}"
    )

    return JsonResponse({
        'status': 'success',
        'message': 'Ayarlar basariyla kaydedildi.',
    })
