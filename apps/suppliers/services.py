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

from django.db import transaction
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

        # Tarih/saat bileşenleri. USE_TZ=False ortamında timezone.now()
        # naive datetime döndürür; localtime() kullanmıyoruz.
        now = timezone.now()
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

    # ========================================================================
    # FAZ 52 — TEDARİKÇİ AÇILIŞ CARİSİ (Opening Balance) (2026-05-05)
    # ========================================================================
    # Mağaza programa yeni geçtiğinde, mevcut tedarikçilerle olan tarihsel
    # bakiyeler (örn. 50 gr alacak, 1.500 ₺ borç) stok hareketsiz olarak
    # ledger'a girilir. Bu kayıtlar diğer işlem tiplerinden 'OB-' prefix'i
    # ile ayrılır:
    #   - Raporlamada "Açılış Bakiyesi" badge'i ile gösterilir.
    #   - book_supplier_tx içindeki FIFO auto-setoff filtresi
    #     (`.exclude(process_no__startswith='OB')`) tarafından muaf tutulur:
    #     normal akıştaki yeni işlemler bu satırı OTOMATİK kapatmaz, açılış
    #     denge kalemi tarihsel olarak sabit kalır. Mağaza bu satırı
    #     kapatmak isterse "Cari Düzelt" akışını kullanır (ADJ- yazımı).
    #
    # Mimariyi BOZMAYAN tasarım kararları:
    #   1) Yeni transaction_type EKLENMEDİ — mevcut ENTRY/EXIT kullanılır
    #      (yön sign ile belirtilir: receivable=EXIT, payable=ENTRY).
    #   2) Yeni model alanı YOK — yalnızca process_no prefix değişiyor.
    #   3) book_supplier_tx çağrılır (auto_setoff=False) — kendi yazma
    #      yolu yeniden icat edilmez.
    # ========================================================================

    OB_PREFIX = 'OB'

    @staticmethod
    def generate_opening_balance_process_no(supplier_id) -> str:
        """
        Tedarikçi açılış bakiyesi (opening balance) için process_no üretir.

        Format:
            OB-{YYYYMMDD}-{HHMMSS}-{supplier_short8}-{random4}

        Çakışma kontrolü, `generate_adjustment_process_no` ile aynı patern.
        Tek fark prefix ('OB-' vs 'ADJ-') ve dolayısıyla:
          - book_supplier_tx içindeki FIFO setoff filtresi bu kaydı atlar.
          - is_opening_balance_process_no() ile ayrıştırılır.
        """
        from apps.suppliers.models import SupplierLedger

        if supplier_id is None:
            supplier_short = 'unknown0'
        else:
            supplier_short = str(supplier_id).replace('-', '').lower()[:8]
            if len(supplier_short) < 8:
                supplier_short = supplier_short.ljust(8, '0')

        # USE_TZ=False ortamında timezone.now() naive datetime döndürür;
        # localtime() kullanmıyoruz.
        now = timezone.now()
        date_part = now.strftime('%Y%m%d')
        time_part = now.strftime('%H%M%S')

        for attempt in range(SupplierLedgerService._MAX_COLLISION_RETRIES):
            random_part = secrets.token_hex(2)
            candidate = (
                f'{SupplierLedgerService.OB_PREFIX}-'
                f'{date_part}-{time_part}-'
                f'{supplier_short}-{random_part}'
            )
            if not SupplierLedger.objects.filter(process_no=candidate).exists():
                logger.debug(
                    f"generate_opening_balance_process_no: "
                    f"supplier_id={supplier_id}, attempt={attempt + 1}, "
                    f"process_no={candidate}"
                )
                return candidate
            logger.warning(
                f"generate_opening_balance_process_no: çakışma "
                f"(attempt={attempt + 1}, candidate={candidate})."
            )

        fallback_suffix = uuid.uuid4().hex[:12]
        fallback = (
            f'{SupplierLedgerService.OB_PREFIX}-'
            f'{date_part}-{time_part}-'
            f'{supplier_short}-{fallback_suffix}'
        )
        logger.error(
            f"generate_opening_balance_process_no: retry tükendi, "
            f"UUID fallback uygulandı: {fallback}"
        )
        return fallback

    @staticmethod
    def is_opening_balance_process_no(process_no: str) -> bool:
        """
        process_no'nun açılış bakiyesi fişine ait olup olmadığını döndürür.
        UI ve raporlarda "Açılış Bakiyesi" badge'i için kullanılır.
        """
        if not process_no:
            return False
        return str(process_no).startswith(f'{SupplierLedgerService.OB_PREFIX}-')

    @staticmethod
    def write_opening_balance(
        *,
        supplier,
        entries,
        description: str,
        user=None,
        process_no: Optional[str] = None,
    ):
        """
        Tedarikçi için tek seferlik açılış cari kayıtlarını yazar.

        Multi-currency destekli: aynı 'OB-...' process_no altında birden çok
        para birimi için ayrı SupplierLedger satırları oluşturur. Tüm
        satırlar atomik bir transaction içinde yazılır (view tarafında
        @transaction.atomic dekoratörü zorunlu).

        Args:
            supplier: Suppliers instance (kilit alınmış olmalı — view
                      katmanında select_for_update ile alınır).
            entries:  Liste, her eleman dict:
                      {
                        'currency': 'HS' | 'TRY' | 'USD' | ...,
                        'direction': 'RECEIVABLE' | 'PAYABLE',
                                     # RECEIVABLE = biz alacaklıyız (EXIT)
                                     # PAYABLE    = biz borçluyuz (ENTRY)
                        'amount':   Decimal — pozitif değer,
                        'exchange_rate_eur': Decimal | None — fiat için 1
                                     birim = X TL (HS/HG'de None bırakılır),
                      }
            description: Kullanıcı açıklaması (ledger description'a yazılır).
            user:        İşlemi yapan kullanıcı (audit/log için, opsiyonel).
            process_no:  Önceden üretilmişse kullanılır; yoksa burada üretilir.

        Returns:
            dict:
              {
                'process_no': 'OB-...',
                'entries': [
                    {'currency', 'tx_type', 'amount', 'ledger_id'}, ...
                ],
                'entries_count': int,
              }

        Raises:
            ValueError: Geçersiz parametre veya boş entry listesi.

        Mimari Notlar:
            - book_supplier_tx çağrısında `auto_setoff=False` verilir:
              açılış kaydı normal işlemlerin karşı yönüyle FIFO mahsubuna
              GİRMEZ. Açılış denge kalemi olarak ayrı tutulur.
            - book_supplier_tx zaten 'OB-' prefix'li satırları FIFO'dan
              dışlar; ancak yeni yazılan açılış satırının kendisi de
              auto_setoff=True olsaydı, karşı yöndeki normal satırları
              kapatma riski olurdu. False ile kesin güvence.
        """
        from apps.suppliers.models import SupplierLedger
        from apps.process.wholesale_views import book_supplier_tx
        from decimal import Decimal as _D, InvalidOperation as _IO

        if not entries:
            raise ValueError('Açılış bakiyesi için en az bir satır gereklidir.')

        if process_no is None:
            process_no = SupplierLedgerService.generate_opening_balance_process_no(
                supplier.id
            )

        # Aynı tedarikçi için açılış bakiyesi sadece BİR KEZ girilebilir
        # — idempotent kontrol. Daha önce 'OB-' prefix'li kayıt varsa
        # yeni yazıma izin verilmez (mutabakat ADJ akışına yönlendirilir).
        existing = SupplierLedger.objects.filter(
            supplier=supplier,
            process_no__startswith=f'{SupplierLedgerService.OB_PREFIX}-',
        ).exists()
        if existing:
            raise ValueError(
                'Bu tedarikçi için açılış bakiyesi daha önce girilmiş. '
                'Düzeltme yapmak için "Cari Düzelt" akışını kullanın.'
            )

        result_entries = []
        ledger_description = f'[OPENING_BALANCE] {description}'

        for idx, raw in enumerate(entries):
            currency = (raw.get('currency') or '').strip().upper()
            direction = (raw.get('direction') or '').strip().upper()
            amount_raw = raw.get('amount')
            rate_raw = raw.get('exchange_rate_eur')

            if not currency:
                raise ValueError(f'Satır {idx + 1}: Para birimi boş.')
            if not is_valid_ledger_currency(currency):
                raise ValueError(
                    f'Satır {idx + 1}: Geçersiz para birimi "{currency}".'
                )
            if direction not in ('RECEIVABLE', 'PAYABLE'):
                raise ValueError(
                    f'Satır {idx + 1}: Yön "RECEIVABLE" veya "PAYABLE" olmalı.'
                )

            try:
                amount = _D(str(amount_raw))
            except (_IO, ValueError, TypeError):
                raise ValueError(
                    f'Satır {idx + 1}: Tutar geçersiz ({amount_raw!r}).'
                )
            if amount <= 0:
                raise ValueError(
                    f'Satır {idx + 1}: Tutar 0\'dan büyük olmalı.'
                )

            # Fiat birimler için kur (opsiyonel — None bırakılırsa SupplierLedger
            # NULL kabul eder; raporlama TL eşdeğerinden yoksun olur).
            exchange_rate = None
            if rate_raw not in (None, '', '0', 0):
                try:
                    exchange_rate = _D(str(rate_raw))
                    if exchange_rate <= 0:
                        exchange_rate = None
                except (_IO, ValueError, TypeError):
                    exchange_rate = None

            tx_type = 'EXIT' if direction == 'RECEIVABLE' else 'ENTRY'

            ledger = book_supplier_tx(
                supplier=supplier,
                transaction_type=tx_type,
                amount_value=amount,
                currency=currency,
                process_no=process_no,
                description=ledger_description,
                auto_setoff=False,  # Açılış kaydı FIFO'ya katılmaz.
            )

            if ledger is None:
                raise ValueError(
                    f'Satır {idx + 1}: Ledger yazılamadı (book_supplier_tx '
                    f'guard tetiklendi). currency={currency}, amount={amount}.'
                )

            # exchange_rate_eur alanını ayrıca güncelle (book_supplier_tx
            # bu parametreyi ileriye geçirmez; HS/HG dışındaki kayıtlar için
            # tarihsel TL eşdeğer raporlaması bu alana bağlıdır).
            if exchange_rate is not None:
                ledger.exchange_rate_eur = exchange_rate
                ledger.save(update_fields=['exchange_rate_eur'])

            result_entries.append({
                'currency':  currency,
                'tx_type':   tx_type,
                'direction': direction,
                'amount':    str(amount),
                'ledger_id': str(ledger.id),
                'exchange_rate_eur': str(exchange_rate) if exchange_rate else None,
            })

        logger.info(
            f"write_opening_balance: supplier={supplier.id}, "
            f"process_no={process_no}, entries={len(result_entries)}, "
            f"user={getattr(user, 'id', None)}"
        )

        return {
            'process_no': process_no,
            'entries': result_entries,
            'entries_count': len(result_entries),
        }


