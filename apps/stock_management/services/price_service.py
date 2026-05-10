"""
PriceService - Coklu API Fiyat Saglayici + Redis Cache Katmani
===============================================================

Veri akisi:
    1. Celery task her X saniyede aktif PriceProvider'lari sorgular
    2. API cevaplari PriceQuote tablosuna yazilir (tarihsel kayit)
    3. Ayni anda Redis cache'e son fiyatlar yazilir (hizli okuma)
    4. Frontend/Backend PriceService.get_price() ile okur
    5. Redis bos ise DB fallback otomatik devreye girer

Redis Cache yapisi:
    Key format:   kuyumplus:price:{provider_name}:{metal_type}
    Ornek:        kuyumplus:price:harem_altin:GOLD_24K
    Global key:   kuyumplus:price:best:{metal_type}  (en oncelikli saglayicidan)

    Value (JSON):
    {
        "buy_tl": "3250.50",
        "sell_tl": "3265.75",
        "buy_hs": "1.0000",
        "sell_hs": "1.0047",
        "spread_eur": "15.25",
        "change_rate": "0.85",
        "provider": "harem_altin",
        "quoted_at": "2026-03-25T13:00:00",
        "cached_at": "2026-03-25T13:00:01"
    }

Failover:
    - API A basarisiz -> API B denenecek (priority sirasina gore)
    - Tum API'ler basarisiz -> Son basarili DB kaydi kullanilir
    - DB de bos -> Decimal('0') donulur (gosterime uygun hata mesaji ile)
"""

import json
import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.stock_management.models import PriceProvider, PriceProviderMapping, PriceQuote

logger = logging.getLogger('stock_management')


# ============================================================================
# REDIS CACHE KEY URETECI
# ============================================================================

# Cache key on eki - settings.py'deki CACHES.KEY_PREFIX ('kuyumplus') ile uyumlu
PRICE_CACHE_PREFIX = 'price'


def _cache_key(provider_name: str, metal_type: str) -> str:
    """
    Saglayici-spesifik cache key uret.
    Ornek: 'price:harem_altin:GOLD_24K'
    Not: Django RedisCache otomatik olarak KEY_PREFIX ('kuyumplus') ekler.
    """
    return f"{PRICE_CACHE_PREFIX}:{provider_name}:{metal_type}"


def _best_cache_key(metal_type: str) -> str:
    """
    En oncelikli saglayicidan gelen fiyat icin global key.
    Ornek: 'price:best:GOLD_24K'
    """
    return f"{PRICE_CACHE_PREFIX}:best:{metal_type}"


def _all_metals_key() -> str:
    """Tum metal tiplerinin listesi icin key."""
    return f"{PRICE_CACHE_PREFIX}:all_metals"


# ============================================================================
# ANA SERVIS SINIFI
# ============================================================================

