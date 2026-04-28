"""
FAZ 21: Ürün Fiyat Güncelleme Celery Task

Harem Altın API'sinden canlı döviz ve altın kurlarını çeker,
Products tablosundaki alış/satış fiyatlarını günceller.

API Response Format (gerçek):
    {
        "code": "QARTRY",           ← ESKİ: "currencyCode"
        "baseCurrency": "QAR",      ← ESKİ: "baseCurrencyCode"
        "targetCurrency": "TRY",    ← ESKİ: "targetCurrencyCode"
        "FullName": "Katar Riyali-Türk Lirası",
        "buy": "10.52",             ← bazen null veya eksik olabilir
        "sell": "10.58",            ← bazen null veya eksik olabilir
        "changeRate": "0.12"        ← bazen null veya eksik olabilir
    }

FAZ 21 FIX: Döviz ürünleri (CURRENCY_CODE_MAP) için buy_price_hs/sale_price_hs
YAZILMAZ. Bu alanlar altın ağırlık taban hesabına aittir; dövizde anlamsızdır
ve altın fiyatı değiştikçe stale kalarak yanlış görüntüye neden olur.
Dövizlerde yalnızca buy_price_tl / sale_price_tl güncellenir.
"""
import logging
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Model alanlarının ondalık hassasiyetleri (apps/products/models.py)
#   buy_price_tl / sale_price_tl → decimal_places=2
#   buy_price_hs / sale_price_hs → decimal_places=3
#   profit                       → decimal_places=3
# API "44.7234" gibi 2'den fazla ondalık döndürdüğünde model full_clean()
# ValidationError fırlatıyor. Atama öncesi quantize zorunlu.
_TL_QUANT = Decimal('0.01')
_HS_QUANT = Decimal('0.001')
_RATE_QUANT = Decimal('0.001')


def _quant(value, q):
    """Decimal'i hedef ondalık hassasiyetine yuvarlar."""
    try:
        return value.quantize(q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError):
        return Decimal('0').quantize(q)

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.products.models import Products
from apps.stores.services import update_store_has_cache_for_all_stores

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# FAZ 21: SPECIAL_NAME_MAP — API code/baseCurrency → DB ürün adı eşleştirmesi
# API'deki isim ile veritabanındaki ürün adı farklı olduğunda bu harita kullanılır.
# ─────────────────────────────────────────────────────────────
SPECIAL_NAME_MAP = {
    # Altın ürünleri (baseCurrency → DB adı)
    'TEKALTINESKI': 'Eski Tam',
    'Yeni Tam': 'Yeni Tam',
    'Yeni Çeyrek': 'Yeni Çeyrek',
    'Eski Çeyrek': 'Eski Çeyrek',
    'Yeni Yarım': 'Yeni Yarım',
    'Eski Yarım': 'Eski Yarım',
    'Yeni Ata': 'Yeni Ata',
    'Eski Ata': 'Eski Ata',
    'Yeni Ata5': 'Yeni 5li Ata',
    'Eski Ata5': 'Eski 5li Ata',
    'Yeni Gremse': 'Yeni Gremse',
    'Eski Gremse': 'Eski Gremse',
    'Gram Altın': 'Gram Altın',
    'Has Altın': 'Has Altın 24 Ayar',
    '14 Ayar': '14 Ayar',
    '22 Ayar': '22 Ayar',
    'Gümüş TL': 'Gümüş TL',
}

# FAZ 21: Döviz code → DB ürün adı eşleştirmesi
# API code alanı ile DB'deki ürün name alanı farklıysa burada tanımlanır.
# FAZ 21 FIX: Bu haritadaki ürünler için buy_price_hs/sale_price_hs güncellenmez,
# is_currency=True set edilir.
CURRENCY_CODE_MAP = {
    'USDTRY': 'USDTRY',
    'EURTRY': 'EURTRY',
    'GBPTRY': 'GBPTRY',
    'CADTRY': 'CADTRY',
    'QARTRY': 'QARTRY',
    'CHFTRY': 'CHFTRY',
    'JPYTRY': 'JPYTRY',
    'SARTRY': 'SARTRY',
    'AEDTRY': 'AEDTRY',
    'AUDTRY': 'AUDTRY',
    'KWDTRY': 'KWDTRY',
    'OMRTRY': 'OMRTRY',
    'RUBTRY': 'RUBTRY',
    'BGNTRY': 'BGNTRY',
    'NOKTRY': 'NOKTRY',
    'SEKTRY': 'SEKTRY',
    'DKKTRY': 'DKKTRY',
    'CNYTRY': 'CNYTRY',
    'ILSTRY': 'ILSTRY',
    'MADTRY': 'MADTRY',
    'JODTRY': 'JODTRY',
}


def _safe_decimal(value, default='0'):
    """
    FAZ 21: Güvenli Decimal dönüşümü.
    None, boş string, geçersiz format durumlarında default döner.
    """
    if value is None:
        return Decimal(default)
    try:
        val = Decimal(str(value).strip())
        return val
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


