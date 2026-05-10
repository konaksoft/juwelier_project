"""
Avrupa Merkez Bankası (ECB) FX Kur Çekici
==========================================

Amaç
----
https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml
adresinden günlük referans döviz kurlarını çekip normalize etmek.

Tasarım
-------
Bu modül yalnızca HTTP + XML parse + matematiksel dönüşüm yapar.
Veritabanına YAZIM, Celery schedule ve admin görüntüleme sonraki
katmanlara aittir. Böylece "ECB verisi okunabiliyor mu?" sorusu
tek başına test edilebilir.

ECB Veri Formatı
----------------
ECB XML feed'i **EUR bazlıdır**. Her `<Cube currency="XYZ" rate="1.0823"/>`
kaydı şu anlama gelir:

    1 EUR = 1.0823 XYZ

EUR'nun kendi oranı XML'de geçmez; modül açıkça 1.0 olarak ekler.

USD Bazına Dönüşüm
------------------
`Rates` tablosu `currency_one=USD, currency_two=X` yapısında
çalışır ve `sale_price` şu formülü taşır:

    sale_price = X_per_USD     (1 USD kaç X eder)

ECB'den gelen EUR bazlı oranları USD bazına çevirmek için:

    X_per_USD = X_per_EUR / USD_per_EUR

Örnek:
    USD_per_EUR = 1.0823      (XML'de `<Cube currency="USD" rate="1.0823"/>`)
    GBP_per_EUR = 0.8561      (XML'de `<Cube currency="GBP" rate="0.8561"/>`)
    GBP_per_USD = 0.8561 / 1.0823 = 0.7910

Kısıtlar
--------
* Bu modül VT'ye HIÇ dokunmaz (pure fonksiyonlar).
* Ağ / parse hataları `ECBFetchError` fırlatır; çağıran taraf
  (Celery task) yutup loglar.
* Tüm kullanıcı mesajları `gettext_lazy` ile i18n-uyumlu.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Optional, Tuple

import requests
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Sabitler
# -----------------------------------------------------------------------------

ECB_DAILY_URL: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

REQUEST_TIMEOUT: int = 15

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# ECB XML namespace'leri.
NAMESPACE: Dict[str, str] = {
    "gesmes": "http://www.gesmes.org/xml/2002-08-01",
    "eurofxref": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
}

# Decimal quantize referansı — Rates.sale_price decimal_places=4
QUANTIZE_4: Decimal = Decimal("0.0001")


# -----------------------------------------------------------------------------
# Hata tipi
# -----------------------------------------------------------------------------


class ECBFetchError(Exception):
    """ECB feed indirme veya parse sürecinde yakalanan tüm hatalar."""


# -----------------------------------------------------------------------------
# HTTP Katmanı
# -----------------------------------------------------------------------------


def fetch_ecb_xml(
    url: str = ECB_DAILY_URL,
    timeout: int = REQUEST_TIMEOUT,
) -> str:
    """
    ECB günlük XML feed'inin ham metnini döner.

    Ağ veya HTTP hatalarında `ECBFetchError` fırlatır.
    """
    try:
        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("[ECB] HTTP hatası (%s): %s", url, exc)
        raise ECBFetchError(str(_("ECB HTTP isteği başarısız.")))
    return response.text


# -----------------------------------------------------------------------------
# XML Parse Katmanı
# -----------------------------------------------------------------------------


def _find_inner_cube(root: ET.Element) -> Optional[ET.Element]:
    """
    ECB XML'i iki iç içe <Cube> seviyesine sahiptir:
        <Cube>                             ← dış sarıcı
          <Cube time="2026-04-20">         ← tarih taşıyan iç Cube
            <Cube currency="USD" rate="..."/>
            ...

    Namespace'li veya namespace'siz arama yap. Bulamazsa None.
    """
    # Namespace'li arama (ECB standart yanıtı)
    inner = root.find(
        ".//eurofxref:Cube/eurofxref:Cube[@time]", NAMESPACE
    )
    if inner is not None:
        return inner

    # Fallback: namespace çıkarılmış XML
    for candidate in root.iter():
        # Local name'e indirgenmiş karşılaştırma
        tag = candidate.tag.rsplit("}", 1)[-1]
        if tag == "Cube" and candidate.get("time"):
            return candidate
    return None


def parse_ecb_xml(xml_text: str) -> Tuple[Optional[date], Dict[str, Decimal]]:
    """
    ECB XML'inden `(feed_date, rates_by_code)` tuple'ı döner.

    rates_by_code: `{'USD': Decimal('1.0823'), 'GBP': Decimal('0.8561'), ...}`
    Yani: 1 EUR = X target_currency (ECB standardı).

    EUR = 1.0 olarak otomatik eklenir.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("[ECB] XML parse hatası: %s", exc)
        raise ECBFetchError(str(_("ECB XML parse edilemedi.")))

    inner_cube = _find_inner_cube(root)
    if inner_cube is None:
        logger.warning(
            "[ECB] İç <Cube time=...> bloğu bulunamadı "
            "(root_tag=%s, çocuk_sayısı=%d)",
            root.tag, len(list(root)),
        )
        raise ECBFetchError(str(_("ECB XML içinde Cube yapısı bulunamadı.")))

    # Tarih (ör. '2026-04-20')
    feed_date: Optional[date] = None
    time_attr = inner_cube.get("time")
    if time_attr:
        try:
            feed_date = date.fromisoformat(time_attr)
        except ValueError:
            logger.debug("[ECB] Geçersiz time attr: %s", time_attr)

    # Kur listesi — namespace'li önce, namespace'siz fallback.
    rate_nodes = inner_cube.findall("eurofxref:Cube", NAMESPACE)
    if not rate_nodes:
        rate_nodes = [
            c for c in inner_cube
            if c.tag.rsplit("}", 1)[-1] == "Cube"
        ]

    rates_by_code: Dict[str, Decimal] = {}
    for node in rate_nodes:
        code = (node.get("currency") or "").strip().upper()
        rate_raw = node.get("rate")
        if not code or not rate_raw:
            continue
        try:
            rate_val = Decimal(str(rate_raw))
        except (InvalidOperation, ValueError):
            logger.debug(
                "[ECB] Geçersiz rate değeri (%s=%s); atlandı.",
                code, rate_raw,
            )
            continue
        if rate_val > 0:
            rates_by_code[code] = rate_val

    # EUR'nun kendisi feed'de geçmez; base olduğu için 1.0 ekle.
    rates_by_code["EUR"] = Decimal("1")

    logger.info(
        "[ECB] Parse OK — feed_date=%s, para_birimi_sayısı=%d",
        feed_date, len(rates_by_code),
    )
    return feed_date, rates_by_code


