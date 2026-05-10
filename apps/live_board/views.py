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
    has_buy_eur = Decimal('0')
    has_sell_tl = Decimal('0')

    has_product = Products.objects.filter(
        name='Has Altın 24 Ayar', is_deleted=False
    ).values('buy_price_eur', 'sale_price_eur').first()

    if has_product:
        has_buy_eur = Decimal(str(has_product['buy_price_eur'] or 0))
        has_sell_tl = Decimal(str(has_product['sale_price_eur'] or 0))

    if has_buy_eur <= 0 or has_sell_tl <= 0:
        try:
            from apps.stock_management.services.price_service import PriceService
            ps_buy, ps_sell = PriceService.get_has_altin_tl()
            if ps_buy > 0:
                has_buy_eur = ps_buy
            if ps_sell > 0:
                has_sell_tl = ps_sell
        except Exception:
            pass

    return has_buy_eur, has_sell_tl


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

    # FAZ 20 (2026-04-30): Live Board'da görünebilecek tüm ürünleri grup grup
    # listele. Board Settings sayfasındaki "Ürün Görünürlük & Sıralama" bölümü
    # bu listeyi render eder. Gizli/sıralı durumu lb_settings'ten merge edilir.
    #
    # Liste statik olarak get_live_data()'daki target listeleriyle SİMETRİK
    # tutulur — listelerden biri değişirse buradaki de güncellenmelidir.
    _gold_targets = ['Has Altın 24 Ayar', 'Gram Altın', '22 Ayar Gram', 'Ons']
    _coin_targets = [
        'Yeni Çeyrek', 'Eski Çeyrek',
        'Yeni Yarım', 'Eski Yarım',
        'Yeni Tam', 'Eski Tam',
        'Yeni Ata', 'Eski Ata',
    ]
    _currency_codes = [
        ('USDTRY', 'USD'), ('EURTRY', 'EUR'),
        ('GBPTRY', 'GBP'), ('CHFTRY', 'CHF'),
    ]
    _other_targets = [
        'Gümüş TL',
        'EUR/USD', 'GBP/USD', 'AUD/USD',
        'USD/CHF', 'USD/JPY', 'USD/CAD',
        'USD/KG', 'EUR/KG',
        'Gümüş ONS', 'Platin ONS', 'Paladyum ONS',
        'Platin TL', 'Paladyum TL',
    ]

    hidden_set_ctx = set(lb_settings.hidden_items or [])
    lb_order_ctx = lb_settings.live_board_item_order or {}

    def _build_group(group_key, group_label, names):
        rows = []
        for n in names:
            display = n
            if n == 'Has Altın 24 Ayar':
                display = 'Has Altın'
            elif n == 'Ons':
                display = 'ONS'
            rows.append({
                'db_name': n,
                'display_name': display,
                'group': group_key,
                'group_label': group_label,
                'is_hidden': n in hidden_set_ctx,
                'order': lb_order_ctx.get(n, 0),
            })
        return rows

    all_live_items = []
    all_live_items += _build_group('gold', 'Altın Fiyatları', _gold_targets)
    all_live_items += _build_group('coins', 'Sarrafiye (Ziynet)', _coin_targets)
    # Döviz için DB code (örn. USDTRY) ile display label (USD) ayrı tutulur
    for db_code, label in _currency_codes:
        all_live_items.append({
            'db_name': db_code,
            'display_name': label,
            'group': 'currency',
            'group_label': 'Döviz Kurları',
            'is_hidden': db_code in hidden_set_ctx,
            'order': lb_order_ctx.get(db_code, 0),
        })
    all_live_items += _build_group('others', 'Diğer / Pariteler', _other_targets)

    # FAZ 21 (2026-04-30): Mağazaya özel (custom) ürünler
    # Sabit hedef listelerinde olmayan, kullanıcının Ürünler ekranından
    # eklediği ürünler (ör. "Reşat Altın"). store FK'i mevcut mağazaya
    # eşit olmalı. is_deleted=False koruma. Hardcoded isimler hariç tut
    # (aynı isimde global + store kaydı varsa duplikasyon olmaz).
    _hardcoded_names = set(
        _gold_targets + _coin_targets +
        [c for c, _ in _currency_codes] + _other_targets
    )
    custom_qs = Products.objects.filter(
        store=current_store,
        is_deleted=False,
    ).exclude(name__in=_hardcoded_names).values('name')
    seen_custom = set()
    for cp in custom_qs:
        cname = (cp.get('name') or '').strip()
        if not cname or cname in seen_custom:
            continue
        seen_custom.add(cname)
        all_live_items.append({
            'db_name': cname,
            'display_name': cname,
            'group': 'custom',
            'group_label': 'Mağazaya Özel Ürünler',
            'is_hidden': cname in hidden_set_ctx,
            'order': lb_order_ctx.get(cname, 0),
        })

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
        # FAZ 20 (2026-04-30): Ürün görünürlük & sıralama listesi
        'all_live_items': all_live_items,
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

    # FAZ 21 (2026-04-30): Manuel döviz kuru — has modundan bağımsız.
    # use_manual_currency_rate=True ise döviz ürünleri (USDTRY/EURTRY vb.)
    # için API kuru yerine StoreConfiguration.manual_currency_rates'tan
    # mağazaya özel TL kuru okunur. Has hesaplaması (use_manual) ile
    # ortogonal: birinin açık olması diğerini etkilemez.
    use_manual_currency = False
    manual_currency_rates_map = {}

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
                # FAZ 21: manuel kur okuma
                use_manual_currency = bool(config.use_manual_currency_rate)
                manual_currency_rates_map = config.manual_currency_rates or {}

            # LiveBoardSettings gorsel ayarlari
            try:
                lb_settings = LiveBoardSettings.objects.get(store=store)
                show_settings = {
                    'show_custom_name': lb_settings.show_custom_name,
                    'show_custom_logo': lb_settings.show_custom_logo,
                    'show_currency_section': lb_settings.show_currency_section,
                    'show_sarrafiye_section': lb_settings.show_sarrafiye_section,
                }
                # FAZ 20 (2026-04-30): Ürün bazında görünürlük & sıralama
                hidden_items_list = lb_settings.hidden_items or []
                lb_item_order = lb_settings.live_board_item_order or {}
            except LiveBoardSettings.DoesNotExist:
                hidden_items_list = []
                lb_item_order = {}

        except (Stores.DoesNotExist, Exception):
            hidden_items_list = []
            lb_item_order = {}
    else:
        hidden_items_list = []
        lb_item_order = {}

    # Set lookup için hidden_items'i set'e çevir (O(1) membership test)
    hidden_set = set(hidden_items_list)

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

    # FAZ 20 (2026-04-30): "Diğer / Pariteler" bölümünü API'den gelen parite
    # ürünleriyle genişlet. Bu ürünler `apps/products/tasks.py` PARITY_CODE_MAP
    # tarafından Products tablosuna kaydediliyor (USDKG → "USD/KG" gibi).
    # buy_price_eur / sale_price_eur alanları target birimde tutuluyor (USD, EUR vb.).
    # is_currency=False — retail/wholesale flow'larına sızmaz.
    other_targets = [
        'Gümüş TL',
        'EUR/USD', 'GBP/USD', 'AUD/USD',
        'USD/CHF', 'USD/JPY', 'USD/CAD',
        'USD/KG', 'EUR/KG',
        'Gümüş ONS', 'Platin ONS', 'Paladyum ONS',
        'Platin TL', 'Paladyum TL',
    ]

    # FAZ 21 (2026-04-30): Mağazaya özel (custom) ürünler — kuyumcunun
    # Ürünler ekranından eklediği ve hardcoded target listelerinde olmayan
    # kayıtlar (ör. "Reşat Altın"). Sadece mağazaya ait kayıtlar (store FK).
    custom_targets = []
    if store:
        _hardcoded_set = set(
            gold_targets + coin_targets + list(currency_map.keys()) + other_targets
        )
        custom_qs = Products.objects.filter(
            store=store,
            is_deleted=False,
        ).exclude(name__in=_hardcoded_set).values_list('name', flat=True)
        seen_ct = set()
        for cn in custom_qs:
            cn_s = (cn or '').strip()
            if cn_s and cn_s not in seen_ct:
                seen_ct.add(cn_s)
                custom_targets.append(cn_s)

    all_targets = (
        gold_targets + coin_targets + list(currency_map.keys())
        + other_targets + custom_targets
    )

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

    # FAZ 20 (2026-04-30): `display_order` eklendi. Products.index.html'deki
    # SortableJS drag-drop ile yazılan değer Live Board (Canlı Piyasa) panellerinde
    # özel sıralamayı sağlar. Hızlı İşlem ve Perakende ekranlarıyla SSOT olur.
    product_values_fields = (
        'id',  # FAZ 21: manuel kur lookup'ı için (manual_currency_rates UUID-bazlı)
        'name', 'buy_price_eur', 'sale_price_eur', 'profit', 'description',
        'buy_price_hs', 'sale_price_hs', 'fixed_labor_amount',
        'display_order',
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
        has_altin_buy_tl = Decimal(str(has_product['buy_price_eur'] or 0))
        has_altin_sale_tl = Decimal(str(has_product['sale_price_eur'] or 0))

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
            result['buy_price_eur'] = float(
                (Decimal(str(cp.buy_price_hs)) * has_altin_buy_tl).quantize(Decimal('0.01'))
            )
        if cp.sale_price_hs is not None:
            labor = Decimal(str(cp.fixed_labor_amount or 0))
            result['sale_price_eur'] = float(
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
            result['buy_price_eur'] = float(
                (buy_hs * has_altin_buy_tl).quantize(Decimal('0.01'))
            )
        if sale_hs > 0:
            result['sale_price_eur'] = float(
                (sale_hs * has_altin_sale_tl).quantize(Decimal('0.01'))
            )

        return result

    # --- Manuel doviz kuru overlay fonksiyonu ---
    # FAZ 21 (2026-04-30): use_manual_currency_rate=True ise döviz ürünlerinin
    # TL fiyatları StoreConfiguration.manual_currency_rates JSONField'ından
    # okunur. Format: { "<product_uuid>": {"buy_tl": "45.20", "sell_tl": "46.50"} }
    # Has hesaplamasından bağımsız çalışır; chamber modu değilse devreye girer.
    def apply_manual_currency_overlay(item):
        if not use_manual_currency or not manual_currency_rates_map:
            return item
        item_id = item.get('id')
        if not item_id:
            return item
        rate = manual_currency_rates_map.get(str(item_id))
        if not isinstance(rate, dict):
            return item

        try:
            buy_raw = rate.get('buy_tl')
            sell_raw = rate.get('sell_tl')
            buy_tl = Decimal(str(buy_raw)) if buy_raw not in (None, '') else None
            sell_tl = Decimal(str(sell_raw)) if sell_raw not in (None, '') else None
        except (ValueError, TypeError):
            return item

        if (buy_tl is None or buy_tl <= 0) and (sell_tl is None or sell_tl <= 0):
            return item

        result = item.copy()
        if buy_tl is not None and buy_tl > 0:
            result['buy_price_eur'] = float(buy_tl.quantize(Decimal('0.01')))
        if sell_tl is not None and sell_tl > 0:
            result['sale_price_eur'] = float(sell_tl.quantize(Decimal('0.01')))
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
            # FAZ 20 (2026-04-30): hidden_items kontrolü — kullanıcı bu ürünü
            # Live Board'da gizlemek istiyorsa atla. DB adı üzerinden eşleştir.
            if name in hidden_set:
                continue

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
                # Orijinal DB adını sakla — hidden_items / lb_item_order eşleştirmesi
                # display_name dönüşümünden sonra da çalışsın.
                new_item['_db_name'] = item['name']

                # JS tarafinda find() eslesmesi icin name alanini da donustur
                if item['name'] == 'Has Altın 24 Ayar':
                    new_item['name'] = 'Has Altin'
                elif item['name'] == 'Ons':
                    new_item['name'] = 'ONS'

                result.append(new_item)

        # FAZ 20 (2026-04-30): Sıralama önceliği:
        # 1. Live Board'a özel sıra (lb_item_order) varsa onu kullan
        # 2. Yoksa Products.display_order'a fallback
        # 3. Tie-breaker: display_name (alfabetik)
        def _sort_key(x):
            db_name = x.get('_db_name') or x.get('name') or ''
            lb_ord = lb_item_order.get(db_name)
            if lb_ord is not None:
                primary = (0, int(lb_ord))  # lb_order varsa 0-grup'a düş
            else:
                primary = (1, int(x.get('display_order') or 0))  # 1-grup
            return (primary, x.get('display_name') or '')

        result.sort(key=_sort_key)
        return result

    def filter_currency():
        result = []
        for db_name, display_name in currency_map.items():
            # FAZ 20 (2026-04-30): hidden_items kontrolü — DB code üzerinden
            # eşleştir (örn. "EURTRY"). Display ad ("EUR") değil DB code kullanılır
            # çünkü Board Settings UI'ı DB code'larını gönderir (tutarlılık).
            if db_name in hidden_set:
                continue

            item = next((p for p in product_list if p['name'] == db_name), None)
            if item:
                # FAZ 21: önce chamber/manual_has overlay (varsa), sonra
                # manuel kur overlay. Chamber modunda manuel kur uygulanmaz;
                # API/manuel-has modunda kullanıcı manuel kur açtıysa devreye
                # girer (chamber dışı modlarda use_manual_currency varsa).
                item = apply_price_overlay(item)
                if active_mode != 'chamber':
                    item = apply_manual_currency_overlay(item)
                new_item = item.copy()
                new_item['display_name'] = display_name
                new_item['_db_name'] = db_name
                new_item['name'] = display_name
                result.append(new_item)

        # FAZ 20 (2026-04-30): Döviz panelinde de aynı sıralama önceliği.
        def _sort_key_cur(x):
            db_name = x.get('_db_name') or ''
            lb_ord = lb_item_order.get(db_name)
            if lb_ord is not None:
                primary = (0, int(lb_ord))
            else:
                primary = (1, int(x.get('display_order') or 0))
            return (primary, x.get('display_name') or '')

        result.sort(key=_sort_key_cur)
        return result

    # FAZ 21 (2026-04-30): Mağazaya özel ürünler (Reşat Altın vb.) "Diğer /
    # Pariteler" paneli içinde, hardcoded paritelerden sonra render edilir.
    # filter_and_sort tek liste alıp lb_item_order önceliğini global uygular,
    # böylece kullanıcı Board Settings'te custom ürünü pariteler üstüne taşırsa
    # da sıralama doğru olur.
    data = {
        'gold': filter_and_sort(gold_targets),
        'coins': filter_and_sort(coin_targets),
        'currency': filter_currency(),
        'others': filter_and_sort(other_targets + custom_targets),
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
            # FAZ 20 (2026-04-30): Ürün görünürlük & sıralama
            'hidden_items': lb_settings.hidden_items or [],
            'live_board_item_order': lb_settings.live_board_item_order or {},
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

        # FAZ 20 (2026-04-30): hidden_items ve live_board_item_order multipart
        # request'te JSON-encoded string olarak gelir; parse et + tip kontrolü.
        if 'hidden_items' in request.POST:
            try:
                raw_hidden = json.loads(request.POST.get('hidden_items') or '[]')
                if isinstance(raw_hidden, list):
                    cleaned = list({str(x).strip() for x in raw_hidden if x and str(x).strip()})
                    lb_settings.hidden_items = cleaned
            except (json.JSONDecodeError, ValueError):
                logger.debug("hidden_items JSON parse hatasi (multipart)")

        if 'live_board_item_order' in request.POST:
            try:
                raw_order = json.loads(request.POST.get('live_board_item_order') or '{}')
                if isinstance(raw_order, dict):
                    cleaned_order = {}
                    for k, v in raw_order.items():
                        try:
                            cleaned_order[str(k).strip()] = int(v)
                        except (TypeError, ValueError):
                            continue
                    lb_settings.live_board_item_order = cleaned_order
            except (json.JSONDecodeError, ValueError):
                logger.debug("live_board_item_order JSON parse hatasi (multipart)")
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

        # FAZ 20 (2026-04-30): Ürün görünürlük & sıralama
        # hidden_items: list[str] formatında. Tip kontrolü yap; aksi takdirde
        # default boş listeye düş — DB tipini bozma.
        if 'hidden_items' in body:
            raw_hidden = body['hidden_items']
            if isinstance(raw_hidden, list):
                # Sadece string elemanları kabul et, set ile dedupe
                cleaned = list({str(x).strip() for x in raw_hidden if x and str(x).strip()})
                lb_settings.hidden_items = cleaned
            else:
                lb_settings.hidden_items = []

        # live_board_item_order: dict[str, int] formatında. Geçersiz tipleri ele.
        if 'live_board_item_order' in body:
            raw_order = body['live_board_item_order']
            if isinstance(raw_order, dict):
                cleaned_order = {}
                for k, v in raw_order.items():
                    try:
                        cleaned_order[str(k).strip()] = int(v)
                    except (TypeError, ValueError):
                        continue
                lb_settings.live_board_item_order = cleaned_order
            else:
                lb_settings.live_board_item_order = {}

    lb_settings.save()

    # FAZ 20 (2026-04-30): Cache invalidation. Ayar değişince Live Board'un
    # 4 saniyelik Redis TTL'i nedeniyle eski veri görünür. Bu mağazaya ait
    # cache key'lerini sil — kullanıcı kaydet'ten sonra anında yansır.
    try:
        cache.delete(f'live_data:{current_store.id}')
        cache.delete('live_data:global')
    except Exception as cache_err:
        logger.debug(f"Cache invalidation hatasi (kritik degil): {cache_err}")

    logger.info(
        f"Canli ekran gorsel ayarlari guncellendi: user={request.user}, store={current_store}"
    )

    return JsonResponse({
        'status': 'success',
        'message': 'Ayarlar basariyla kaydedildi.',
    })
