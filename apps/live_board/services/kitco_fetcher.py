"""
Kitco Canlı Piyasa Fiyat Çekici
================================

Amaç
----
https://www.kitco.com/price/precious-metals sayfasından
SADECE `requests` kullanarak spot değerli maden fiyatlarını
(GOLD, SILVER, PLATINUM, PALLADIUM, RHODIUM) hafif bir
şekilde çekmek.

Tasarım Notu
------------
Bu modül yalnızca veri indirir ve normalize eder. Veritabanına
yazım, Celery schedule, view ve frontend entegrasyonu çağıran
katmanlara bırakılır. Böylece "Kitco çekilebiliyor mu?" sorusu
bu tek modülle yalıtılmış biçimde cevaplanabilir.

Kısıtlar
--------
* Selenium veya tarayıcı otomasyonu YOKTUR.
* Çekilen ham fiyatlar hiçbir işçilik/kâr formülüne bağlanmaz.
* Hata durumunda sistem çökmez — KitcoFetchError fırlatılır,
  çağıran taraf (Celery task, management command) try/except
  ile yutup loglar.
* gettext_lazy ile i18n uyumu korunur.
"""

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Sabitler
# -----------------------------------------------------------------------------

KITCO_URL: str = "https://www.kitco.com/price/precious-metals"

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "*/*;q=0.8"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

REQUEST_TIMEOUT: int = 15

SUPPORTED_METALS: Tuple[str, ...] = (
    "GOLD",
    "SILVER",
    "PLATINUM",
    "PALLADIUM",
    "RHODIUM",
)

# Kitco widget'ı bu para birimlerini sunuyor; başlangıçta hepsini
# kabul listesine alıp filtrelemeyi UI'a taşıyoruz.
SUPPORTED_CURRENCIES: Tuple[str, ...] = (
    "USD", "EUR", "GBP", "CHF", "JPY",
    "AUD", "CAD", "CNY", "HKD", "BRL",
)

SUPPORTED_UNITS: Tuple[str, ...] = ("OZ", "GRAM", "KILO", "TOLA")

# Troy ons → gram dönüşüm katsayısı.
OZ_TO_GRAM: Decimal = Decimal("31.1034768")

# Kitco'nun __NEXT_DATA__'sında metal adı açıkça geçmeyebilir; `ID`
# alanı ISO 4217 kodu (XAU/XAG/XPT/XPD/XRH), bazen de sayısal bir
# kimlik olabilir. Bu harita ISO kodlarını iç metal etiketimize bağlar.
METAL_ID_ALIASES: Dict[str, str] = {
    "XAU": "GOLD",
    "XAG": "SILVER",
    "XPT": "PLATINUM",
    "XPD": "PALLADIUM",
    "XRH": "RHODIUM",
}

# Next.js uygulamalarının initial state'i bu script tag'inde yer alır.
NEXT_DATA_REGEX: re.Pattern = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


# -----------------------------------------------------------------------------
# Hata tipi
# -----------------------------------------------------------------------------


class KitcoFetchError(Exception):
    """Kitco veri çekme/parse sürecinde yakalanan tüm hatalar."""


# -----------------------------------------------------------------------------
# Yardımcılar
# -----------------------------------------------------------------------------