# ============================================================================
# FAZ TS-2 — SUPPLIER DELETE SERVICE (2026-04-29)
# ============================================================================
# FAZ TS-1'de SupplierLedger.supplier ve GoldPurchases.supplier on_delete
# davranışları CASCADE -> SET_NULL olarak güncellendi (şema güvenliği).
# Bu servis, mağaza UI'sından gelen "tedarikçi sil" akışını üstlenir:
#
#   1. Preflight kontrolleri (açık process bloku, uyarı bilgileri)
#   2. Atomik silme:
#        - Aktif SupplierLedger kayıtlarını is_active=False yap
#        - Suppliers.is_deleted=True, is_active=False yap
#   3. Audit log
#
# Korunan invariantlar:
#   - StockLedger'a dokunulmaz (zaten supplier FK yok).
#   - GoldPurchases satırları silinmez; supplier alanı SET_NULL ile NULL
#     olur (şema seviyesinde, FAZ TS-1).
#   - Process / Invoice / BarcodeTemplate'a dokunulmaz; supplier alanları
#     yine SET_NULL ile NULL olur.
#   - SupplierLedger satırları kalır (audit trail), sadece is_active=False
#     işaretlenir.
# ============================================================================


class SupplierDeleteBlocked(Exception):
    """
    Tedarikçi silme akışında bloklayıcı bir önkoşul ihlali tespit edildiğinde
    fırlatılır. View katmanı bu exception'u 409 Conflict ile döndürür.

    Attributes:
        errors: Bloklayan kontrollerin listesi (her biri dict).
                Örn: [{'code': 'OPEN_PROCESSES', 'supplier_id': '...',
                       'count': 3, 'message': '...'}]
        warnings: Bloklamayan ama dikkat gerektiren bilgilerin listesi.
                  `force=True` parametresiyle bypass edilebilir.
    """

    def __init__(self, errors=None, warnings=None):
        self.errors = errors or []
        self.warnings = warnings or []
        super().__init__(
            f"Supplier delete blocked: {len(self.errors)} error(s), "
            f"{len(self.warnings)} warning(s)."
        )


