"""
Supplier Services — Cari Ledger Servis Katmanı
================================================================================

FAZ B: Çoklu Maden/Ürün entegrasyonu için ledger servisleri.

Bu modül, SupplierLedger kayıtlarında hangi para biriminin kullanılacağına
karar veren "Currency Router" fonksiyonlarını içerir. Amaç, material_type
bazında doğru cari birimini otomatik olarak seçerek business logic'i
tek noktadan yönetmektir.

Kural Özeti:
    GOLD    -> HS (Has Altın gram)
    SILVER  -> HG (Has Gümüş gram)
    WATCH   -> fiat (TRY/USD/EUR - caller belirler)
    DIAMOND -> fiat (TRY/USD/EUR - caller belirler)

Böylece SupplierLedger.currency alanında veri bütünlüğünü veritabanı
constraint'i yerine servis katmanı garanti eder (Zero Migration Risk).

FAZ 11 / BL-02: SupplierLedgerService sınıfı eklendi. Cari düzeltme
(adjustment) fişleri için standart process_no üretici ve prefix kontrol
yardımcı metotları içerir. Immutable Ledger mimarisine uygun; hiçbir
mevcut kaydı güncellemez/silmez.
"""

import logging
import secrets
import uuid
from typing import Optional

from django.utils import timezone

from apps.suppliers.models import LedgerCurrencyChoices

logger = logging.getLogger('suppliers')


# ============================================================================
# MATERYAL -> LEDGER PARA BİRİMİ EŞLEŞTİRME SABİTLERİ
# ============================================================================

# Metal bazlı ürünlerin varsayılan ledger birimleri.
MATERIAL_METAL_CURRENCY_MAP = {
    'GOLD':   LedgerCurrencyChoices.HS.value,   # Has Altın
    'SILVER': LedgerCurrencyChoices.HG.value,   # Has Gümüş
}

# Fiat bazlı ürünler (adet bazlı, metal kavramı yok)
FIAT_BASED_MATERIALS = {'WATCH', 'DIAMOND'}

# Geçerli fiat birimleri (WATCH/DIAMOND için)
VALID_FIAT_CURRENCIES = {
    LedgerCurrencyChoices.TRY.value,
    LedgerCurrencyChoices.USD.value,
    LedgerCurrencyChoices.EUR.value,
    LedgerCurrencyChoices.GBP.value,
    LedgerCurrencyChoices.CAD.value,
    LedgerCurrencyChoices.QAR.value,
}


# ============================================================================
# ANA FONKSİYON: Currency Router
# ============================================================================

def get_ledger_currency(product, fiat_currency: str = 'TRY') -> str:
    """
    Bir ürünün SupplierLedger/CustomerLedger kaydında kullanılacak
    para birimini belirler.

    Mantık:
        product.material_type == 'GOLD'    -> 'HS'
        product.material_type == 'SILVER'  -> 'HG'
        product.material_type == 'WATCH'   -> fiat_currency (örn: TRY, USD)
        product.material_type == 'DIAMOND' -> fiat_currency (örn: TRY, USD)

    Args:
        product: Products model instance. material_type alanı okunur.
                 None veya material_type yoksa güvenli default olarak
                 'GOLD' kabul edilir (geriye dönük uyumluluk).
        fiat_currency: WATCH/DIAMOND için kullanılacak fiat birim kodu.
                       Geçersiz değer verilirse ValueError fırlatır.
                       Default: 'TRY'.

    Returns:
        str: LedgerCurrencyChoices değerlerinden biri ('HS', 'HG', 'TRY' vb.)

    Raises:
        ValueError: Geçersiz fiat_currency verilirse (WATCH/DIAMOND için).

    Örnekler:
        >>> get_ledger_currency(altin_urunu)
        'HS'
        >>> get_ledger_currency(gumus_urunu)
        'HG'
        >>> get_ledger_currency(saat_urunu, fiat_currency='USD')
        'USD'
        >>> get_ledger_currency(pirlanta_urunu, fiat_currency='EUR')
        'EUR'
        >>> get_ledger_currency(pirlanta_urunu, fiat_currency='XXX')
        ValueError: Geçersiz fiat_currency 'XXX' ...

    NOT: Bu fonksiyon SupplierLedger.currency alanına YAZILACAK değeri üretir.
         SupplierLedger modelinde hâlâ choices=... yoktur; veri bütünlüğünü
         bu fonksiyon garanti eder.
    """
    # Materyal tipini güvenli şekilde al (None veya eksik alan koruması)
    mat_type = getattr(product, 'material_type', None) or 'GOLD'
    mat_type = str(mat_type).upper()

    # 1) Metal bazlı ürünler (GOLD, SILVER) -> sabit metal birimi
    if mat_type in MATERIAL_METAL_CURRENCY_MAP:
        currency = MATERIAL_METAL_CURRENCY_MAP[mat_type]
        logger.debug(
            f"get_ledger_currency: product={getattr(product, 'id', None)}, "
            f"material_type={mat_type} -> {currency}"
        )
        return currency

    # 2) Fiat bazlı ürünler (WATCH, DIAMOND) -> caller'ın verdiği fiat birim
    if mat_type in FIAT_BASED_MATERIALS:
        normalized = (fiat_currency or 'TRY').upper()
        if normalized not in VALID_FIAT_CURRENCIES:
            raise ValueError(
                f"Geçersiz fiat_currency '{fiat_currency}'. "
                f"material_type={mat_type} ürünleri için izin verilen "
                f"birimler: {sorted(VALID_FIAT_CURRENCIES)}"
            )
        logger.debug(
            f"get_ledger_currency: product={getattr(product, 'id', None)}, "
            f"material_type={mat_type}, fiat={normalized} -> {normalized}"
        )
        return normalized

    # 3) Tanınmayan material_type -> güvenli default (HS, geriye dönük uyum)
    logger.warning(
        f"get_ledger_currency: bilinmeyen material_type='{mat_type}' "
        f"(product={getattr(product, 'id', None)}). "
        f"Güvenli default olarak 'HS' döndürülüyor."
    )
    return LedgerCurrencyChoices.HS.value


