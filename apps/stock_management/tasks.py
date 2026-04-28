"""
Celery Tasks - Stok Yonetimi
=============================

1. fetch_prices_from_providers : Tum aktif API saglayicilarindan fiyat ceker
2. daily_stock_integrity_check : StockSnapshot ile StockLedger SUM tutarliligini dogrular
3. cleanup_old_price_quotes    : 30 gunden eski PriceQuote kayitlarini temizler
4. warmup_price_cache          : Redis cache'i DB'den doldurur
"""

import logging
from decimal import Decimal

import requests
from celery import shared_task
from django.conf import settings
from django.db.models import Case, DecimalField, F, Sum, When
from django.utils import timezone

logger = logging.getLogger('stock_management')


# ============================================================================
# TASK 1: Fiyat Cekme
# ============================================================================

@shared_task(
    name='stock_management.fetch_prices_from_providers',
    ignore_result=True,
    soft_time_limit=120,
    time_limit=180,
)
def fetch_prices_from_providers():
    """
    Tum aktif PriceProvider'lardan fiyat ceker.

    Islem akisi:
        1. Aktif ve ACTIVE/MAINTENANCE durumundaki saglayicilari al
        2. Oncelik sirasina gore her birini sorgula
        3. Has Altin fiyatini baz al (diger fiyatlarin Has cinsini hesaplamak icin)
        4. API cevabini PriceService.process_api_response() ile isle
        5. Hata olursa saglayicinin hata sayacini artir
    """
    from apps.stock_management.models import PriceProvider
    from apps.stock_management.services.price_service import PriceService

    providers = PriceProvider.objects.filter(
        is_active=True,
        status__in=[
            PriceProvider.ProviderStatus.ACTIVE,
            PriceProvider.ProviderStatus.MAINTENANCE,
        ],
    ).order_by('priority')

    if not providers.exists():
        logger.warning("Aktif fiyat saglayici bulunamadi.")
        return "Aktif saglayici yok."

    total_processed = 0
    total_errors = 0

    # Oncelikle Has Altin bazini al (ilk basarili saglayicidan)
    base_has_buy_tl = None
    base_has_sell_tl = None

    for provider in providers:
        try:
            # API cagrisi
            response_data = _call_provider_api(provider)

            if response_data is None:
                provider.mark_error("API cevabi bos veya gecersiz")
                total_errors += 1
                continue

            # Has Altin bazini bul (ilk saglayicidan)
            if base_has_buy_tl is None:
                has_data = _extract_has_altin(response_data)
                if has_data:
                    base_has_buy_tl = has_data['buy']
                    base_has_sell_tl = has_data['sell']

            # Cevabi isle
            count = PriceService.process_api_response(
                provider=provider,
                response_data=response_data,
                base_has_buy_tl=base_has_buy_tl,
                base_has_sell_tl=base_has_sell_tl,
            )

            total_processed += count
            logger.info(
                f"Fiyat guncellendi: provider={provider.name}, count={count}"
            )

        except requests.Timeout:
            provider.mark_error(f"Timeout ({provider.timeout_seconds}s)")
            total_errors += 1
            logger.error(f"API timeout: provider={provider.name}")

        except requests.RequestException as e:
            provider.mark_error(str(e)[:500])
            total_errors += 1
            logger.error(f"API hatasi: provider={provider.name}, err={e}")

        except Exception as e:
            provider.mark_error(str(e)[:500])
            total_errors += 1
            logger.exception(f"Beklenmeyen hata: provider={provider.name}")

    # Eski sistemle uyumluluk: StorePriceCache'i de guncelle
    try:
        from apps.stores.services import update_store_has_cache_for_all_stores
        update_store_has_cache_for_all_stores()
    except Exception as e:
        logger.warning(f"StorePriceCache guncelleme hatasi (kritik degil): {e}")

    # API fiyatlarini Products tablosuna senkronize et
    if total_processed > 0:
        try:
            _sync_api_prices_to_products()
        except Exception as e:
            logger.error(f"Products senkronizasyon hatasi: {e}")

    result_msg = (
        f"Fiyat cekme tamamlandi: {total_processed} fiyat islendi, "
        f"{total_errors} hata."
    )
    logger.info(result_msg)
    return result_msg