class SupplierDeleteService:
    """
    Tedarikçi/Çantacı silme akışı için servis katmanı.

    Kullanım:
        service = SupplierDeleteService(user=request.user)

        # 1) Önce preflight raporu (UI uyarı modalı için)
        report = service.preflight(supplier_ids=['uuid1', 'uuid2'])
        # report = {
        #     'blocking': [...],
        #     'warnings': [...],
        #     'summary': [...],
        # }

        # 2) Sonra atomik silme. Bloklayıcı varsa SupplierDeleteBlocked.
        result = service.execute(supplier_ids=['uuid1', 'uuid2'], force=False)
        # result = {
        #     'deleted_count': 2,
        #     'ledgers_deactivated': 17,
        #     'gold_purchases_anonymized': 4,
        #     'details': [...],
        # }

    Invariantlar:
        - StockLedger'a, Products'a, Process'e dokunulmaz.
        - GoldPurchases satırları SET_NULL ile anonim kalır (FAZ TS-1 şeması).
        - SupplierLedger satırları is_active=False ile pasifleştirilir;
          fiziksel silme yoktur (Immutable Ledger prensibi).
    """

    # Açık process durumları — bu durumlardaki bir Process tedarikçiye bağlıysa
    # silme bloklanır. Açık iş varken cariyi yok etmek muhasebe çelişkisi yaratır.
    OPEN_PROCESS_STATUSES = (
        'PENDING',
        'IN_PROGRESS',
        'OPEN_BINDING',
        'WAITING_STOCK',
    )

    def __init__(self, user):
        """
        Args:
            user: İşlemi gerçekleştiren kullanıcı (request.user). Audit log
                  ve store filtresi için kullanılır. None olamaz.
        """
        if user is None:
            raise ValueError("SupplierDeleteService requires a user (got None).")
        self.user = user

    # ------------------------------------------------------------------
    # PREFLIGHT — silmeden önce UI'ya rapor üretir
    # ------------------------------------------------------------------

    def preflight(self, supplier_ids):
        """
        Verilen tedarikçi ID'leri için silme öncesi durum raporu üretir.

        Hiçbir veriyi değiştirmez; sadece okuma sorguları yapar.

        Returns:
            dict:
                blocking  - silmeyi engelleyen problem listesi
                warnings  - bloklamayan ama dikkat gerektiren bilgiler
                summary   - tedarikçi başına özet (bakiye, kayıt sayıları)
        """
        from apps.suppliers.models import Suppliers, SupplierLedger
        from apps.process.models import Process
        from apps.gold_purchases.models import GoldPurchases

        store_id = getattr(self.user, 'store_id', None)
        suppliers = list(
            Suppliers.objects
            .filter(id__in=supplier_ids, store_id=store_id, is_deleted=False)
        )
        found_ids = {str(s.id) for s in suppliers}
        missing_ids = [sid for sid in supplier_ids if str(sid) not in found_ids]

        blocking = []
        warnings = []
        summary = []

        if missing_ids:
            blocking.append({
                'code': 'NOT_FOUND',
                'message': (
                    f"{len(missing_ids)} adet tedarikçi bulunamadı veya bu "
                    f"mağazaya ait değil."
                ),
                'supplier_ids': missing_ids,
            })

        for s in suppliers:
            # 1) Açık process kontrolü (bloklayıcı)
            open_processes = (
                Process.objects
                .filter(
                    supplier=s,
                    is_deleted=False,
                    is_status__in=self.OPEN_PROCESS_STATUSES,
                )
                .values_list('process_no', flat=True)[:10]
            )
            open_processes = list(open_processes)
            if open_processes:
                blocking.append({
                    'code': 'OPEN_PROCESSES',
                    'supplier_id': str(s.id),
                    'supplier_name': s.company_name,
                    'count': len(open_processes),
                    'sample_process_nos': open_processes,
                    'message': (
                        f"'{s.company_name}' tedarikçisinin {len(open_processes)} "
                        f"adet açık (tamamlanmamış) işlemi var. Önce kapatılmalı."
                    ),
                })

            # 2) Bakiye uyarısı (bloklamaz)
            try:
                balance = s.balance_summary() or {}
            except Exception:
                balance = {}
            non_zero_currencies = []
            for currency, row in balance.items():
                if not isinstance(row, dict):
                    continue
                net = row.get('net') or 0
                if net:
                    non_zero_currencies.append({
                        'currency': currency,
                        'net': str(net),
                    })
            if non_zero_currencies:
                warnings.append({
                    'code': 'NON_ZERO_BALANCE',
                    'supplier_id': str(s.id),
                    'supplier_name': s.company_name,
                    'balances': non_zero_currencies,
                    'message': (
                        f"'{s.company_name}' tedarikçisinin sıfırlanmamış "
                        f"bakiyesi var. Silme sonrası ilgili cari kayıtlar "
                        f"is_active=False olarak işaretlenecek."
                    ),
                })

            # 3) Anonimleşecek GoldPurchases sayısı (bilgilendirme)
            gp_count = GoldPurchases.objects.filter(
                supplier=s, is_deleted=False
            ).count()
            if gp_count:
                warnings.append({
                    'code': 'GOLD_PURCHASES_ANONYMIZE',
                    'supplier_id': str(s.id),
                    'supplier_name': s.company_name,
                    'count': gp_count,
                    'message': (
                        f"'{s.company_name}' tedarikçisine bağlı {gp_count} "
                        f"adet barkodlu alış kaydı korunacak ve tedarikçi "
                        f"alanı boş bırakılacak (anonimleştirme)."
                    ),
                })

            # 4) Aktif ledger sayısı (bilgilendirme)
            active_ledger_count = SupplierLedger.objects.filter(
                supplier=s, is_active=True
            ).count()

            summary.append({
                'supplier_id': str(s.id),
                'supplier_name': s.company_name,
                'account_type': s.account_type,
                'active_ledger_count': active_ledger_count,
                'gold_purchases_count': gp_count,
                'open_processes_count': len(open_processes),
                'balance_summary': {
                    cur: {k: str(v) for k, v in row.items()}
                    for cur, row in (balance or {}).items()
                    if isinstance(row, dict)
                },
            })

        return {
            'blocking': blocking,
            'warnings': warnings,
            'summary': summary,
        }

    # ------------------------------------------------------------------
    # EXECUTE — atomik silme
    # ------------------------------------------------------------------

    @transaction.atomic
    def execute(self, supplier_ids, force=False):
        """
        Verilen tedarikçileri atomik olarak siler.

        Akış (her tedarikçi için):
            1. Preflight çalıştırılır.
            2. Bloklayıcı varsa SupplierDeleteBlocked fırlatılır
               (force parametresi bloklayıcıları bypass ETMEZ; sadece
               warnings'i bypass eder).
            3. Aktif SupplierLedger kayıtları is_active=False yapılır.
            4. Suppliers kayıtları is_deleted=True, is_active=False yapılır.
            5. ActivityLog'a yazılır.

        Schema seviyesinde (FAZ TS-1) zaten:
            - SupplierLedger.supplier SET_NULL  (CASCADE değil)
            - GoldPurchases.supplier  SET_NULL  (CASCADE değil)
        olduğu için fiziksel DELETE yapılmasa dahi, ileride yapılırsa
        veri kaybı yaşanmaz. Bu servis fiziksel DELETE kullanmaz; soft-delete
        ile audit trail'i korur.

        Args:
            supplier_ids: Silinecek tedarikçi ID'leri.
            force: True ise warnings (bakiye, anonimleşecek GP) bypass edilir.
                   Bloklayıcı (OPEN_PROCESSES, NOT_FOUND) yine engellenir.

        Returns:
            dict:
                deleted_count            - kaç tedarikçi soft-delete edildi
                ledgers_deactivated      - kaç SupplierLedger is_active=False yapıldı
                gold_purchases_count     - kaç GoldPurchases satırı etkilendi (bilgi)
                details                  - tedarikçi başına detay listesi

        Raises:
            SupplierDeleteBlocked: Bloklayıcı önkoşul varsa veya force=False
                                   iken warnings doluysa.
        """
        from apps.suppliers.models import Suppliers, SupplierLedger
        from apps.gold_purchases.models import GoldPurchases
        from apps.activity_logs.views import write_log

        report = self.preflight(supplier_ids)

        # Bloklayıcı problemler her durumda durdurur.
        if report['blocking']:
            raise SupplierDeleteBlocked(
                errors=report['blocking'],
                warnings=report['warnings'],
            )

        # Warnings (bakiye / anonimleşme) sadece force=False iken durdurur.
        if report['warnings'] and not force:
            raise SupplierDeleteBlocked(
                errors=[],
                warnings=report['warnings'],
            )

        store_id = getattr(self.user, 'store_id', None)

        # Snapshot al — döngü sonrasında is_deleted=True olduğu için aynı
        # filtreyi yeniden kullanırsak count() 0 döner. Bu yüzden listeye alıyoruz.
        suppliers_to_delete = list(
            Suppliers.objects.filter(
                id__in=supplier_ids,
                store_id=store_id,
                is_deleted=False,
            )
        )

        details = []
        total_ledgers_deactivated = 0
        total_gp_affected = 0

        # Tek tek dönerek her tedarikçi için audit detayı topluyoruz.
        # Toplu update yerine bireysel akış: log granülaritesi için.
        for s in suppliers_to_delete:
            ledger_count_before = SupplierLedger.objects.filter(
                supplier=s, is_active=True
            ).count()
            gp_count = GoldPurchases.objects.filter(
                supplier=s, is_deleted=False
            ).count()

            # 1) Aktif ledger kayıtlarını pasifleştir (Immutable Ledger:
            #    fiziksel silme yok, sadece is_active=False).
            ledgers_deactivated = SupplierLedger.objects.filter(
                supplier=s, is_active=True
            ).update(is_active=False)

            # 2) Tedarikçinin kendisini soft-delete et.
            #    GoldPurchases.supplier ve diğer SET_NULL FK'ları,
            #    fiziksel DELETE yapılmadığı için NULL'a dönmez. Bu
            #    soft-delete pattern'ı, mevcut "is_deleted=False" filtreli
            #    sorgular sayesinde silinmiş gibi davranır. Şema güvencesi
            #    (FAZ TS-1) ileride fiziksel DELETE yapılması gerekirse
            #    GoldPurchases'ı korumayı garanti eder.
            Suppliers.objects.filter(pk=s.pk).update(
                is_deleted=True,
                is_active=False,
            )

            details.append({
                'supplier_id': str(s.id),
                'supplier_name': s.company_name,
                'account_type': s.account_type,
                'ledgers_deactivated': ledgers_deactivated,
                'ledgers_active_before': ledger_count_before,
                'gold_purchases_count': gp_count,
            })
            total_ledgers_deactivated += ledgers_deactivated
            total_gp_affected += gp_count

            # 3) Audit log — request objesi servis seviyesinde elimizde
            #    olmadığı için write_log için minimal bir proxy yeterli;
            #    ancak mevcut write_log API'si request bekliyor. View
            #    katmanında log atılır; servis kendisi log atmaz.

        return {
            'deleted_count': len(suppliers_to_delete),
            'ledgers_deactivated': total_ledgers_deactivated,
            'gold_purchases_count': total_gp_affected,
            'details': details,
        }