def _safe_decimal(value: Any) -> Optional[Decimal]:
    """Herhangi bir değeri güvenle Decimal'e çevirir; hata olursa None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    try:
        cleaned = str(value).replace(",", "").strip()
        if not cleaned:
            return None
        return Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _normalize_metal(value: Any) -> Optional[str]:
    """
    Ham metal etiketini SUPPORTED_METALS içinden birine eşler.

    Sırayla dener:
    1. ISO 4217 kodu (XAU/XAG/XPT/XPD/XRH) — tam eşleşme
    2. Substring eşleşme (örn. 'Gold Spot' → GOLD)
    """
    if value is None:
        return None
    upper = str(value).strip().upper()
    if not upper:
        return None
    if upper in METAL_ID_ALIASES:
        return METAL_ID_ALIASES[upper]
    for metal in SUPPORTED_METALS:
        if metal in upper:
            return metal
    return None


def _normalize_currency(value: Any) -> Optional[str]:
    if value is None:
        return None
    upper = str(value).strip().upper()
    return upper if upper in SUPPORTED_CURRENCIES else None


def _normalize_unit(value: Any) -> str:
    if value is None:
        return "OZ"
    upper = str(value).strip().upper()
    # "OUNCE" gibi varyasyonları da yakala
    if "OUNCE" in upper or upper == "OZ":
        return "OZ"
    if "GRAM" in upper:
        return "GRAM"
    if "KILO" in upper:
        return "KILO"
    if "TOLA" in upper:
        return "TOLA"
    return upper if upper in SUPPORTED_UNITS else "OZ"


# -----------------------------------------------------------------------------
# HTTP katmanı
# -----------------------------------------------------------------------------


def fetch_kitco_html(
    url: str = KITCO_URL,
    timeout: int = REQUEST_TIMEOUT,
) -> str:
    """
    Kitco sayfasının ham HTML'ini döner.

    Ağ veya HTTP hatalarında KitcoFetchError fırlatır.
    """
    try:
        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("[Kitco] HTTP hatası (%s): %s", url, exc)
        raise KitcoFetchError(str(_("Kitco HTTP isteği başarısız.")))
    return response.text


# -----------------------------------------------------------------------------
# Next.js __NEXT_DATA__ çıkarımı
# -----------------------------------------------------------------------------


def extract_next_data(html: str) -> Dict[str, Any]:
    """HTML içinden __NEXT_DATA__ JSON bloğunu okur."""
    match = NEXT_DATA_REGEX.search(html)
    if not match:
        logger.warning(
            "[Kitco] __NEXT_DATA__ bloğu bulunamadı (html_size=%d)",
            len(html),
        )
        raise KitcoFetchError(str(_("__NEXT_DATA__ bloğu bulunamadı.")))
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("[Kitco] __NEXT_DATA__ JSON parse hatası: %s", exc)
        raise KitcoFetchError(str(_("__NEXT_DATA__ geçersiz JSON.")))


def _walk_for_price_records(
    node: Any,
    accumulator: List[Dict[str, Any]],
) -> None:
    """
    JSON ağacında `bid` + `ask` alanlarını birlikte içeren dict'leri
    toplar. Sabit bir yol takip etmek yerine ağacı tarayan bu yöntem,
    Kitco'nun alt yapısı değişse bile çalışmaya devam eder.
    """
    if isinstance(node, dict):
        keys_lower = {k.lower(): k for k in node.keys()}
        if "bid" in keys_lower and "ask" in keys_lower:
            accumulator.append(node)
        for value in node.values():
            _walk_for_price_records(value, accumulator)
    elif isinstance(node, list):
        for item in node:
            _walk_for_price_records(item, accumulator)


def parse_price_records(next_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Next.js JSON ağacında fiyat içeren aday dict'leri listeler."""
    records: List[Dict[str, Any]] = []
    _walk_for_price_records(next_data, records)
    return records