def _call_provider_api(provider) -> list:
    """
    Saglayicinin API'sini cagir ve cevabi dondur.

    Mevcut RapidAPI (HaremAltin) yapisini destekler,
    yeni saglayicilar icin genisletilebilir.
    """
    if not provider.base_url:
        # base_url yoksa eski settings-based yapiyi kullan
        api_key_name = provider.api_key_setting or 'RAPIDAPI_KEY'
        api_key = getattr(settings, api_key_name, '')

        if not api_key:
            raise ValueError(f"API key bulunamadi: settings.{api_key_name}")

        # Varsayilan HaremAltin RapidAPI yapisi
        host = provider.extra_config.get(
            'host',
            getattr(settings, 'RAPIDAPI_HOST', '')
        )
        url = f"https://{host}/economy/live-exchange-rates"

        headers = {
            'x-rapidapi-key': api_key,
            'x-rapidapi-host': host,
        }
    else:
        url = provider.base_url
        headers = {}

        # API key varsa header'a ekle
        if provider.api_key_setting:
            api_key = getattr(settings, provider.api_key_setting, '')
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'

    # Ek header'lar
    if provider.extra_headers:
        headers.update(provider.extra_headers)

    response = requests.get(
        url,
        headers=headers,
        timeout=provider.timeout_seconds or 10,
    )
    response.raise_for_status()

    data = response.json()

    # RapidAPI formatinda 'data' alani varsa
    if isinstance(data, dict):
        if 'data' in data:
            return data['data']
        elif 'results' in data:
            return data['results']
        elif 'items' in data:
            return data['items']
        else:
            return [data]

    return data if isinstance(data, list) else [data]


def _extract_has_altin(response_data: list) -> dict:
    """Response'dan Has Altin bazini bul."""
    has_codes = ['ALTINTRY', 'HAS_ALTIN', 'XAU_TRY', 'GOLD_24K']

    for item in response_data:
        if not isinstance(item, dict):
            continue
        # FAZ 21 FIX: API artık 'code' field'ı gönderiyor (eski: 'currencyCode')
        code = str(item.get('code') or item.get('currencyCode') or '').upper()
        if code in has_codes:
            try:
                return {
                    'buy': Decimal(str(item.get('buy', 0))),
                    'sell': Decimal(str(item.get('sell', 0))),
                }
            except (ValueError, TypeError):
                continue

    return None


# ============================================================================
# TASK 2: Gunluk Stok Tutarlilik Kontrolu
# ============================================================================