# ============================================================================
# FAZ 51 (R-02) — SUPPLIER LEDGER REVERSAL HELPERS
# ============================================================================
#
# Mimari: Append-Only "audit-shadow" REVERSAL.
#
# Eski davranış: cancel_row / cancel_scrap_purchase / cancel_bracelet_purchase
# vb. iptal akışları aktif SupplierLedger satırlarını
# `update(is_active=False)` ile pasifleştiriyordu. Bu yeterli matematik
# bütünlüğü sağlıyor (balance_summary is_active=True topluyor) ancak
# audit zinciri yok: kim, ne zaman, hangi gerekçeyle iptal etti?
#
# Yeni helper'lar bu boşluğu MEVCUT DAVRANIŞI KORUYARAK kapatır:
#   - Orijinal kayıt is_active=False yapılır + reversed_by/reversed_at/
#     reverse_reason doldurulur (audit alanları).
#   - Aynı zamanda yeni bir REVERSAL satırı (transaction_type='REVRS')
#     parent FK ile yazılır → orijinal satıra bağlanır. Bu satır da
#     `is_active=False` ile yazılır → balance_summary'ye girmez (bakiye
#     değişmez), sadece `transaction_type='REVRS'` filtreleyen audit
#     sorguları görür.
#
# İdempotent: aynı orijinal için zaten REVERSAL varsa (parent eşitliği)
# tekrar yazılmaz. Caller'lar güvenle birden fazla kez çağırabilir.
#
# Bakiye davranışı: HİÇ DEĞİŞMEZ. Mevcut raporlar/ekranlar ek satırı
# görmez. Yalnız denetim sorguları + ileri faz REVERSAL pattern bunu
# kullanır.
# ============================================================================

