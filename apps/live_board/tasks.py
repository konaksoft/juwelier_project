"""
Kitco Canlı Piyasa Celery Task'ları
====================================

Bu modül dakikada bir çalışan ana fetch task'ını barındırır.

İZOLASYON KURALI (KRİTİK)
-------------------------
Veritabanı yazımı YALNIZCA `KitcoPriceCache` tablosunadır.

Hiçbir koşulda aşağıdaki tablolara YAZIM YAPILMAZ:
    * Products
    * ChamberProductPrice
    * StockSnapshot
    * Rates  (yalnızca OKUMA — USD→{EUR, GBP, CAD, AUD, JPY, CHF} kurları için)
    * StoreConfiguration

Çoklu Para Birimi Açılımı
-------------------------
Kitco ham yanıtı USD bazlı gelir. Diğer para birimleri Rates
tablosundan SALT-OKUMA ile türetilir:

    EUR fiyat = USD fiyat * (USD→EUR oranı)
    GBP fiyat = USD fiyat * (USD→GBP oranı)
    CAD fiyat = USD fiyat * (USD→CAD oranı)
    ... (AUD, JPY, CHF)

Rate yoksa ilgili para birimi kaydı üretilmez (sessizce atlanır);
USD ve mevcut diğer para birimleri yazılmaya devam eder.

Hata yönetimi: Task Celery worker'ını çökertmez. Her istisna
loglanıp sessizce geçilir; beat schedule bir sonraki turda
tekrar dener.
"""

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from celery import shared_task
from django.db import transaction

from apps.live_board.models import KitcoPriceCache
from apps.live_board.services.kitco_fetcher import (
    KitcoFetchError,
    extract_next_data,
    fetch_kitco_html,
    normalize_record,
    parse_price_records,
)

logger = logging.getLogger('live_board.kitco')


# USD'den türetilecek para birimleri listesi.
# USD zaten doğrudan Kitco'dan gelir, burada listelenmez.
DERIVED_CURRENCIES = ('EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF')


# -----------------------------------------------------------------------------
# Yardımcılar (Yalnızca Okuma)
# -----------------------------------------------------------------------------


def _read_usd_to_currency_rate(target_code: str) -> Optional[Decimal]:
    """
    `definitions.rates.Rates` tablosundan USD→<target_code> dönüşüm
    oranını OKUR.

    İzolasyon kuralı gereği bu fonksiyon:
      * Rates tablosuna YAZMAZ
      * Rates tablosunu GÜNCELLEMEZ
      * Rates tablosundan SİLMEZ
      * Yalnızca mevcut kayıtları okur

    Arama sırası:
      1. currency_one=USD, currency_two=<target>  → sale_price
      2. currency_one=<target>, currency_two=USD  → 1 / sale_price

    Hiçbiri bulunamazsa None döner. Bu durumda ilgili para birimi
    kaydı üretilmez (kısmi sonuç kabul edilir).
    """
    code = (target_code or '').strip().upper()
    if not code or code == 'USD':
        return None

    try:
        # Lokal import — circular import ihtimaline karşı
        from apps.definitions.rates.models import Rates
    except Exception as exc:
        logger.warning(
            "[Kitco Task] Rates modeli import edilemedi (%s): %s",
            code, exc,
        )
        return None

    try:
        forward = (
            Rates.objects
            .filter(
                currency_one__code__iexact='USD',
                currency_two__code__iexact=code,
                is_active=True,
                is_deleted=False,
            )
            .order_by('-modified_on')
            .only('sale_price')
            .first()
        )
        if forward and forward.sale_price and forward.sale_price > 0:
            return Decimal(str(forward.sale_price))

        reverse = (
            Rates.objects
            .filter(
                currency_one__code__iexact=code,
                currency_two__code__iexact='USD',
                is_active=True,
                is_deleted=False,
            )
            .order_by('-modified_on')
            .only('sale_price')
            .first()
        )
        if reverse and reverse.sale_price and reverse.sale_price > 0:
            return Decimal('1') / Decimal(str(reverse.sale_price))
    except Exception as exc:
        logger.warning(
            "[Kitco Task] Rates okuma hatası (%s): %s", code, exc
        )

    return None


def _extract_source_timestamp(
    raw_candidates: List[Dict[str, Any]],
) -> Optional[datetime]:
    """
    Kitco ham kayıtlarından `originalTime` (ISO 8601) alanını alır
    ve timezone-aware datetime döner. Parse edilemezse None.
    """
    if not raw_candidates:
        return None
    try:
        first = raw_candidates[0]
        keys_lower = {k.lower(): k for k in first.keys()}
        orig_key = keys_lower.get('originaltime')
        if not orig_key:
            return None
        raw = first.get(orig_key)
        if not raw:
            return None
        iso = str(raw).strip().replace('Z', '+00:00')
        return datetime.fromisoformat(iso)
    except Exception as exc:
        logger.debug("[Kitco Task] source_timestamp parse başarısız: %s", exc)
        return None


