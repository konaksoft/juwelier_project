"""
ECB FX Kur Senkronizasyon Celery Task'ı
========================================

Amaç
----
Avrupa Merkez Bankası'nın günlük referans döviz kurlarını çekip
`Rates` tablosuna (USD bazlı) yazmak. Bu sayede
`apps.live_board.tasks.fetch_kitco_live_rates` task'ı EUR/GBP/CHF/
CAD/AUD/JPY fiyatlarını bu tablodan okuyarak türetebilir.

Veri Akışı
----------
    ECB XML (EUR bazlı)
        → parse_ecb_xml           (EUR bazlı dict)
        → compute_usd_based_rates (USD bazlı dict)
        → Rates.update_or_create  (currency_one=USD, currency_two=<hedef>)

İzolasyon / Güvenlik Kuralları
-----------------------------
1. Task yalnızca `Rates` ve `Currencies` tablolarını OKUR/YAZAR.
2. Currencies'e YAZIM yapılmaz — kayıt yoksa o hedef sessizce atlanır
   (Currencies.created_by FK zorunlu olduğundan otomatik üretim
    tehlikeli; admin/custom UI üzerinden ekleme beklenir).
3. Manuel girilmiş TL / Türkiye kurları gibi `currency_one != USD`
   kayıtlarına DOKUNULMAZ. update_or_create yalnızca
   `currency_one=USD, currency_two=<target>` eşleşmesinde çalışır.
4. Task worker'ı ASLA çökertmez — tüm istisnalar loglanıp yutulur.

Zamanlama
---------
ECB feed'i günde bir kez (CET 16:00 civarı, iş günleri) güncellenir.
Buna rağmen geçici ağ hatalarına karşı saatte bir çalıştırılır;
update_or_create idempotent olduğundan fazladan çağrı yan etki
üretmez.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.definitions.currencies.models import Currencies
from apps.definitions.rates.models import Rates
from apps.definitions.rates.services.ecb_fetcher import (
    ECBFetchError,
    compute_usd_based_rates,
    fetch_ecb_xml,
    parse_ecb_xml,
)

logger = logging.getLogger('definitions.rates.ecb')


# ─────────────────────────────────────────────────────────────────────────────
# Hedef para birimleri
# ─────────────────────────────────────────────────────────────────────────────
# `apps.live_board.tasks.DERIVED_CURRENCIES` ile eşleşir. Kitco task'ı
# bu altı para birimi için USD bazlı kur arar; ECB beslemesi tam
# bunları üretir.
# ─────────────────────────────────────────────────────────────────────────────
TARGET_CURRENCIES = ('EUR', 'GBP', 'CHF', 'CAD', 'AUD', 'JPY')


# -----------------------------------------------------------------------------
# Yardımcılar
# -----------------------------------------------------------------------------


def _find_currency_by_code(code: str) -> Optional[Currencies]:
    """
    Currencies tablosunda ISO koduyla (case-insensitive) aktif kayıt ara.

    Yeni kayıt OLUŞTURMAZ. `Currencies.created_by` zorunlu FK olduğu
    için otomatik üretim güvensizdir; yoksa sessizce None döner ve
    ilgili hedef atlanır.
    """
    if not code:
        return None
    return (
        Currencies.objects
        .filter(
            code__iexact=code.strip(),
            is_active=True,
            is_deleted=False,
        )
        .first()
    )


def _to_market_datetime(feed_date) -> datetime:
    """
    ECB feed_date (date) → Rates.market_time için datetime.

    USE_TZ=False olduğunda naive datetime dönmek güvenlidir;
    settings değişirse timezone.make_aware ile aware'e terfi edilir.
    """
    if feed_date is None:
        return timezone.now()

    dt = datetime.combine(feed_date, datetime.min.time())
    try:
        if timezone.is_naive(dt) and getattr(timezone, 'is_aware', None):
            # USE_TZ=True ise aware'e çevir; False ise naive kalması
            # Django için zaten beklenen davranış.
            from django.conf import settings as _settings
            if getattr(_settings, 'USE_TZ', False):
                dt = timezone.make_aware(dt)
    except Exception:
        # Guard — zamana dokunan hiçbir şey task'ı çökertmesin.
        pass
    return dt


# -----------------------------------------------------------------------------
# Ana Task — Celery Beat saatlik çağırır
# -----------------------------------------------------------------------------


@shared_task(name='definitions.rates.sync_fx_rates_from_ecb')
def sync_fx_rates_from_ecb() -> Dict[str, Any]:
    """
    ECB feed'inden kurları çekip `Rates` tablosuna (USD bazlı) yazar.

    Dönüş
    -----
    Başarı:
        {'status': 'ok',
         'written': {'EUR': 1, 'GBP': 1, ...},
         'skipped': {'JPY': 'missing_currency', ...},
         'feed_date': '2026-04-20'}

    Hata:
        {'status': 'fetch_error', 'message': '...'}
        {'status': 'unexpected_error', 'message': '...'}
        {'status': 'missing_usd_currency', 'message': '...'}
        {'status': 'no_rates'}

    Task ASLA istisna yaymaz; tüm hata yolları dict döner ve worker
    çalışmaya devam eder.
    """
    logger.info("[ECB Task] sync_fx_rates_from_ecb tetiklendi.")

    # ── 1) ECB'den XML çek ve parse et ────────────────────────────────────
    try:
        xml_text = fetch_ecb_xml()
        feed_date, rates_eur_base = parse_ecb_xml(xml_text)
        usd_rates = compute_usd_based_rates(
            rates_eur_base, TARGET_CURRENCIES
        )
    except ECBFetchError as exc:
        logger.warning("[ECB Task] Fetch/parse hatası: %s", exc)
        return {'status': 'fetch_error', 'message': str(exc)}
    except Exception as exc:
        logger.exception("[ECB Task] Beklenmedik fetch hatası: %s", exc)
        return {'status': 'unexpected_error', 'message': str(exc)}

    if not usd_rates:
        logger.warning("[ECB Task] USD bazlı kur listesi boş.")
        return {'status': 'no_rates'}

    # ── 2) USD Currencies kaydı zorunlu ────────────────────────────────────
    usd_currency = _find_currency_by_code('USD')
    if usd_currency is None:
        logger.error(
            "[ECB Task] Currencies tablosunda code='USD' aktif kaydı yok. "
            "Custom UI üzerinden ekleyin."
        )
        return {
            'status': 'missing_usd_currency',
            'message': str(_(
                "Currencies table has no active USD record. "
                "Please add it from the custom UI."
            )),
        }

    # ── 3) Rates tablosuna yaz ─────────────────────────────────────────────
    now_ts = timezone.now()
    market_time = _to_market_datetime(feed_date)

    written: Dict[str, int] = {}
    skipped: Dict[str, str] = {}

    for code, rate in usd_rates.items():
        target_currency = _find_currency_by_code(code)
        if target_currency is None:
            logger.info(
                "[ECB Task] Currencies'te %s kaydı yok; atlandı "
                "(custom UI üzerinden ekleyin).",
                code,
            )
            skipped[code] = 'missing_currency'
            continue

        try:
            rate_decimal = Decimal(str(rate)).quantize(Decimal('0.0001'))
        except Exception:
            skipped[code] = 'decimal_error'
            continue

        if rate_decimal <= 0:
            skipped[code] = 'non_positive'
            continue

        try:
            with transaction.atomic():
                obj, created = Rates.objects.update_or_create(
                    currency_one=usd_currency,
                    currency_two=target_currency,
                    defaults={
                        'name': f'USD/{code}',
                        # ECB referans kuru tek sayı verir — bid/ask
                        # ayrımı yoktur; her ikisine de aynı değer yazılır.
                        # Kitco task'ı sale_price'ı okur; buy_price diğer
                        # ekranlarda tutarlılık için aynı değerle doldurulur.
                        'buy_price': rate_decimal,
                        'sale_price': rate_decimal,
                        'market_time': market_time,
                        'modified_on': now_ts,
                        'is_active': True,
                        'is_deleted': False,
                    },
                )
                written[code] = 1 if created else 2  # 1=yeni, 2=güncellendi
        except Exception as exc:
            logger.warning(
                "[ECB Task] Rates yazım hatası (%s): %s", code, exc
            )
            skipped[code] = f'write_error: {exc}'

    logger.info(
        "[ECB Task] OK — yazım: %s, atlama: %s, feed_date: %s",
        written, skipped, feed_date,
    )

    return {
        'status': 'ok',
        'written': written,
        'skipped': skipped,
        'feed_date': feed_date.isoformat() if feed_date else None,
    }