# -----------------------------------------------------------------------------
# Matematiksel Dönüşüm Katmanı
# -----------------------------------------------------------------------------


def compute_usd_based_rates(
    rates_eur_base: Dict[str, Decimal],
    targets: Iterable[str],
) -> Dict[str, Decimal]:
    """
    ECB'nin EUR bazlı oranlarını USD bazlı oranlara çevirir.

    Formül:
        target_per_USD = target_per_EUR / USD_per_EUR

    Parametre
    ---------
    rates_eur_base:
        `parse_ecb_xml()` çıktısı. ör. `{'USD': 1.08, 'GBP': 0.85, ...}`
    targets:
        USD → ? yönünde üretilecek hedef para birimleri (ör. EUR, GBP, CHF).

    Dönüş
    -----
    `{'EUR': 0.9240, 'GBP': 0.7910, ...}` — 4 ondalık basamakta,
    tüm değerler Decimal.

    Notlar
    ------
    * USD hedeflere dahil edilse bile çıktıda yer ALMAZ (USD→USD=1
      anlamsız).
    * Kaynak oran eksikse o hedef sessizce atlanır (KeyError
      fırlatılmaz).
    * Sonuç oranı 0 veya negatifse o hedef atlanır.
    """
    usd_per_eur = rates_eur_base.get("USD")
    if not usd_per_eur or usd_per_eur <= 0:
        raise ECBFetchError(
            str(_("ECB feed içinde USD oranı bulunamadı."))
        )

    result: Dict[str, Decimal] = {}
    for raw_code in targets:
        code = (raw_code or "").strip().upper()
        if not code or code == "USD":
            continue
        target_per_eur = rates_eur_base.get(code)
        if not target_per_eur or target_per_eur <= 0:
            logger.debug(
                "[ECB] Hedef %s için EUR bazlı oran yok; atlandı.",
                code,
            )
            continue
        try:
            rate_usd = (target_per_eur / usd_per_eur).quantize(QUANTIZE_4)
        except (InvalidOperation, ValueError, ZeroDivisionError):
            continue
        if rate_usd > 0:
            result[code] = rate_usd

    logger.info(
        "[ECB] USD bazlı dönüşüm OK — hedef=%d, üretilen=%d",
        len(list(targets)) if not isinstance(targets, tuple) else len(targets),
        len(result),
    )
    return result


# -----------------------------------------------------------------------------
# Üst Seviye API (Tek Fonksiyonluk Kullanım)
# -----------------------------------------------------------------------------


def fetch_usd_based_rates(
    targets: Iterable[str],
    url: str = ECB_DAILY_URL,
) -> Tuple[Optional[date], Dict[str, Decimal]]:
    """
    Uçtan uca kolaylık fonksiyonu: ECB feed'ini indirir, parse eder,
    USD bazına çevirir ve sonucu döner.

    Dönüş: `(feed_date, usd_rates_dict)`

    Hata durumunda `ECBFetchError` fırlatır (çağıran try/except ile
    yutmalıdır).
    """
    xml_text = fetch_ecb_xml(url)
    feed_date, rates_eur_base = parse_ecb_xml(xml_text)
    usd_rates = compute_usd_based_rates(rates_eur_base, targets)
    return feed_date, usd_rates