def _safe_convert(
    value_usd: Decimal,
    rate_usd_to_target: Decimal,
) -> Optional[Decimal]:
    """USD * (USD→target oranı) = target. Hata olursa None."""
    try:
        return (value_usd * rate_usd_to_target).quantize(Decimal('0.0001'))
    except (InvalidOperation, TypeError, ValueError):
        return None


# -----------------------------------------------------------------------------
# Ana Task — Celery Beat her 60 saniyede bir çağırır
# -----------------------------------------------------------------------------


@shared_task(name='live_board.fetch_kitco_live_rates')
def fetch_kitco_live_rates() -> Dict[str, Any]:
    """
    Kitco'dan USD spot fiyatları çeker, Rates tablosundan salt-okuma
    kurları kullanarak EUR/GBP/CAD/AUD/JPY/CHF türetir ve
    `KitcoPriceCache`'e yazar.

    Dönüş değeri: Celery sonuç deposunda görünecek özet sözlük.
    (Task'ın kendisi hiçbir hata fırlatmaz; her şey yakalanır ve
    loglanır.)
    """
    # Teşhis amaçlı giriş logu.
    # Beat bu task'ı tetikledi mi? sorusu bu INFO satırıyla anında
    # log dosyasından doğrulanabilir.
    logger.info("[Kitco Task] fetch_kitco_live_rates tetiklendi.")

    # 1) Kitco sayfasını çek ve ham aday kayıtları çıkar
    try:
        html = fetch_kitco_html()
        next_data = extract_next_data(html)
        raw_candidates = parse_price_records(next_data)
        logger.info(
            "[Kitco Task] HTML boyutu=%d, aday kayıt sayısı=%d",
            len(html) if html else 0,
            len(raw_candidates),
        )
    except KitcoFetchError as exc:
        logger.warning("[Kitco Task] Fetch hatası: %s", exc)
        return {'status': 'fetch_error', 'message': str(exc)}
    except Exception as exc:
        logger.exception("[Kitco Task] Beklenmedik fetch hatası: %s", exc)
        return {'status': 'unexpected_error', 'message': str(exc)}

    # 2) Normalize (pozisyonel fallback etkin)
    total = len(raw_candidates)
    normalized: List[Dict[str, Any]] = []
    seen: set = set()
    for idx, raw in enumerate(raw_candidates):
        rec = normalize_record(raw, position=idx, total=total)
        if rec is None:
            continue
        key = (rec['metal'], rec['currency'], rec['unit'])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(rec)

    if not normalized:
        logger.warning("[Kitco Task] Normalize edilmiş kayıt yok")
        return {'status': 'no_records'}

    # 3) Kurları ve kaynak zamanı oku (USD hariç tümü türetilecek)
    rate_map: Dict[str, Optional[Decimal]] = {}
    for code in DERIVED_CURRENCIES:
        rate_map[code] = _read_usd_to_currency_rate(code)

    source_ts = _extract_source_timestamp(raw_candidates)

    # 4) KitcoPriceCache'e yaz (USD zorunlu, türetilenler koşullu)
    written_counts: Dict[str, int] = {'USD': 0}
    for code in DERIVED_CURRENCIES:
        written_counts[code] = 0

    for rec in normalized:
        metal = rec['metal']
        unit = rec['unit']
        bid_usd: Decimal = rec['bid']
        ask_usd: Decimal = rec['ask']

        # 4a) USD kaydı — her zaman yazılır
        try:
            with transaction.atomic():
                KitcoPriceCache.objects.update_or_create(
                    metal_type=metal,
                    currency=KitcoPriceCache.Currency.USD,
                    unit=unit,
                    defaults={
                        'bid_price': bid_usd,
                        'ask_price': ask_usd,
                        'source_timestamp': source_ts,
                    },
                )
                written_counts['USD'] += 1
        except Exception as exc:
            logger.warning(
                "[Kitco Task] USD yazım hatası (%s/%s): %s",
                metal, unit, exc,
            )

        # 4b) Türetilen para birimleri (kur varsa)
        for code in DERIVED_CURRENCIES:
            rate = rate_map.get(code)
            if rate is None or rate <= 0:
                continue

            conv_bid = _safe_convert(bid_usd, rate)
            conv_ask = _safe_convert(ask_usd, rate)
            if conv_bid is None or conv_ask is None:
                continue

            try:
                with transaction.atomic():
                    KitcoPriceCache.objects.update_or_create(
                        metal_type=metal,
                        currency=code,
                        unit=unit,
                        defaults={
                            'bid_price': conv_bid,
                            'ask_price': conv_ask,
                            'source_timestamp': source_ts,
                        },
                    )
                    written_counts[code] += 1
            except Exception as exc:
                logger.warning(
                    "[Kitco Task] %s yazım hatası (%s/%s): %s",
                    code, metal, unit, exc,
                )

    logger.info(
        "[Kitco Task] OK — yazım: %s, kurlar: %s, kaynak: %s",
        written_counts,
        {k: (str(v) if v is not None else None) for k, v in rate_map.items()},
        source_ts,
    )
    return {
        'status': 'ok',
        'written': written_counts,
        'rates': {
            k: (str(v) if v is not None else None)
            for k, v in rate_map.items()
        },
        'source_timestamp': source_ts.isoformat() if source_ts else None,
    }