def normalize_record(
    raw: Dict[str, Any],
    position: Optional[int] = None,
    total: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Ham aday dict'i standart bir sözlüğe indirger.

    Metal eşleşme stratejisi (sırayla):
    1. `metal`/`symbol`/`name`/`commodity`/`product` alanları
    2. `ID` alanı (ISO kodu veya metal adı formatında)
    3. Pozisyonel fallback — `total == 5` ise Kitco'nun standart
       widget sırası (GOLD, SILVER, PLATINUM, PALLADIUM, RHODIUM)
       kabul edilir.

    Kitco default sayfası kayıt içinde `currency` alanı üretmez
    (yalnızca USD servis eder). Dolayısıyla currency bulunamazsa
    "USD" varsayılanıyla işaretlenir; EUR vb. diğer para birimleri
    Rates tablosundaki USD→hedef kuru ile türetilir.

    Dönüş formatı::

        {
            "metal":    "GOLD",
            "currency": "USD",
            "unit":     "OZ",
            "bid":      Decimal("1845.23"),
            "ask":      Decimal("1846.10"),
        }
    """
    keys_lower = {k.lower(): k for k in raw.keys()}

    def lookup(*names: str) -> Any:
        for name in names:
            original_key = keys_lower.get(name.lower())
            if original_key is not None:
                return raw.get(original_key)
        return None

    # 1) İsim bazlı eşleşme
    metal = _normalize_metal(
        lookup("metal", "symbol", "name", "commodity", "product")
    )
    # 2) ID bazlı eşleşme (ISO kodu veya metin)
    if metal is None:
        metal = _normalize_metal(lookup("id"))
    # 3) Pozisyonel fallback (Kitco'nun widget sırası sabittir)
    if (
        metal is None
        and position is not None
        and total == len(SUPPORTED_METALS)
    ):
        metal = SUPPORTED_METALS[position]

    if metal is None:
        return None

    bid = _safe_decimal(lookup("bid"))
    ask = _safe_decimal(lookup("ask"))
    if bid is None or ask is None:
        return None

    currency = _normalize_currency(
        lookup("currency", "ccy", "cur", "curr")
    ) or "USD"

    unit = _normalize_unit(lookup("unit", "weight", "uom"))

    return {
        "metal": metal,
        "currency": currency,
        "unit": unit,
        "bid": bid,
        "ask": ask,
    }


# -----------------------------------------------------------------------------
# Üst seviye API
# -----------------------------------------------------------------------------


def fetch_kitco_live_prices(
    url: str = KITCO_URL,
) -> List[Dict[str, Any]]:
    """
    Ana giriş noktası: normalize edilmiş canlı fiyat listesi döner.

    Hata durumunda KitcoFetchError fırlatır. Celery task bunu
    try/except ile yutacak; dolayısıyla sistem çökmez.
    """
    html = fetch_kitco_html(url)
    next_data = extract_next_data(html)
    raw_records = parse_price_records(next_data)
    total = len(raw_records)
    logger.info("[Kitco] __NEXT_DATA__ içinde aday kayıt: %d", total)

    results: List[Dict[str, Any]] = []
    seen: set = set()
    for idx, raw in enumerate(raw_records):
        rec = normalize_record(raw, position=idx, total=total)
        if rec is None:
            continue
        key = (rec["metal"], rec["currency"], rec["unit"])
        if key in seen:
            continue
        seen.add(key)
        results.append(rec)

    logger.info(
        "[Kitco] Normalize edilmiş kayıt sayısı: %d",
        len(results),
    )

    if not results:
        raise KitcoFetchError(
            str(_("Kitco yanıtında geçerli fiyat kaydı bulunamadı."))
        )

    return results


def diagnose_kitco_page(url: str = KITCO_URL) -> Dict[str, Any]:
    """
    Tanılama yardımcısı: Kitco sayfasının yapısı hakkında özet bilgi
    döner. Management komutu bu fonksiyonu çağırıp konsola basar.

    Hiçbir istisna fırlatmaz — hataları çıktı sözlüğüne gömer.
    """
    diag: Dict[str, Any] = {"url": url}
    try:
        html = fetch_kitco_html(url)
    except KitcoFetchError as exc:
        diag["error"] = f"fetch_html: {exc}"
        return diag

    diag["html_size"] = len(html)
    diag["has_next_data_tag"] = "__NEXT_DATA__" in html
    diag["contains_world_spot_price"] = "World Spot Price" in html
    diag["contains_eur_literal"] = (">EUR<" in html) or ('"EUR"' in html)

    try:
        next_data = extract_next_data(html)
    except KitcoFetchError as exc:
        diag["error"] = f"extract_next_data: {exc}"
        return diag

    diag["next_data_top_keys"] = list(next_data.keys())
    raw_records = parse_price_records(next_data)
    diag["candidate_record_count"] = len(raw_records)
    diag["first_candidate_keys"] = (
        sorted(raw_records[0].keys()) if raw_records else []
    )

    # JSON'a güvenli biçimde yazılabilecek primitif değer dönüştürücü.
    def _safe_val(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        return str(v)

    # Tüm aday kayıtların ID alanlarını listele — metal sırasını ve
    # ID formatını (ISO kod, sayısal, tam ad) gözlemlemek için kritik.
    diag["all_candidate_ids"] = [
        _safe_val(raw.get("ID") if "ID" in raw else raw.get("id"))
        for raw in raw_records
    ]

    # İlk ham kaydın tüm alan/değer çiftlerini dök — ID formatını
    # net görebilmek için.
    diag["first_candidate_values"] = (
        {k: _safe_val(v) for k, v in raw_records[0].items()}
        if raw_records
        else {}
    )

    # Normalize — pozisyonel fallback etkin.
    total = len(raw_records)
    normalized: List[Dict[str, Any]] = []
    for idx, raw in enumerate(raw_records):
        rec = normalize_record(raw, position=idx, total=total)
        if rec is not None:
            normalized.append(rec)

    diag["normalized_record_count"] = len(normalized)
    diag["normalized_sample"] = [
        {
            "metal": r["metal"],
            "currency": r["currency"],
            "unit": r["unit"],
            "bid": str(r["bid"]),
            "ask": str(r["ask"]),
        }
        for r in normalized[:5]
    ]
    return diag