def write_supplier_reversal(*, original, audit: dict, reason: str):
    """Tek bir SupplierLedger satırını append-only REVERSAL ile iptal eder.

    İdempotent: zaten REVERSAL'i olan kayıt için no-op döner.

    Args:
        original: SupplierLedger instance (transaction_type ENTRY veya EXIT).
        audit: extract_audit_context çıktısı (actor + ip + user_agent dict).
        reason: İptal nedeni (zorunlu — boşsa "Manuel iptal" set edilir).

    Returns:
        Yazılmış REVERSAL SupplierLedger instance veya None (idempotent skip).
    """
    from apps.suppliers.models import SupplierLedger

    if original is None:
        return None
    if original.transaction_type == SupplierLedger.REVERSAL:
        return None

    reason = (reason or 'Manuel iptal')[:255]
    actor = audit.get('actor') if audit else None

    # İdempotency — aynı orijinal için aktif/pasif fark etmeksizin REVERSAL var mı?
    existing = SupplierLedger.objects.filter(
        parent=original,
        transaction_type=SupplierLedger.REVERSAL,
    ).first()
    if existing:
        return existing

    # Orijinali pasifleştir + audit alanlarını doldur (mevcut davranış + audit).
    # SADECE is_active=True iken bu güncelleme anlamlı; pasifse audit alanlarını
    # yine de doldurmaya çalış (ama caller önce kontrol etti varsayıyoruz).
    update_fields = []
    if original.is_active:
        original.is_active = False
        update_fields.append('is_active')
    if original.reversed_by_id is None and actor is not None:
        original.reversed_by = actor
        update_fields.append('reversed_by')
    if original.reversed_at is None:
        original.reversed_at = timezone.now()
        update_fields.append('reversed_at')
    if not (original.reverse_reason or '').strip():
        original.reverse_reason = reason
        update_fields.append('reverse_reason')
    if update_fields:
        try:
            original.save(update_fields=update_fields)
        except Exception:
            # Defensive: eski şemada reversed_* alanı yoksa save'i sade yap
            try:
                original.is_active = False
                original.save(update_fields=['is_active'])
            except Exception:
                pass

    # REVERSAL satırı yaz (is_active=False → balance toplamına girmez).
    rev = SupplierLedger.objects.create(
        supplier=original.supplier,
        product=original.product,
        transaction_type=SupplierLedger.REVERSAL,
        cantaci_tx_type=original.cantaci_tx_type,
        quantity_piece=original.quantity_piece,
        quantity_gram=original.quantity_gram,
        description=f'İPTAL: {reason}',
        amount_value=original.amount_value,
        currency=original.currency,
        exchange_rate_eur=original.exchange_rate_eur,
        process_no=original.process_no,
        source_process_id=original.source_process_id,
        is_active=False,  # bakiyeyi etkilemez; sadece audit izi
        parent=original,
        reversal_target_type=original.transaction_type,
        reversed_by=actor,
        reversed_at=timezone.now(),
        reverse_reason=reason,
    )
    return rev