@shared_task(
    name='stock_management.daily_stock_integrity_check',
    ignore_result=False,
    soft_time_limit=300,
    time_limit=600,
)
def daily_stock_integrity_check():
    """
    StockSnapshot.stock_gram ile StockLedger'in kumulatif SUM'u
    arasindaki farki kontrol eder.

    Her gece 00:05'te calistirilmasi onerilir.
    Fark tespit edilirse loglama yapar ve opsiyonel bildirim gonderir.
    """
    from apps.stock_management.models import StockSnapshot, StockLedger

    discrepancies = []
    checked = 0

    # YOL 2 (SSOT): is_currency=True ürünler artık Payment SSOT üzerinden takip edilir.
    # StockSnapshot kayıtları döviz için ölü veridir; integrity check'ten exclude edilir,
    # aksi halde sürekli false-positive CRITICAL log üretirdi.
    for snap in (
        StockSnapshot.objects
        .select_related('product', 'store')
        .exclude(product__is_currency=True)
        .iterator(chunk_size=500)
    ):
        # Ledger'dan hesaplanmis gercek stok
        agg = StockLedger.objects.filter(
            product=snap.product,
            store=snap.store,
        ).aggregate(
            net_gram=Sum(
                Case(
                    When(direction='IN', then=F('quantity_gram')),
                    When(direction='OUT', then=-F('quantity_gram')),
                    default=Decimal('0'),
                    output_field=DecimalField(),
                )
            )
        )

        ledger_gram = agg['net_gram'] or Decimal('0')
        snapshot_gram = snap.stock_gram or Decimal('0')
        diff = abs(snapshot_gram - ledger_gram)

        if diff > Decimal('0.001'):  # 1 miligramdan fazla sapma
            discrepancy = {
                'product_id': str(snap.product_id),
                'product_name': snap.product.name,
                'store': str(snap.store),
                'snapshot_gram': float(snapshot_gram),
                'ledger_gram': float(ledger_gram),
                'diff_gram': float(diff),
            }
            discrepancies.append(discrepancy)
            logger.critical(
                f"STOK TUTARSIZLIGI: {discrepancy}"
            )

        checked += 1

    if discrepancies:
        logger.critical(
            f"STOK TUTARLILIK KONTROLU BASARISIZ: "
            f"{len(discrepancies)} / {checked} kayitta uyumsuzluk tespit edildi."
        )
    else:
        logger.info(
            f"Stok tutarlilik kontrolu basarili: {checked} kayit dogrulandi."
        )

    return {
        'checked': checked,
        'discrepancies_count': len(discrepancies),
        'discrepancies': discrepancies[:50],  # Ilk 50 kayit
    }


# ============================================================================
# TASK 3: Eski Fiyat Kayitlarini Temizle
# ============================================================================

@shared_task(
    name='stock_management.cleanup_old_price_quotes',
    ignore_result=True,
    soft_time_limit=120,
    time_limit=180,
)
def cleanup_old_price_quotes(days: int = 30):
    """
    Belirtilen gun sayisindan eski PriceQuote kayitlarini siler.

    Her hafta Pazar 03:00'da calistirilmasi onerilir.
    """
    from apps.stock_management.models import PriceQuote

    cutoff = timezone.now() - timezone.timedelta(days=days)

    deleted_count, _ = PriceQuote.objects.filter(
        created_on__lt=cutoff,
    ).delete()

    logger.info(
        f"Eski fiyat kayitlari temizlendi: "
        f"{deleted_count} kayit silindi (>{days} gun)"
    )

    return f"{deleted_count} kayit silindi"


# ============================================================================
# TASK 4: Cache Warmup
# ============================================================================

@shared_task(
    name='stock_management.warmup_price_cache',
    ignore_result=True,
    soft_time_limit=60,
    time_limit=120,
)
def warmup_price_cache():
    """
    Redis cache'i DB'den doldurur.
    Sunucu restart'i veya Redis flush sonrasi calistirilir.
    """
    from apps.stock_management.services.price_service import PriceService

    loaded = PriceService.warmup_cache()
    return f"{loaded} fiyat cache'e yuklendi"


# ============================================================================
# TASK 5: API Fiyatlarini Products Tablosuna Senkronize Et
# ============================================================================

# PriceQuote MetalType -> Products tablo name eslestirmesi
_METAL_TO_PRODUCT_MAP = {
    'GOLD_24K': 'Has Altın',
    'GOLD_22K': '22 Ayar Gram',
    'SILVER': 'Gümüş',
    'USD': 'USDTRY',
    'EUR': 'EURTRY',
    'GBP': 'GBPTRY',
}