class PriceService:
    """
    Fiyat okuma, yazma ve cache yonetimi icin merkezi servis.

    OKUMA (View/Template/API tarafinda kullanilir):
        # En iyi fiyati al (en yuksek oncelikli calisancaglayici)
        price = PriceService.get_price('GOLD_24K')
        # price = {'buy_tl': Decimal('3250.50'), 'sell_tl': Decimal('3265.75'), ...}

        # Belirli saglayicidan al
        price = PriceService.get_price('GOLD_24K', provider_name='harem_altin')

        # Tum saglayicilarin fiyatlarini karsilastir
        all_prices = PriceService.get_all_provider_prices('GOLD_24K')

    YAZMA (Celery task tarafinda kullanilir):
        PriceService.save_and_cache_quote(
            provider=provider_instance,
            metal_type='GOLD_24K',
            currency_code='ALTIN',
            buy_price_eur=Decimal('3250.50'),
            sell_price_eur=Decimal('3265.75'),
            raw_data={...},
        )
    """

    # --- OKUMA ISLEMLERI ---

    @classmethod
    def get_price(
        cls,
        metal_type: str,
        provider_name: Optional[str] = None,
    ) -> dict:
        """
        Belirli bir metal/doviz tipi icin guncel fiyati dondurur.

        Oncelik sirasi:
            1. Redis cache (hizli)
            2. Veritabani son kayit (yavas ama guvenilir)
            3. Bos deger (tum kaynaklar basarisiz)

        Args:
            metal_type: PriceQuote.MetalType enum degeri (orn: 'GOLD_24K')
            provider_name: Belirli saglayici adi. None ise en oncelikli saglayici.

        Returns:
            dict: {
                'buy_tl': Decimal,
                'sell_tl': Decimal,
                'buy_hs': Decimal,
                'sell_hs': Decimal,
                'spread_eur': Decimal,
                'change_rate': Decimal,
                'provider': str,
                'quoted_at': str (ISO format),
                'source': str ('cache' | 'db' | 'empty'),
            }
        """
        # 1. Redis cache'ten oku
        if provider_name:
            cache_key = _cache_key(provider_name, metal_type)
        else:
            cache_key = _best_cache_key(metal_type)

        cached = cache.get(cache_key)
        if cached:
            try:
                data = json.loads(cached) if isinstance(cached, str) else cached
                return {
                    'buy_tl': Decimal(str(data.get('buy_tl', '0'))),
                    'sell_tl': Decimal(str(data.get('sell_tl', '0'))),
                    'buy_hs': Decimal(str(data.get('buy_hs', '0'))),
                    'sell_hs': Decimal(str(data.get('sell_hs', '0'))),
                    'spread_eur': Decimal(str(data.get('spread_eur', '0'))),
                    'change_rate': Decimal(str(data.get('change_rate', '0'))),
                    'provider': data.get('provider', ''),
                    'quoted_at': data.get('quoted_at', ''),
                    'source': 'cache',
                }
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning(f"Cache parse hatasi: key={cache_key}, err={e}")

        # 2. Veritabanindan fallback
        return cls._get_price_from_db(metal_type, provider_name)

    @classmethod
    def _get_price_from_db(
        cls,
        metal_type: str,
        provider_name: Optional[str] = None,
    ) -> dict:
        """FAZ 7: DB'den son fiyat kaydini cek — OperationalError'da bos deger don."""
        try:
            queryset = PriceQuote.objects.filter(metal_type=metal_type)

            if provider_name:
                queryset = queryset.filter(provider__name=provider_name)
            else:
                queryset = queryset.filter(
                    provider__is_active=True,
                    provider__status=PriceProvider.ProviderStatus.ACTIVE,
                ).order_by('provider__priority', '-quoted_at')

            quote = queryset.first()

            if quote:
                logger.info(
                    f"DB fallback kullanildi: metal={metal_type}, "
                    f"provider={quote.provider.name}"
                )
                return {
                    'buy_tl': quote.buy_price_eur,
                    'sell_tl': quote.sell_price_eur,
                    'buy_hs': quote.buy_price_hs,
                    'sell_hs': quote.sell_price_hs,
                    'spread_eur': quote.spread_eur,
                    'change_rate': quote.change_rate,
                    'provider': quote.provider.name,
                    'quoted_at': quote.quoted_at.isoformat() if quote.quoted_at else '',
                    'source': 'db',
                }
        except Exception as exc:
            logger.error(f"DB fiyat sorgusu basarisiz: metal={metal_type}, err={exc}")
            return cls._empty_price(metal_type)

        logger.warning(f"Fiyat bulunamadi: metal={metal_type}, provider={provider_name}")
        return cls._empty_price(metal_type)

    @classmethod
    def _empty_price(cls, metal_type: str) -> dict:
        """Bos fiyat sablonu."""
        return {
            'buy_tl': Decimal('0'),
            'sell_tl': Decimal('0'),
            'buy_hs': Decimal('0'),
            'sell_hs': Decimal('0'),
            'spread_eur': Decimal('0'),
            'change_rate': Decimal('0'),
            'provider': '',
            'quoted_at': '',
            'source': 'empty',
        }

    @classmethod
    def get_all_provider_prices(cls, metal_type: str) -> list:
        """
        Tum aktif saglayicilarin bu metal icin fiyatlarini dondurur.
        Karsilastirma ekrani icin.

        Returns:
            list: [
                {'provider': 'harem_altin', 'buy_tl': ..., 'sell_tl': ..., 'priority': 1},
                {'provider': 'grand_bazaar', 'buy_tl': ..., 'sell_tl': ..., 'priority': 2},
            ]
        """
        providers = PriceProvider.objects.filter(
            is_active=True,
        ).order_by('priority')

        results = []
        for provider in providers:
            price = cls.get_price(metal_type, provider_name=provider.name)
            price['priority'] = provider.priority
            price['display_name'] = provider.display_name
            price['status'] = provider.status
            results.append(price)

        return results

    @classmethod
    def get_has_altin_tl(cls, provider_name: Optional[str] = None) -> tuple:
        """
        Has Altin (24K) icin alis ve satis TL fiyatini dondurur.

        Mevcut sistemdeki compute_store_has_tl() yerine gecmek icin tasarlandi.

        Returns:
            tuple: (buy_tl: Decimal, sell_tl: Decimal)
        """
        price = cls.get_price('GOLD_24K', provider_name=provider_name)
        return price['buy_tl'], price['sell_tl']

    # --- YAZMA ISLEMLERI ---

    @classmethod
    def save_and_cache_quote(
        cls,
        *,
        provider: PriceProvider,
        metal_type: str,
        currency_code: str,
        buy_price_eur: Decimal,
        sell_price_eur: Decimal,
        buy_price_hs: Decimal = Decimal('0.000000'),
        sell_price_hs: Decimal = Decimal('0.000000'),
        change_rate: Decimal = Decimal('0.0000'),
        quote_type: str = PriceQuote.QuoteType.SPOT,
        raw_data: Optional[dict] = None,
        quoted_at: Optional[datetime] = None,
    ) -> PriceQuote:
        """
        API'den gelen fiyati hem DB'ye hem Redis'e yaz.

        Bu metot Celery task'i tarafindan cagrilir.

        Args:
            provider: PriceProvider instance
            metal_type: Standart metal tipi (orn: 'GOLD_24K')
            currency_code: API'den gelen orijinal kod (orn: 'ALTIN')
            buy_price_eur: Alis fiyati TL
            sell_price_eur: Satis fiyati TL
            buy_price_hs: Alis fiyati Has (hesaplanmis)
            sell_price_hs: Satis fiyati Has (hesaplanmis)
            change_rate: Yuzde degisim
            quote_type: Fiyat tipi (SPOT, DAILY_OPEN, vb.)
            raw_data: API'nin ham JSON cevabi
            quoted_at: Fiyatin gecerli oldugu an

        Returns:
            PriceQuote: Olusturulan kayit
        """
        if quoted_at is None:
            quoted_at = timezone.now()

        # 1. DB'ye yaz (tarihsel kayit)
        quote = PriceQuote.objects.create(
            provider=provider,
            metal_type=metal_type,
            currency_code=currency_code,
            quote_type=quote_type,
            buy_price_eur=buy_price_eur,
            sell_price_eur=sell_price_eur,
            buy_price_hs=buy_price_hs,
            sell_price_hs=sell_price_hs,
            change_rate=change_rate,
            raw_data=raw_data,
            quoted_at=quoted_at,
        )

        # 2. Redis cache'e yaz
        cache_data = {
            'buy_tl': str(buy_price_eur),
            'sell_tl': str(sell_price_eur),
            'buy_hs': str(buy_price_hs),
            'sell_hs': str(sell_price_hs),
            'spread_eur': str(sell_price_eur - buy_price_eur),
            'change_rate': str(change_rate),
            'provider': provider.name,
            'quoted_at': quoted_at.isoformat(),
            'cached_at': timezone.now().isoformat(),
        }

        cache_value = json.dumps(cache_data, default=str)
        ttl = provider.cache_ttl_seconds or 30

        # Saglayici-spesifik key
        provider_key = _cache_key(provider.name, metal_type)
        cache.set(provider_key, cache_value, timeout=ttl)

        # En oncelikli saglayici ise global 'best' key'i de guncelle
        cls._update_best_price_cache(provider, metal_type, cache_value, ttl)

        # 3. Saglayici basari durumunu guncelle
        provider.mark_success()

        return quote

    @classmethod
    def _update_best_price_cache(
        cls,
        provider: PriceProvider,
        metal_type: str,
        cache_value: str,
        ttl: int,
    ):
        """
        En yuksek oncelikli aktif saglayicinin fiyatini 'best' key'e yaz.

        Mantik:
            - Bu saglayici en yuksek oncelikli ise (priority en dusuk numara),
              dogrudan 'best' key'i guncelle.
            - Degilse, mevcut 'best' key'in saglayicisini kontrol et.
              Eger mevcut 'best' bu saglayicidan dusuk oncelikli ise guncelleme.
        """
        best_key = _best_cache_key(metal_type)

        # Mevcut best'i kontrol et
        current_best = cache.get(best_key)
        if current_best:
            try:
                current_data = json.loads(current_best) if isinstance(current_best, str) else current_best
                current_provider_name = current_data.get('provider', '')

                if current_provider_name:
                    try:
                        current_prov = PriceProvider.objects.get(name=current_provider_name)
                        # Mevcut best daha oncelikli ise (dusuk priority numarasi) dokunma
                        if current_prov.priority < provider.priority:
                            return
                    except PriceProvider.DoesNotExist:
                        pass  # Eski saglayici silinmis, yenisini yaz
            except (json.JSONDecodeError, TypeError):
                pass  # Parse edilemiyorsa ustune yaz

        # Best key'i guncelle
        cache.set(best_key, cache_value, timeout=ttl)

    @classmethod
    def process_api_response(
        cls,
        provider: PriceProvider,
        response_data: dict,
        base_has_buy_eur: Optional[Decimal] = None,
        base_has_sell_tl: Optional[Decimal] = None,
    ) -> int:
        """
        Bir API saglayicisinin cevabini isle.

        API'nin gonderdigi tum metal/doviz fiyatlarini mapping tablosuna gore
        sistemimizin standart tiplerine cevirir, DB'ye ve cache'e yazar.

        Args:
            provider: PriceProvider instance
            response_data: API'den gelen JSON cevap (list of dicts)
            base_has_buy_eur: Has Altin alis TL kuru (Has bazli hesaplama icin)
            base_has_sell_tl: Has Altin satis TL kuru

        Returns:
            int: Basariyla islenen fiyat sayisi
        """
        # Bu saglayicinin mapping'lerini cek
        mappings = PriceProviderMapping.objects.filter(
            provider=provider,
            is_active=True,
        ).select_related('provider')

        # Mapping'leri source_code bazli dict'e cevir (hizli erisim)
        mapping_dict = {}
        for m in mappings:
            mapping_dict[m.source_code.upper()] = m

        processed_count = 0
        items = response_data if isinstance(response_data, list) else [response_data]

        for item in items:
            if not isinstance(item, dict):
                continue

            # API'nin gonderdigi kodu bul
            source_code = str(
                item.get('currencyCode', '') or
                item.get('code', '') or
                item.get('symbol', '')
            ).upper()

            if not source_code:
                continue

            # Mapping var mi?
            mapping = mapping_dict.get(source_code)
            if not mapping:
                continue

            # Fiyatlari cek (mapping'deki alan adlarina gore)
            try:
                buy_tl = Decimal(str(item.get(mapping.buy_field_name, 0) or 0))
                sell_tl = Decimal(str(item.get(mapping.sell_field_name, 0) or 0))
            except Exception as e:
                logger.warning(
                    f"Fiyat parse hatasi: provider={provider.name}, "
                    f"code={source_code}, err={e}"
                )
                continue

            if buy_tl <= 0 and sell_tl <= 0:
                continue

            # Has bazli fiyat hesapla
            buy_hs = Decimal('0')
            sell_hs = Decimal('0')
            if base_has_buy_eur and base_has_buy_eur > 0:
                buy_hs = (buy_tl / base_has_buy_eur).quantize(
                    Decimal('0.000001'), rounding=ROUND_HALF_UP
                )
            if base_has_sell_tl and base_has_sell_tl > 0:
                sell_hs = (sell_tl / base_has_sell_tl).quantize(
                    Decimal('0.000001'), rounding=ROUND_HALF_UP
                )

            change_rate = Decimal(str(item.get('changeRate', 0) or 0))

            # DB + Cache yaz
            cls.save_and_cache_quote(
                provider=provider,
                metal_type=mapping.target_metal_type,
                currency_code=source_code,
                buy_price_eur=buy_tl,
                sell_price_eur=sell_tl,
                buy_price_hs=buy_hs,
                sell_price_hs=sell_hs,
                change_rate=change_rate,
                raw_data=item,
            )

            processed_count += 1

        return processed_count

    # --- CACHE YONETIMI ---

    @classmethod
    def invalidate_cache(cls, metal_type: Optional[str] = None, provider_name: Optional[str] = None):
        """
        Belirli fiyat cache'ini temizle.

        Args:
            metal_type: Temizlenecek metal tipi. None ise tum metaller.
            provider_name: Temizlenecek saglayici. None ise tum saglayicilar.
        """
        if metal_type and provider_name:
            cache.delete(_cache_key(provider_name, metal_type))
            cache.delete(_best_cache_key(metal_type))
        elif metal_type:
            # Bu metal icin tum saglayici cache'lerini temizle
            providers = PriceProvider.objects.values_list('name', flat=True)
            keys = [_cache_key(p, metal_type) for p in providers]
            keys.append(_best_cache_key(metal_type))
            cache.delete_many(keys)
        else:
            logger.info("Tum fiyat cache'i temizleniyor...")
            # Tum metal ve saglayici kombinasyonlari
            providers = PriceProvider.objects.values_list('name', flat=True)
            metal_types = [choice[0] for choice in PriceQuote.MetalType.choices]
            keys = []
            for p in providers:
                for m in metal_types:
                    keys.append(_cache_key(p, m))
            for m in metal_types:
                keys.append(_best_cache_key(m))
            if keys:
                cache.delete_many(keys)

    @classmethod
    def warmup_cache(cls):
        """
        Redis cache'i DB'den doldur.

        Sunucu yeniden baslatildiginda veya Redis temizlendiginde
        son fiyatlari DB'den cache'e yukler.
        """
        providers = PriceProvider.objects.filter(
            is_active=True,
            status=PriceProvider.ProviderStatus.ACTIVE,
        ).order_by('priority')

        metal_types = [choice[0] for choice in PriceQuote.MetalType.choices]
        loaded = 0

        for metal_type in metal_types:
            for provider in providers:
                quote = PriceQuote.objects.filter(
                    provider=provider,
                    metal_type=metal_type,
                ).order_by('-quoted_at').first()

                if quote:
                    cache_data = {
                        'buy_tl': str(quote.buy_price_eur),
                        'sell_tl': str(quote.sell_price_eur),
                        'buy_hs': str(quote.buy_price_hs),
                        'sell_hs': str(quote.sell_price_hs),
                        'spread_eur': str(quote.spread_eur),
                        'change_rate': str(quote.change_rate),
                        'provider': provider.name,
                        'quoted_at': quote.quoted_at.isoformat(),
                        'cached_at': timezone.now().isoformat(),
                    }

                    cache_value = json.dumps(cache_data, default=str)
                    ttl = provider.cache_ttl_seconds or 300  # Warmup icin daha uzun TTL

                    cache.set(_cache_key(provider.name, metal_type), cache_value, timeout=ttl)
                    loaded += 1

            # Best key'i en oncelikli saglayicidan doldur
            best_quote = PriceQuote.objects.filter(
                metal_type=metal_type,
                provider__is_active=True,
                provider__status=PriceProvider.ProviderStatus.ACTIVE,
            ).order_by('provider__priority', '-quoted_at').first()

            if best_quote:
                best_data = {
                    'buy_tl': str(best_quote.buy_price_eur),
                    'sell_tl': str(best_quote.sell_price_eur),
                    'buy_hs': str(best_quote.buy_price_hs),
                    'sell_hs': str(best_quote.sell_price_hs),
                    'spread_eur': str(best_quote.spread_eur),
                    'change_rate': str(best_quote.change_rate),
                    'provider': best_quote.provider.name,
                    'quoted_at': best_quote.quoted_at.isoformat(),
                    'cached_at': timezone.now().isoformat(),
                }
                cache.set(
                    _best_cache_key(metal_type),
                    json.dumps(best_data, default=str),
                    timeout=300,
                )

        logger.info(f"Cache warmup tamamlandi: {loaded} fiyat kaydi yuklendi")
        return loaded
