"""ExchangeRateService — Cari/Emanet Refactor.

Cari hesap (CustomerLedger) için işlem anındaki kurun otomatik
çekilmesini sağlar. Personel manuel kur giremesin (manipülasyon
koruması) — kur servisten okunur ve `exchange_rate_eur` alanına
sabit (tarihi) kayıt olarak yazılır.

Kaynak öncelik sırası:
  1) Stores.price_cache.has_buy_eur (anlık güncel)
  2) PriceQuote son SPOT kayıt (Redis düşerse fallback)
  3) None (servis kullanılamıyorsa — caller karar verir)

NOT: Bu servis sadece OKUMA yapar. Kur yazımı (price feed)
`apps.stock_management.tasks.fetch_prices_from_providers` ile
çalışır.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


def get_current_has_rate(store=None) -> Optional[Decimal]:
    """1 gr Has Altın'ın güncel TL ALIŞ kuru.

    Args:
        store: Stores instance (opsiyonel). Yoksa global son SPOT
               PriceQuote kullanılır.

    Returns:
        Decimal kur veya None (kaynak bulunamadıysa).
    """
    # 1) Mağaza özel cache (en güncel)
    if store is not None:
        try:
            cache = getattr(store, 'price_cache', None)
            if cache and cache.has_buy_eur and cache.has_buy_eur > 0:
                return Decimal(cache.has_buy_eur)
        except Exception:
            pass

    # 2) PriceQuote tablosu fallback
    try:
        from apps.stock_management.models import PriceQuote
        quote = (
            PriceQuote.objects
            .filter(metal_type=PriceQuote.MetalType.GOLD_24K)
            .order_by('-id')
            .values('buy_price_eur')
            .first()
        )
        if quote and quote['buy_price_eur'] and quote['buy_price_eur'] > 0:
            return Decimal(quote['buy_price_eur'])
    except Exception:
        pass

    # 3) FAZ 22.8 — PriceService fallback (Redis cache + DB son kayıt).
    #    Mağaza price_cache'i ve PriceQuote son satırı eşzamanlı yazılır;
    #    ancak Redis cache son kayıttan daha taze olabilir veya farklı bir
    #    yazma yolu izleyebilir. Bu zincir cari/fx-rates ile collect_and_close
    #    arasındaki "frontend kuru bulur ama backend bulamaz" yarışını kapatır.
    try:
        from apps.stock_management.services.price_service import PriceService
        hs_data = PriceService.get_price('GOLD_24K')
        if hs_data:
            buy_tl = hs_data.get('buy_tl') or 0
            if buy_tl and Decimal(buy_tl) > 0:
                return Decimal(buy_tl)
            # buy_tl yoksa sell_tl bile olsa kullan (tahsilat satışta yapılır)
            sell_tl = hs_data.get('sell_tl') or 0
            if sell_tl and Decimal(sell_tl) > 0:
                return Decimal(sell_tl)
    except Exception:
        pass

    # 4) FAZ 22.9 — compute_store_has_tl fallback (Products tablosu).
    #    Kaynak 1-3 PriceQuote/Redis ekosistemine bağımlı; PriceQuote tablosu
    #    boş/stale ise hepsi başarısız olur. `get_has_gold_prices` view'ı
    #    bu durumda `Products('Has Altın 24 Ayar')` kaydından kuru okuyarak
    #    ekrana yansıtır — backend de aynı zinciri kullanmalı, aksi halde
    #    "ekranda kur var ama tahsilat alamıyor" çelişkisi yaşanır.
    try:
        from apps.stores.services import compute_store_has_tl
        if store is not None:
            buy_tl, sale_tl = compute_store_has_tl(store)
            if buy_tl and Decimal(buy_tl) > 0:
                return Decimal(buy_tl)
            if sale_tl and Decimal(sale_tl) > 0:
                return Decimal(sale_tl)
    except Exception:
        pass

    return None


def get_current_fx_rate(currency_code: str, store=None) -> Optional[Decimal]:
    """1 birim dövizin güncel TL ALIŞ kuru.

    Args:
        currency_code: 'USD', 'EUR', 'GBP'
        store: opsiyonel.

    Returns:
        Decimal kur veya None.
    """
    code = (currency_code or '').upper()
    if code not in ('USD', 'EUR', 'GBP'):
        return None

    try:
        from apps.stock_management.models import PriceQuote
        metal_map = {
            'USD': PriceQuote.MetalType.USD,
            'EUR': PriceQuote.MetalType.EUR,
            'GBP': PriceQuote.MetalType.GBP,
        }
        quote = (
            PriceQuote.objects
            .filter(metal_type=metal_map[code])
            .order_by('-id')
            .values('buy_price_eur')
            .first()
        )
        if quote and quote['buy_price_eur'] and quote['buy_price_eur'] > 0:
            return Decimal(quote['buy_price_eur'])
    except Exception:
        pass

    return None


def assert_rate_in_tolerance(
    submitted_rate: Decimal,
    system_rate: Decimal,
    tolerance_pct: Decimal = Decimal('0.5'),
) -> bool:
    """Kullanıcıdan gelen kur değeri sistem kuruna ± tolerance_pct
    içinde mi? Cari/Emanet Refactor → manuel kur kabul edilmez,
    sadece güvenlik kontrolü için bu fonksiyon vardır.
    """
    if not submitted_rate or not system_rate or system_rate <= 0:
        return False
    diff_pct = abs((submitted_rate - system_rate) / system_rate) * Decimal('100')
    return diff_pct <= tolerance_pct