@shared_task(name='products.update_products_from_api', ignore_result=True, soft_time_limit=120, time_limit=180)
def update_products_from_api():
    """
    FAZ 21: Harem Altın API'sinden canlı fiyatları çeker ve Products tablosunu günceller.

    API field mapping (gerçek → eski kod):
        code           → currencyCode
        baseCurrency   → baseCurrencyCode
        targetCurrency → targetCurrencyCode
        buy            → buy (aynı, ama null olabilir)
        sell           → sell (aynı, ama null olabilir)
        changeRate     → changeRate (aynı, ama null olabilir)

    FAZ 21 FIX: Döviz ürünleri (CURRENCY_CODE_MAP) için:
        - buy_price_hs / sale_price_hs güncellenmez (stale kalırsa has×kur çarpımı yanlış sonuç üretir)
        - is_currency=True set edilir
        - Yalnızca buy_price_tl / sale_price_tl doğrudan API değeriyle güncellenir
    """
    url = f"https://{settings.RAPIDAPI_HOST}/economy/live-exchange-rates"

    if not settings.RAPIDAPI_KEY:
        return "Hata: RAPIDAPI_KEY settings dosyasında bulunamadı."

    headers = {
        'x-rapidapi-key': settings.RAPIDAPI_KEY,
        'x-rapidapi-host': settings.RAPIDAPI_HOST,
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)

        data = response.json()

        if data.get("status") != "success":
            return f"API yanıtı başarısız: {data.get('message', 'Bilinmeyen hata')}"

        api_items = data.get("data", [])
        if not api_items:
            return "API'den veri gelmedi (data boş)."

        # ─── 1. HAS ALTIN BAZ FİYATLARI (yalnızca altın ürünler için HS hesabında kullanılır) ───
        # FAZ 21: API field adı "code" (eski kodda "currencyCode" idi)
        has_item = next(
            (it for it in api_items
             if (it.get("code") or it.get("currencyCode") or "").lower() == "altintry"),
            None
        )
        if not has_item:
            return "Has Altın (altintry) fiyatı API yanıtında bulunamadı!"

        base_has_buy = _safe_decimal(has_item.get("buy"))
        base_has_sale = _safe_decimal(has_item.get("sell"))

        if base_has_buy <= 0 or base_has_sale <= 0:
            return f"Has Altın baz fiyatları geçersiz: buy={base_has_buy}, sell={base_has_sale}"

        updated_count = 0
        skipped_zero = 0
        new_count = 0

        # ─── 2. TÜM ÜRÜNLERİ GÜNCELLE ───
        for item in api_items:
            # FAZ 21: API field adları düzeltildi (code, baseCurrency, targetCurrency)
            currency_code = item.get("code") or item.get("currencyCode") or ""
            base_currency = item.get("baseCurrency") or item.get("baseCurrencyCode") or ""
            target_currency = item.get("targetCurrency") or item.get("targetCurrencyCode") or ""

            # ─── 3. GÜVENLİ FİYAT PARSE ───
            buy_price = _safe_decimal(item.get("buy"))
            sell_price = _safe_decimal(item.get("sell"))
            change_rate = _safe_decimal(item.get("changeRate"))

            # FAZ 21: buy veya sell 0 ise bu ürünü GÜNCELLEME (fiyat sıfırlanmasını önle)
            if buy_price <= 0 and sell_price <= 0:
                skipped_zero += 1
                log.debug("API fiyat=0, atlanıyor: code=%s", currency_code)
                continue

            # ─── 4. ÜRÜN İSMİ BELİRLEME ───
            # Öncelik 1: CURRENCY_CODE_MAP (code → DB adı)
            # Öncelik 2: SPECIAL_NAME_MAP (baseCurrency → DB adı)
            # Öncelik 3: code doğrudan kullan
            name = CURRENCY_CODE_MAP.get(currency_code.upper())
            is_currency_product = name is not None  # FAZ 21 FIX: döviz mi?

            if not name:
                name = SPECIAL_NAME_MAP.get(base_currency)
            if not name:
                # Fallback: target TRY ise code kullan, değilse baseCurrency
                if target_currency == "TRY":
                    name = currency_code
                else:
                    name = base_currency or currency_code

            # FAZ 21 FIX: Döviz ürünleri için buy_price_hs/sale_price_hs hesaplanmaz.
            # Altın/diğer ürünler için baz HS hesaplama (mağaza bağımsız) yapılır.
            if is_currency_product:
                buy_price_hs = None
                sell_price_hs = None
            else:
                buy_price_hs = buy_price / base_has_buy
                sell_price_hs = sell_price / base_has_sale

            # ─── 5. GÜNCELLEME / OLUŞTURMA ───
            # 22 Ayar için iki ürün (22 Ayar + 22 Ayar Gram)
            products_to_update_names = [name]
            if name == "22 Ayar":
                products_to_update_names = ["22 Ayar", "22 Ayar Gram"]

            for target_product_name in products_to_update_names:
                existing_product = Products.objects.filter(
                    name__iexact=target_product_name, is_scrap=False
                ).exclude(category__name="Barkodlu Ürünler").first()

                if existing_product:
                    # FAZ 21 / Hotfix 2026-04-27: Asimetrik API verisi (buy=null, sell>0) durumu.
                    # Harem Altın bazı dövizler için yalnızca sell döndürür. Eski kod
                    # `buy_price_tl`'yi update_fields'a koruyup atlatılan assignment ile
                    # in-memory 0'ı tekrar DB'ye yazıyordu. Artık eksik tarafı diğeriyle doldur.
                    effective_buy = buy_price if buy_price > 0 else sell_price
                    effective_sell = sell_price if sell_price > 0 else buy_price

                    update_fields = ["profit", "description"]

                    if effective_buy > 0:
                        existing_product.buy_price_tl = _quant(effective_buy, _TL_QUANT)
                        update_fields.append("buy_price_tl")
                    if effective_sell > 0:
                        existing_product.sale_price_tl = _quant(effective_sell, _TL_QUANT)
                        update_fields.append("sale_price_tl")

                    # FAZ 21 FIX: Döviz ürünlerinde buy_price_hs/sale_price_hs güncellenmez.
                    # is_currency flag'i güncellenir (daha önce set edilmemişse düzeltilir).
                    if is_currency_product:
                        existing_product.is_currency = True
                        update_fields.append("is_currency")
                    else:
                        # HS hesaplaması için effective değerleri tekrar hesapla
                        if effective_buy > 0:
                            existing_product.buy_price_hs = _quant(effective_buy / base_has_buy, _HS_QUANT)
                            update_fields.append("buy_price_hs")
                        if effective_sell > 0:
                            existing_product.sale_price_hs = _quant(effective_sell / base_has_sale, _HS_QUANT)
                            update_fields.append("sale_price_hs")

                    existing_product.profit = _quant(change_rate, _RATE_QUANT)
                    existing_product.description = (
                        f"{base_currency}-{target_currency} kuru güncellendi. "
                        f"Değişim: {change_rate}%"
                    )
                    existing_product.save(update_fields=update_fields)
                    updated_count += 1
                else:
                    # Yeni ürün oluştur (sadece fiyatı olan ürünler)
                    if buy_price > 0 or sell_price > 0:
                        # Hotfix 2026-04-27: Asimetrik API (buy=null, sell>0) için
                        # eksik tarafı diğeriyle doldur — yoksa USDTRY gibi ürünler
                        # buy_price_tl=0 ile yaratılıp dönüşüm modalını kilitliyor.
                        effective_buy = buy_price if buy_price > 0 else sell_price
                        effective_sell = sell_price if sell_price > 0 else buy_price

                        create_kwargs = dict(
                            id=uuid.uuid4(),
                            name=target_product_name,
                            is_protected=True,
                            buy_price_tl=_quant(effective_buy, _TL_QUANT),
                            sale_price_tl=_quant(effective_sell, _TL_QUANT),
                            profit=_quant(change_rate, _RATE_QUANT),
                            description=(
                                f"{base_currency}-{target_currency} kuru güncellendi. "
                                f"Değişim: {change_rate}%"
                            ),
                            created_on=timezone.now(),
                        )
                        # FAZ 21 FIX: Döviz ürünlerinde HS alanları 0, is_currency=True
                        if is_currency_product:
                            create_kwargs['is_currency'] = True
                            create_kwargs['buy_price_hs'] = Decimal('0.000')
                            create_kwargs['sale_price_hs'] = Decimal('0.000')
                        else:
                            create_kwargs['buy_price_hs'] = _quant(effective_buy / base_has_buy, _HS_QUANT)
                            create_kwargs['sale_price_hs'] = _quant(effective_sell / base_has_sale, _HS_QUANT)

                        Products.objects.create(**create_kwargs)
                        new_count += 1

        # Cache yenileme (fonksiyon None döner, unpack edilmez)
        update_store_has_cache_for_all_stores()

        result_msg = (
            f"Ürünler güncellendi. {updated_count} güncellendi, {new_count} yeni eklendi, "
            f"{skipped_zero} atlandı (fiyat=0)."
        )
        log.info("FAZ21 API_UPDATE: %s", result_msg)
        return result_msg

    except requests.Timeout:
        log.error("FAZ21: API zaman aşımı (timeout)")
        return "Hata: API zaman aşımı (20 saniye)"
    except requests.ConnectionError:
        log.error("FAZ21: API bağlantı hatası")
        return "Hata: API'ye bağlanılamıyor"
    except Exception as e:
        log.exception("FAZ21: Beklenmeyen hata: %s", e)
        return f"Hata: {e}"