def reverse_supplier_ledger_for_process(
    *, audit: dict, reason: str,
    process_id=None, process_no: str = None,
):
    """Bir process'e ait aktif SupplierLedger satırlarını append-only iptal eder.

    İki kanal:
      1) `process_id` verilirse → `source_process_id=process_id` ile filtre
         (FAZ 21 satır-bazlı Bug 2B çözümüyle uyumlu).
      2) `process_no` verilirse → `process_no=...` ile filtre (legacy fallback).

    Her iki kanaldan gelen aktif satırlar `write_supplier_reversal` ile
    geri sarılır. Caller her iki parametreyi de geçebilir; servis çift-iptal
    koruması (existing parent kontrolü) ile idempotenttir.

    Args:
        audit: extract_audit_context çıktısı.
        reason: İptal nedeni (zorunlu).
        process_id: UUID — Process satırı id'si.
        process_no: str — Process.process_no.

    Returns:
        dict: {reversed_count, skipped_count}.
    """
    from apps.suppliers.models import SupplierLedger

    reversed_count = 0
    skipped_count = 0

    qs_filters = []
    if process_id is not None:
        qs_filters.append({'source_process_id': process_id, 'is_active': True})
    if process_no:
        qs_filters.append({'process_no': process_no, 'is_active': True})

    if not qs_filters:
        return {'reversed_count': 0, 'skipped_count': 0}

    # Birleşik queryset — set ile dedupe
    seen_ids = set()
    rows = []
    for f in qs_filters:
        for r in SupplierLedger.objects.filter(**f).exclude(
            transaction_type=SupplierLedger.REVERSAL,
        ):
            if r.pk in seen_ids:
                continue
            seen_ids.add(r.pk)
            rows.append(r)

    for r in rows:
        try:
            res = write_supplier_reversal(
                original=r, audit=audit, reason=reason,
            )
            if res is None:
                skipped_count += 1
            else:
                reversed_count += 1
        except Exception:
            logger.exception(
                "reverse_supplier_ledger_for_process: REVERSAL atlandı SL #%s",
                r.pk,
            )
            skipped_count += 1

    return {'reversed_count': reversed_count, 'skipped_count': skipped_count}