def _sync_api_prices_to_products():
    """
    PriceService'ten (Redis/DB) guncel fiyatlari okuyup
    Products tablosundaki ilgili urunlerin buy_price_tl ve sale_price_tl
    alanlarini gunceller.

    Bu fonksiyon fetch_prices_from_providers() task'i icerisinden,
    basarili fiyat cekimi sonrasinda cagirilir.

    Kurallar:
        - Sadece use_manual_has_calculation=False olan magazalarin
          urunlerini etkiler. (Simdilik Products global oldugundan
          en az bir magaza API modunda ise guncellenir.)
        - Products.objects.filter(...).update() kullanilir (save() degil).
        - Sadece buy_price_tl ve sale_price_tl alanlari guncellenir.
        - 0 veya negatif deger gelirse o urun atlanir (koruma).
        - Hata durumunda son basarili deger Products'ta kalir (sessiz failover).
    """
    from apps.stock_management.services.price_service import PriceService
    from apps.products.models import Products
    from apps.settings.models import StoreConfiguration
    from django.core.cache import cache

    # En az bir magaza API modunda mi kontrol et
    manual_only = StoreConfiguration.objects.filter(
        use_manual_has_calculation=True
    ).exists()

    all_stores_manual = False
    if manual_only:
        total_stores = StoreConfiguration.objects.count()
        manual_stores = StoreConfiguration.objects.filter(
            use_manual_has_calculation=True
        ).count()
        if total_stores > 0 and manual_stores >= total_stores:
            all_stores_manual = True

    if all_stores_manual:
        logger.info("Tum magazalar manuel modda — Products senkronizasyonu atlanıyor.")
        return

    updated = 0

    for metal_type, product_name in _METAL_TO_PRODUCT_MAP.items():
        try:
            price = PriceService.get_price(metal_type)

            buy_tl = price.get('buy_tl', Decimal('0'))
            sell_tl = price.get('sell_tl', Decimal('0'))

            if buy_tl <= 0 and sell_tl <= 0:
                continue

            update_fields = {}
            if buy_tl > 0:
                update_fields['buy_price_tl'] = buy_tl
            if sell_tl > 0:
                update_fields['sale_price_tl'] = sell_tl

            if update_fields:
                rows = Products.objects.filter(
                    name=product_name,
                    is_deleted=False,
                ).update(**update_fields)
                if rows > 0:
                    updated += rows
        except Exception as e:
            logger.warning(
                f"Products sync hatasi: metal={metal_type}, "
                f"product={product_name}, err={e}"
            )

    # Gram Altin = Has Altin ile ayni fiyat (geleneksel kuyumculuk)
    try:
        has_price = PriceService.get_price('GOLD_24K')
        has_buy = has_price.get('buy_tl', Decimal('0'))
        has_sell = has_price.get('sell_tl', Decimal('0'))

        if has_buy > 0 and has_sell > 0:
            gram_fields = {}
            if has_buy > 0:
                gram_fields['buy_price_tl'] = has_buy
            if has_sell > 0:
                gram_fields['sale_price_tl'] = has_sell

            rows = Products.objects.filter(
                name='Gram Altın',
                is_deleted=False,
            ).update(**gram_fields)
            if rows > 0:
                updated += rows
    except Exception as e:
        logger.warning(f"Gram Altin sync hatasi: {e}")

    # Ons fiyati (PriceQuote'ta ons mapping varsa)
    try:
        from apps.stock_management.models import PriceQuote
        ons_quote = PriceQuote.objects.filter(
            currency_code__in=['ONS', 'XAU_USD', 'ONSTRY', 'ONS_TRY', 'XAUTRY'],
            provider__is_active=True,
        ).order_by('-quoted_at').first()

        if ons_quote and (ons_quote.buy_price_tl > 0 or ons_quote.sell_price_tl > 0):
            ons_fields = {}
            if ons_quote.buy_price_tl > 0:
                ons_fields['buy_price_tl'] = ons_quote.buy_price_tl
            if ons_quote.sell_price_tl > 0:
                ons_fields['sale_price_tl'] = ons_quote.sell_price_tl

            if ons_fields:
                rows = Products.objects.filter(
                    name='Ons',
                    is_deleted=False,
                ).update(**ons_fields)
                if rows > 0:
                    updated += rows
    except Exception as e:
        logger.warning(f"Ons sync hatasi: {e}")

    # Son senkronizasyon zamanini cache'e yaz
    cache.set('kuyumplus:last_price_sync', timezone.now().isoformat(), timeout=3600)

    if updated > 0:
        logger.info(f"Products tablosu senkronize edildi: {updated} urun guncellendi.")