def is_metal_based_currency(currency: str) -> bool:
    """
    Verilen ledger para biriminin metal bazlı (gram cinsinden) olup
    olmadığını döndürür.

    Args:
        currency: 'HS', 'HG', 'TRY' vb.

    Returns:
        True  -> HS veya HG
        False -> fiat (TRY, USD, EUR, vb.)
    """
    return (currency or '').upper() in {
        LedgerCurrencyChoices.HS.value,
        LedgerCurrencyChoices.HG.value,
    }


def is_valid_ledger_currency(currency: str) -> bool:
    """
    Verilen para biriminin LedgerCurrencyChoices enum'unda tanımlı
    olup olmadığını kontrol eder.

    ÖNEMLİ: Bu fonksiyon yalnızca doğrulama amaçlıdır. SupplierLedger.currency
    alanı hâlâ choices constraint'i içermez. İş akışları verinin kaynağında
    bu doğrulamayı çağırabilir.
    """
    if not currency:
        return False
    return str(currency).upper() in {c.value for c in LedgerCurrencyChoices}


# ============================================================================
# FAZ 11 / BL-02 — SUPPLIER LEDGER SERVICE (2026-04-24)
# ============================================================================
# Cari düzeltme (adjustment) fişleri için standart process_no üretici.
# BL-01 (Cari Sıfırlama) iş akışı bu servisi çağırarak Immutable Ledger'a
# yeni bir Adjustment Entry yazar — mevcut kayıtlar asla UPDATE/DELETE
# edilmez. process_no prefix'i ile adjustment kayıtları diğer işlem
# tiplerinden (OB, C, P, WHL) ayırt edilebilir.
# ============================================================================

