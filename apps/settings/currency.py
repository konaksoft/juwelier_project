"""
Mağaza Para Birimi SSOT (Single Source of Truth)
================================================================================

Bu modül, "bu mağaza hangi para biriminde çalışır?" sorusunun TEK kanonik
cevabıdır. Kaynak: ``StoreConfiguration.primary_currency``
(bkz. apps/settings/models.py — 'MAĞAZA BİRİNCİL PARA BİRİMİ').

NEDEN AYRI MODÜL?
    Aynı okuma mantığı projede birden fazla yerde özel (private) helper olarak
    kopyalanmıştı:
        - apps/process/fast_views.py::_get_store_primary_currency
        - apps/banking/bank_views.py::_get_store_primary_currency
    Üçüncü bir kopya (gold_purchases) yerine kanonik modül çıkarıldı; mevcut
    iki helper bu modüle delege eder (davranışları birebir korunur).

KURAL — HARD-CODE YASAK:
    Hiçbir çağrı yerinde "Almanya ise EUR" gibi ülke→para birimi eşlemesi
    YAZILMAZ. Ülke/pazar farkı YALNIZCA StoreConfiguration.primary_currency
    üzerinden gelir. Bu sayede Türkiye (TRY) ve Almanya (EUR) mağazaları aynı
    kod yolunu kullanır; davranış farkı veriden doğar.

ESKİ KAYIT KORUMASI:
    Bu modül YALNIZCA "yeni kayıt varsayılanı" üretir. Veritabanında zaten
    kayıtlı bir para birimini DEĞİŞTİRMEK için kullanılamaz (bkz.
    gold_purchases/views.py::multi_material_product_update — kayıtlı
    sale_currency asla mağaza varsayılanıyla ezilmez).
"""
from __future__ import annotations

# Yapılandırma satırı hiç yoksa dönülecek güvenli varsayılan.
# (juwelier_project Almanya pazarı — StoreConfiguration.primary_currency
#  model default'u da 'EUR'dur.)
FALLBACK_PRIMARY_CURRENCY = 'EUR'


def read_primary_currency(store):
    """Mağazanın yapılandırılmış birincil para birimini döner; YOKSA None.

    "Yapılandırılmamış" ile "EUR olarak yapılandırılmış" arasındaki farkı
    KORUR — varsayılan çözümü bu ayrıma bağlıdır. ASLA istisna fırlatmaz.
    """
    if not store:
        return None
    try:
        from apps.settings.models import StoreConfiguration  # lazy: circular import koruması
        cfg = (
            StoreConfiguration.objects
            .filter(store=store)
            .only('primary_currency')
            .first()
        )
        if cfg and getattr(cfg, 'primary_currency', None):
            return str(cfg.primary_currency).upper()
    except Exception:
        pass
    return None


def get_store_primary_currency(store, default: str = FALLBACK_PRIMARY_CURRENCY) -> str:
    """
    Mağazanın birincil para birimini (ISO kodu, büyük harf) döner.

    StoreConfiguration yoksa, alan boşsa veya sorgu hata verirse `default`
    döner. ASLA istisna fırlatmaz: rapor/ekran akışları para birimi
    okunamadığı için çökmemelidir.
    """
    code = read_primary_currency(store)
    if code:
        return code
    return (default or FALLBACK_PRIMARY_CURRENCY).upper()


def get_store_primary_currency_symbol(store, default: str = FALLBACK_PRIMARY_CURRENCY) -> str:
    """Mağaza birincil para biriminin gösterim sembolü (€, ₺, $, £ ...)."""
    code = get_store_primary_currency(store, default=default)
    try:
        from apps.settings.models import StoreConfiguration
        return StoreConfiguration.PRIMARY_CURRENCY_SYMBOLS.get(code, code)
    except Exception:
        return code


def resolve_default_sale_currency(store, allowed, legacy_default: str = 'USD') -> str:
    """
    Bir "döviz cinsinden satış fiyatı" seçicisi için YENİ KAYIT varsayılanını
    çözer.

    Mantık (tek kural, ülke hard-code'u YOK):
        1. Mağazanın birincil para birimi (`primary_currency`) alınır.
        2. Bu kod ilgili seçicinin desteklediği kodlar (`allowed`) arasındaysa
           varsayılan O'dur.  →  Almanya (EUR) mağazası: EUR
                                 TRY yapılandırılmış mağaza: TRY
        3. Desteklenmiyorsa (örn. primary_currency='CHF' ama seçici CHF
           sunmuyor) modülün eski varsayılanı korunur (`legacy_default`).

    Args:
        store: Stores instance (veya None).
        allowed: Seçicinin kabul ettiği kod dizisi, örn. ('USD','EUR','GBP','TRY').
        legacy_default: Eşleşme yoksa dönecek eski varsayılan (regresyon koruması).

    Returns:
        str: Büyük harfli ISO para birimi kodu.
    """
    allowed_upper = {str(c).upper() for c in (allowed or ())}
    # DİKKAT: get_store_primary_currency() yerine read_primary_currency()
    # kullanılır. Yapılandırma satırı HİÇ YOKSA mağazaya varsayılan bir para
    # birimi ATFEDİLMEZ; modülün eski varsayılanı korunur (regresyon koruması).
    primary = read_primary_currency(store)
    if primary and primary in allowed_upper:
        return primary
    return (legacy_default or 'USD').upper()