class SupplierLedgerService:
    """
    Tedarikçi cari ledger işlemleri için servis katmanı.

    Immutable Ledger prensibi:
        SupplierLedger kayıtları asla güncellenmez/silinmez. Her düzeltme
        veya sıfırlama, ters yönde yeni bir Adjustment Entry oluşturarak
        yapılır. Bu servis, bu düzeltme işlemleri için standart process_no
        üretici sağlar.

    Kullanım:
        from apps.suppliers.services import SupplierLedgerService

        pno = SupplierLedgerService.generate_adjustment_process_no(
            supplier_id=supplier.id
        )
        # pno örneği: 'ADJ-20260424-143512-a1b2c3d4-9f8e'

        if SupplierLedgerService.is_adjustment_process_no(pno):
            # Bu kayıt bir düzeltme fişidir — rapor/ekstrede özel
            # etiketlenir.
            ...
    """

    # Düzeltme fişleri için standart prefix.
    # BL-01'de SupplierLedger.process_no bu prefix ile başlayan kayıtlar
    # "Mutabakat Düzeltmesi" olarak raporlanacaktır.
    ADJ_PREFIX = 'ADJ'

    # Çakışma önleme için retry limiti. Random4 hex (65536 olasılık) +
    # saniye precision + supplier_short8 sayesinde pratikte 1. denemede
    # unique sonuç alınır; bu sadece astronomik kenar durumlar için.
    _MAX_COLLISION_RETRIES = 5

    @staticmethod
    def generate_adjustment_process_no(supplier_id) -> str:
        """
        Tedarikçi cari düzeltme (adjustment) işlemi için standart,
        çakışma riski pratikte sıfır olan bir process_no üretir.

        Format:
            ADJ-{YYYYMMDD}-{HHMMSS}-{supplier_short8}-{random4}

        Bileşenler:
            YYYYMMDD        — İşlem tarihi (yerel saat dilimi)
            HHMMSS          — İşlem saati (saniye precision)
            supplier_short8 — Supplier UUID'nin ilk 8 hex karakteri
                              (aynı tedarikçiye ait kayıtların ekstrede
                              görsel olarak eşleşmesini kolaylaştırır)
            random4         — 4 hex karakter (16^4 = 65.536 olasılık;
                              aynı saniyede aynı tedarikçiye çift yazım
                              çakışmasına karşı güvence)

        Örnek çıktı:
            'ADJ-20260424-143512-a1b2c3d4-9f8e'

        Uzunluk: 33 karakter (SupplierLedger.process_no max_length=50).

        Çakışma Koruması:
            Üretilen process_no'nun veritabanında mevcut olup olmadığı
            kontrol edilir; çakışma varsa random4 kısmı yeniden üretilerek
            {_MAX_COLLISION_RETRIES} kez denenir. Hepsi başarısız olursa
            (astronomik olasılık) UUID tabanlı fallback uygulanır.

        Args:
            supplier_id: Suppliers.id — UUID instance veya str olabilir.
                         None verilirse 'unknown0' placeholder kullanılır
                         (cari dışı düzeltme senaryosu — pratikte beklenmez
                         ama defensive).

        Returns:
            str: ADJ- prefix ile başlayan, global unique process_no.

        Örnek Kullanım (BL-01):
            pno = SupplierLedgerService.generate_adjustment_process_no(
                supplier.id
            )
            book_supplier_tx(
                supplier=supplier,
                transaction_type='ENTRY',  # Ters yönde kapatma
                amount_value=net_balance,
                currency='HS',
                process_no=pno,
                description=f'Mutabakat düzeltmesi: {user_note}',
                auto_setoff=True,
            )

        NOT:
            Döngüsel import'u engellemek için SupplierLedger lazy import
            edilir (fonksiyon içinde).
        """
        # Lazy import — apps.suppliers.models servisle aynı pakette ancak
        # modül yükleme sırasında yan etkilerden kaçınmak için içe alıyoruz.
        from apps.suppliers.models import SupplierLedger

        # Supplier ID'yi 8 hex karaktere indir (UUID veya str için).
        if supplier_id is None:
            supplier_short = 'unknown0'
        else:
            supplier_short = str(supplier_id).replace('-', '').lower()[:8]
            # Çok kısa ID gelirse (test/mock) sağdan sıfır doldur.
            if len(supplier_short) < 8:
                supplier_short = supplier_short.ljust(8, '0')

        # Tarih/saat bileşenleri — yerel saat dilimi.
        now = timezone.localtime(timezone.now())
        date_part = now.strftime('%Y%m%d')
        time_part = now.strftime('%H%M%S')

        # Çakışma kontrollü üretim döngüsü.
        for attempt in range(SupplierLedgerService._MAX_COLLISION_RETRIES):
            random_part = secrets.token_hex(2)  # 4 hex karakter
            candidate = (
                f'{SupplierLedgerService.ADJ_PREFIX}-'
                f'{date_part}-{time_part}-'
                f'{supplier_short}-{random_part}'
            )

            # Çakışma kontrolü — SupplierLedger.process_no tekil olmasa da
            # adjustment kayıtlarının unique olması iş mantığı gereğidir.
            if not SupplierLedger.objects.filter(process_no=candidate).exists():
                logger.debug(
                    f"generate_adjustment_process_no: "
                    f"supplier_id={supplier_id}, attempt={attempt + 1}, "
                    f"process_no={candidate}"
                )
                return candidate

            logger.warning(
                f"generate_adjustment_process_no: process_no çakışması "
                f"(attempt={attempt + 1}, candidate={candidate}). "
                f"Yeniden deneniyor."
            )

        # Astronomik kenar durumu — UUID fallback.
        fallback_suffix = uuid.uuid4().hex[:12]
        fallback = (
            f'{SupplierLedgerService.ADJ_PREFIX}-'
            f'{date_part}-{time_part}-'
            f'{supplier_short}-{fallback_suffix}'
        )
        logger.error(
            f"generate_adjustment_process_no: {SupplierLedgerService._MAX_COLLISION_RETRIES} "
            f"denemede çakışma çözülemedi. UUID fallback uygulandı: {fallback}"
        )
        return fallback

    @staticmethod
    def is_adjustment_process_no(process_no: str) -> bool:
        """
        Verilen process_no'nun bir cari düzeltme (adjustment) fişine ait
        olup olmadığını döndürür.

        BL-04 kullanımı:
            PDF ekstresi ve tedarikçi detay sayfasında bu metodla
            işlem satırları "Mutabakat Düzeltmesi" badge'i ile gösterilir.

        Args:
            process_no: Kontrol edilecek process numarası.

        Returns:
            True  -> process_no 'ADJ-' ile başlıyor.
            False -> diğer tüm durumlar (None, boş string, farklı prefix).
        """
        if not process_no:
            return False
        return str(process_no).startswith(f'{SupplierLedgerService.ADJ_PREFIX}-')
