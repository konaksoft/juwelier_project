import json
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum, IntegerField, DecimalField  # Sum hala _process_payments_and_balances için lazım
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone

# Uygulama içi modeller
from apps.products.models import Products
from apps.customers.models import Customers
from apps.process.models import Process, Payment
# PavoTerminal, PavoLocalSale kaldırıldı — pavo app Juwelier Plus'ta yok
from apps.settings.models import StoreConfiguration

# Yardımcı Modüller
from apps.process.notifications import trigger_transaction_notifications, fmt_tl
from apps.process.views import generate_process_no, update_product_stock
from apps.banking.services import (
    PaymentBankAccountValidator,
    POSCommissionService,
    FXBalanceGuard,
    FXBalanceReader,
    InsufficientFXBalanceError,
    get_currency_code_from_product,
    detect_currency_from_name,
    PRODUCT_NAME_FROM_CURRENCY,
    SUPPORTED_FX_CURRENCIES,
)

# --- FAZ 3: StockSnapshot ve PriceService entegrasyonu ---
from apps.stock_management.models import StockSnapshot
from apps.stock_management.services.price_service import PriceService

# pavo_process ve invoice_process kaldırıldı — pavo/invoices app Juwelier Plus'ta yok
from apps.process.invoice_process import create_invoice_from_process  # stub

log = logging.getLogger(__name__)


# --- YARDIMCI VE DÖNÜŞÜM FONKSİYONLARI ---

def _dec(x, q='0.01'):
    """Güvenli Decimal dönüşümü."""
    try:
        return Decimal(str(x)).quantize(Decimal(q), rounding=ROUND_HALF_UP)
    except:
        return Decimal('0.00')


def _get_store_config(user):
    """Mağaza konfigürasyonunu getirir."""
    store = getattr(user, 'store', None)
    if not store:
        return None
    config, _ = StoreConfiguration.objects.get_or_create(store=store)
    return config


# --- FAZ 17: DÖVİZ KURU YARDIMCI FONKSİYONLARI ---

# Döviz kodu → Ürün adı eşlemesi.
# Bu ürünlerin sale_price_eur değeri, 1 birim döviz = X TL kurunu verir.
# Faz 13: SSOT — apps.banking.services.PRODUCT_NAME_FROM_CURRENCY üzerinden okunur.
# Yeni döviz eklemek yalnızca services.CURRENCY_FROM_PRODUCT_NAME güncellemesi gerektirir.
_CURRENCY_PRODUCT_MAP = dict(PRODUCT_NAME_FROM_CURRENCY)


def _get_exchange_rate_for_currency(currency_code):
    """
    Verilen döviz kodu için güncel kuru Products tablosundan çeker.

    Döviz ürünlerinin (USDTRY, EURTRY vb.) sale_price_eur değeri
    1 birim döviz = X TL kurunu temsil eder.

    Args:
        currency_code: 'USD', 'EUR', 'GBP' vb.

    Returns:
        Decimal kur değeri veya None (kur bulunamazsa).
        TRY için Decimal('1') döner.
    """
    if not currency_code or currency_code == 'TRY':
        return Decimal('1')

    product_name_key = _CURRENCY_PRODUCT_MAP.get(currency_code.upper())
    if not product_name_key:
        log.warning("FAZ17: Bilinmeyen döviz kodu: %s — kur bulunamadı.", currency_code)
        return None

    prod = Products.objects.filter(name__icontains=product_name_key).first()
    if prod and prod.sale_price_eur and prod.sale_price_eur > 0:
        return Decimal(str(prod.sale_price_eur))

    log.warning(
        "FAZ17: %s ürünü bulunamadı veya sale_price_eur=0. "
        "Döviz kuru hesaplanamıyor.", product_name_key,
    )
    return None


def _build_currency_extra(bank_account, amount_eur):
    """
    Eğer banka hesabının para birimi TRY değilse,
    Payment kaydına eklenecek currency_amount ve exchange_rate dict'ini döndürür.

    Args:
        bank_account: BankAccount instance (veya None)
        amount_eur: Decimal — TL cinsinden ödeme tutarı

    Returns:
        dict — {'currency_amount': Decimal, 'exchange_rate': Decimal} veya {}
    """
    if not bank_account:
        return {}

    acct_currency = getattr(bank_account, 'currency', 'TRY') or 'TRY'
    if acct_currency == 'TRY':
        return {}

    rate = _get_exchange_rate_for_currency(acct_currency)
    if not rate or rate <= 0:
        log.warning(
            "FAZ17: Döviz kuru bulunamadı (hesap=%s, currency=%s). "
            "currency_amount NULL kalacak.",
            bank_account.name, acct_currency,
        )
        return {}

    currency_amount = (amount_eur / rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {
        'currency_amount': currency_amount,
        'exchange_rate': rate,
    }


# --- FAZ 18: AKILLI KASA YÖNLENDİRME + ÇİFT TARAFLI DÖVİZ + ONAY ---

def _resolve_or_create_cash_account(store, currency='TRY'):
    """
    FAZ 20 — Akıllı Kasa Yönlendirmesi (Merkez Döviz Kasası Mimarisi).

    TRY için:
        0 hesap  → Otomatik "Merkez TRY Nakit Kasası" oluştur
        1 hesap  → Doğrudan döndür
        2+ hesap → None döndür (caller'ın seçim sorması gerek)

    Döviz (USD, EUR, GBP vb.) için:
        Tüm dövizler TEK bir "Merkez Döviz Kasası"na yönlendirilir.
        Bu kasa currency='FX' ile işaretlenir.
        Payment.currency_amount ve Payment.exchange_rate alanları
        her kaydın gerçek para birimini takip eder.

    Args:
        store: Stores instance
        currency: 'TRY', 'USD', 'EUR' vb.

    Returns:
        (BankAccount | None, int)  → (hesap, toplam_hesap_sayısı)
    """
    from apps.banking.models import BankAccount

    if currency == 'TRY':
        # TRY için standart kasa araması
        qs = BankAccount.objects.filter(
            store=store,
            account_type='CASH',
            currency='TRY',
            is_active=True,
            is_deleted=False,
        )
        count = qs.count()

        if count == 0:
            new_account = BankAccount.objects.create(
                store=store,
                name='Merkez TRY Nakit Kasası',
                account_type='CASH',
                currency='TRY',
                is_active=True,
            )
            log.info(
                "FAZ20: TRY kasa oluşturuldu — store=%s account_id=%s",
                store, new_account.id,
            )
            return new_account, 1

        if count == 1:
            return qs.first(), 1
        return None, count

    # Döviz (TRY harici): Merkez Döviz Kasası
    # Tüm döviz türleri tek bir kasada toplanır (currency='FX')
    fx_qs = BankAccount.objects.filter(
        store=store,
        account_type='CASH',
        currency='FX',
        is_active=True,
        is_deleted=False,
    )
    fx_count = fx_qs.count()

    if fx_count == 0:
        new_account = BankAccount.objects.create(
            store=store,
            name='Merkez Döviz Kasası',
            account_type='CASH',
            currency='FX',
            is_active=True,
        )
        log.info(
            "FAZ20: Merkez Döviz Kasası oluşturuldu — store=%s account_id=%s",
            store, new_account.id,
        )
        return new_account, 1

    if fx_count == 1:
        return fx_qs.first(), 1

    # 2+ Merkez Döviz Kasası: kullanıcıya seçim sorulmalı (nadir durum)
    return None, fx_count


def _get_approval_status(user):
    """
    FAZ 18 — Onaylı Kasa durumunu kontrol eder.

    Returns:
        bool: True ise yeni Payment kayıtları is_approved=False olarak oluşturulacak.
              False ise doğrudan is_approved=True (mevcut davranış).
    """
    store = getattr(user, 'store', None)
    if not store:
        return False
    try:
        config = store.config
        return bool(config.is_safe_approval_required)
    except Exception:
        return False


def _process_currency_exchange(
    process_no, product, qty, unit_price, total_amount_eur,
    operation_type, user, needs_approval,
    try_bank_account_id=None, fx_bank_account_id=None,
):
    """
    FAZ 18 — Döviz Bozma: Çift Taraflı (Double-Entry) Kasa İşlemi.

    FAZ 20.1: `try_bank_account_id` ve `fx_bank_account_id` opsiyonel parametreleri
    frontend'de kullanıcının UI'dan seçtiği kasa ID'lerini taşır.
    Verilirse o kasa doğrudan kullanılır; verilmezse 0-1-N kuralıyla auto-resolve edilir.

    Bir döviz ürünü (is_currency=True, ör. USDTRY) işlem gördüğünde:
    - PURCHASE (Döviz Alışı — müşteriden USD alıyoruz):
        • USD kasasına +qty GİRİŞ (currency_amount=qty, amount=total_amount_eur)
        • TRY kasasından -total_amount_eur ÇIKIŞ
    - SALE (Döviz Satışı — müşteriye USD veriyoruz):
        • USD kasasından -qty ÇIKIŞ (currency_amount=qty, amount=total_amount_eur)
        • TRY kasasına +total_amount_eur GİRİŞ

    Args:
        process_no: str — işlem numarası
        product: Products instance (is_currency=True olan döviz ürünü)
        qty: Decimal — döviz miktarı (ör. 50 USD)
        unit_price: Decimal — kur (ör. 46.29 TL/USD)
        total_amount_eur: Decimal — TL toplam (qty × unit_price)
        operation_type: 'SALE' veya 'PURCHASE'
        user: User instance
        needs_approval: bool — is_approved=False olacak mı

    Returns:
        (paid_total_tl, fx_account, try_account)
    """
    store = getattr(user, 'store', None)
    if not store:
        raise ValidationError("Mağaza bilgisi bulunamadı.")

    # Faz 13: Döviz kodu tespiti SSOT üzerinden. Tüm desteklenen para birimleri
    # (USD/EUR/GBP/CAD/QAR/SAR/CHF/AUD) services.CURRENCY_FROM_PRODUCT_NAME'den
    # gelir. Eskiden burada hardcoded ['USD','EUR','GBP','CAD','QAR'] döngüsü
    # vardı; SARTRY/CHFTRY/AUDTRY için "Döviz kodu tespit edilemedi" hatası
    # buradan fırlardı. Artık tek doğrulama: get_currency_code_from_product.
    fx_currency = get_currency_code_from_product(product)
    if not fx_currency:
        raise ValidationError(
            f"Döviz kodu tespit edilemedi: '{product.name}'. "
            "Ürün adı USDTRY, EURTRY, SARTRY, CHFTRY vb. formatında olmalı."
        )

    from apps.banking.models import BankAccount

    # ─── Döviz kasası (FX): önce kullanıcı seçimi, sonra 0-1-N fallback ───
    fx_account = None
    if fx_bank_account_id:
        fx_account = BankAccount.objects.filter(
            id=fx_bank_account_id, store=store,
            account_type='CASH', is_active=True, is_deleted=False,
        ).first()
    if not fx_account:
        fx_account, fx_count = _resolve_or_create_cash_account(store, fx_currency)
        if not fx_account:
            raise ValidationError(
                f"{fx_currency} para biriminde birden fazla nakit kasa bulundu ({fx_count} adet). "
                "Lütfen kasa seçimi yapınız."
            )

    # ─── TRY kasası: önce kullanıcı seçimi, sonra 0-1-N fallback ───
    try_account = None
    if try_bank_account_id:
        try_account = BankAccount.objects.filter(
            id=try_bank_account_id, store=store,
            account_type='CASH', currency='TRY',
            is_active=True, is_deleted=False,
        ).first()
    if not try_account:
        try_account, try_count = _resolve_or_create_cash_account(store, 'TRY')
        if not try_account:
            raise ValidationError(
                f"TRY para biriminde birden fazla nakit kasa bulundu ({try_count} adet). "
                "Lütfen kasa seçimi yapınız."
            )

    _is_approved = not needs_approval
    exchange_rate = unit_price  # 1 birim döviz = X TL

    # Faz 13: Payment.reference her FX kaydında [KOD] etiketi taşır.
    # Bu, _get_fx_breakdown'ın doğru kırılım yapması için ZORUNLU SSOT alanıdır.
    # Eskiden reference boş kalıyordu ve sistem _guess_fx_code_from_rate fallback'ine
    # düşüp gerçek kuru aralığa göre yorumluyordu (GBP→EUR cross-wiring kök nedeni).
    fx_reference = f'[{fx_currency}] Döviz {operation_type} (Hızlı)'

    if operation_type == 'PURCHASE':
        # Döviz ALIŞI: Müşteriden döviz alıyoruz
        # 1) Döviz kasasına GİRİŞ (+qty döviz)
        Payment.objects.create(
            process_no=process_no,
            payment_type='CASH',
            amount=total_amount_eur,
            currency_amount=qty,
            exchange_rate=exchange_rate,
            is_output=False,  # GİRİŞ
            bank_account=fx_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
            reference=fx_reference,
        )
        # 2) TRY kasasından ÇIKIŞ (-TL tutar)
        Payment.objects.create(
            process_no=process_no,
            payment_type='CASH',
            amount=total_amount_eur,
            is_output=True,  # ÇIKIŞ
            bank_account=try_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
        )
    else:
        # Döviz SATIŞI: Müşteriye döviz veriyoruz
        # ─── YOL 3 (Acil Guard): Bakiye yeterliliğini Payment yazmadan ÖNCE kontrol et ───
        # transaction.atomic bloğu içinde olduğumuzdan select_for_update kilidi etkindir.
        # Yetersizse InsufficientFXBalanceError fırlar — view katmanı yakalar (HTTP 400).
        FXBalanceGuard.assert_sufficient(
            fx_bank_account=fx_account,
            currency_code=fx_currency,
            requested_amount=qty,
            use_lock=True,
        )

        # 1) Döviz kasasından ÇIKIŞ (-qty döviz)
        Payment.objects.create(
            process_no=process_no,
            payment_type='CASH',
            amount=total_amount_eur,
            currency_amount=qty,
            exchange_rate=exchange_rate,
            is_output=True,  # ÇIKIŞ
            bank_account=fx_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
            reference=fx_reference,
        )
        # 2) TRY kasasına GİRİŞ (+TL tutar)
        Payment.objects.create(
            process_no=process_no,
            payment_type='CASH',
            amount=total_amount_eur,
            is_output=False,  # GİRİŞ
            bank_account=try_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
        )

    return total_amount_eur, fx_account, try_account


def _process_barter_currency_entry(process_no, product, qty, unit_price, total_amount_eur, user, needs_approval):
    """
    FAZ 19: TAKAS — Tek Yönlü Döviz Kasa Girişi.

    Müşteri döviz + TL vererek fiziksel ürün (altın vb.) aldığında,
    döviz kasasına TEK YÖNLÜ GİRİŞ yapılır.
    TRY kasasından ÇIKIŞ yapılmaz (çünkü karşılığında TL değil, altın verildi).

    Kullanım:
        Sepet: 100 EURTRY (Alış) + 1 Yeni Çeyrek (Satış)
        → EUR kasasına +100 EUR GİRİŞ (tek yönlü)
        → TRY kasasından ÇIKIŞ YOK (altın verildi, TL değil)
        → Fark (7633 TL) normal ödeme akışıyla tahsil edilir

    Args:
        process_no: str
        product: Products instance (is_currency=True olan döviz ürünü)
        qty: Decimal — döviz miktarı (ör. 100 EUR)
        unit_price: Decimal — kur (ör. 51.13 TL/EUR)
        total_amount_eur: Decimal — TL karşılığı (qty × unit_price)
        user: User instance
        needs_approval: bool

    Returns:
        (fx_account, currency_amount) veya (None, 0) başarısızlıkta
    """
    store = getattr(user, 'store', None)
    if not store:
        log.warning("FAZ19: Mağaza bilgisi bulunamadı — barter currency entry atlanıyor.")
        return None, Decimal('0')

    # Faz 13: SSOT üzerinden döviz tespiti (USD/EUR/GBP/CAD/QAR/SAR/CHF/AUD).
    fx_currency = get_currency_code_from_product(product)
    if not fx_currency:
        log.warning("FAZ19: Döviz kodu tespit edilemedi: '%s'", product.name)
        return None, Decimal('0')

    # Döviz kasası (0-1-N kuralı)
    fx_account, fx_count = _resolve_or_create_cash_account(store, fx_currency)
    if not fx_account:
        log.warning(
            "FAZ19: %s kasası bulunamadı/çoklu (%d adet). Takas döviz girişi atlanıyor.",
            fx_currency, fx_count,
        )
        return None, Decimal('0')

    _is_approved = not needs_approval
    exchange_rate = unit_price

    # TEK YÖNLÜ: Döviz kasasına GİRİŞ (TRY karşılığı ÇIKIŞ YOK)
    # Faz 13: reference [KOD] etiketi zorunlu — _get_fx_breakdown SSOT'u için.
    Payment.objects.create(
        process_no=process_no,
        payment_type='CASH',
        amount=total_amount_eur,
        currency_amount=qty,
        exchange_rate=exchange_rate,
        is_output=False,  # GİRİŞ
        bank_account=fx_account,
        reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
        is_approved=_is_approved,
        reference=f'[{fx_currency}] Takas — Döviz Girişi',
    )

    log.info(
        "FAZ19 BARTER_FX_ENTRY: process_no=%s currency=%s qty=%s rate=%s tl=%s",
        process_no, fx_currency, qty, exchange_rate, total_amount_eur,
    )

    return fx_account, qty


# --- İŞ MANTIĞI FONKSİYONLARI ---

# ============================================================================
# ONARIM FAZI 2: Fast View MASAK TL Normalizasyonu
# ============================================================================
def _fast_normalize_total_to_tl(product, total_amount):
    """
    Tek urunlu fast islemde urun yabanci fiat cinsinden fiyatlandirildiginda
    total_amount'i TL'ye normalize eder. Kur bulunamazsa total_amount'u
    oldugu gibi doner (fallback - sistemi bozmaz).
    """
    try:
        price_cur = (getattr(product, 'price_currency', '') or '').upper()
        # Faz 13: TRY hariç desteklenen tüm döviz kodları SSOT'tan okunur.
        if price_cur and price_cur != 'TRY' and price_cur in SUPPORTED_FX_CURRENCIES:
            rate = _get_exchange_rate_for_currency(price_cur)
            if rate and rate > 0:
                return total_amount * Decimal(str(rate))
    except Exception as exc:
        log.warning(
            f"_fast_normalize_total_to_tl basarisiz ({exc}). "
            f"total_amount oldugu gibi doneriliyor."
        )
    return total_amount


def _check_legal_limits(config, total_amount, payment_type, is_pos_flow, pos_mode, is_manual, manual_cash, customer, product=None):
    """
    2026 Yasal Limit Kontrolleri (Nakit, Fatura, MASAK)

    ONARIM FAZI 2: 'product' parametresi opsiyoneldir. Verilirse ve urun
    yabanci para birimli ise total_amount TL'ye cevrilerek MASAK kontrolu
    en yuksek deger uzerinden calisir (fail-safe).
    """
    CASH_LIMIT = Decimal('30000.00')
    INVOICE_LIMIT = Decimal('36000.00')
    MASAK_LIMIT = Decimal('185000.00')

    # FAIL-SAFE: Urun yabanci para birimli ise TL'ye normalize et
    if product is not None:
        try:
            tl_amount = _fast_normalize_total_to_tl(product, total_amount)
            if tl_amount > total_amount:
                log.info(
                    f"Fast MASAK TL normalizasyonu: orijinal={total_amount}, "
                    f"TL={tl_amount}. Yuksek deger kullanildi."
                )
                total_amount = tl_amount
        except Exception as exc:
            log.warning(f"Fast TL normalizasyon hatasi: {exc}")

    enforce_cash = config.enforce_cash_limit if config else True
    enforce_invoice = config.enforce_invoice_customer if config else True
    enforce_masak = config.enforce_masak_identity if config else True
    enforce_customer_always = getattr(config, 'enforce_customer_always', False) if config else False

    # 0. Tutar Bağımsız Müşteri Zorunluluğu (Mağaza Ayarı)
    if enforce_customer_always and not customer:
        return 'Mağaza ayarlarında her işlem için müşteri seçimi zorunlu kılınmıştır. Lütfen müşteri seçiniz.'

    # 1. Nakit Ödeme Limiti
    if enforce_cash and total_amount > CASH_LIMIT:
        if payment_type == 'CASH':
            return '30.000 TL üzeri tutarlar yasa gereği Nakit tahsil edilemez. Lütfen Kart veya Havale kullanınız.'
        if is_pos_flow and pos_mode == 'CASH':
            return '30.000 TL üzeri tutarlar POS üzerinden Nakit olarak işlenemez.'
        if is_manual and manual_cash > 0:
            return 'İşlem toplamı 30.000 TL\'yi geçtiği için yasa gereği hiç nakit tahsilat yapılamaz.'

    # 2. Fatura Limiti
    if enforce_invoice and total_amount >= INVOICE_LIMIT:
        if not customer:
            return f'{INVOICE_LIMIT} TL ve üzeri işlemlerde fatura kesileceği için müşteri seçimi zorunludur.'

    # 3. MASAK Limiti
    if enforce_masak and total_amount >= MASAK_LIMIT:
        missing = []
        if not (getattr(customer, 'identification_number', '') or '').strip(): missing.append('TCKN')
        if not getattr(customer, 'identification_front_image', None): missing.append('Kimlik Ön Yüz Fotoğrafı')
        if not getattr(customer, 'identification_back_image', None): missing.append('Kimlik Arka Yüz Fotoğrafı')
        if missing:
            return '185.000 TL ve üzeri işlemlerde MASAK gereği müşteri kimlik bilgileri zorunludur. Eksikler: ' + ', '.join(
                missing)

    return None


def _handle_pavo_transaction(raw_pavo, total_amount, pos_reference):
    """
    Pavo POS yanıtını işler ve doğrular.
    """
    if not raw_pavo:
        raise ValidationError('POS ödemesi yanıtı alınamadı.')

    try:
        pavo_obj = json.loads(raw_pavo)
    except:
        raise ValidationError('POS yanıtı JSON formatında değil.')

    if not isinstance(pavo_obj, dict):
        raise ValidationError('POS yanıtı geçersiz.')

    if pavo_obj.get('HasError') is True:
        raise ValidationError(pavo_obj.get('Message') or 'POS ödemesi başarısız.')

    pavo_data = _pavo_extract_data(pavo_obj)
    pavo_status_id = _pavo_extract_status_id(pavo_data)

    # FAZ 30 — StatusId artık settings.PAVO_SUCCESS_STATUS_IDS'e bakıyor.
    # Frontend de aynı set'e bakar; backend/frontend uyumsuzluğu giderildi.
    if not _pavo_is_status_successful(pavo_status_id):
        raise ValidationError('POS ödemesi cihazda tamamlanmadı veya iptal edildi.')

    try:
        pavo_paid_total = _dec(pavo_data.get('TotalPrice') or pavo_data.get('GrossPrice') or total_amount)
    except:
        pavo_paid_total = total_amount

    if pavo_paid_total <= 0:
        raise ValidationError('POS tutarı geçersiz.')

    pavo_sale_number = str(pavo_data.get('SaleNumber') or pavo_data.get('OrderNo') or '').strip()
    pavo_terminal_serial = str(pavo_data.get('TerminalSerialNo') or '').strip()

    pavo_invoice_no = ''
    fin_docs = pavo_data.get('FinancialDocuments') or []
    if isinstance(fin_docs, list) and fin_docs:
        try:
            pavo_invoice_no = str(fin_docs[0].get('InvoiceNo') or '').strip()
        except:
            pass

    if not pos_reference:
        pos_reference = pavo_sale_number

    pavo_inquiry_data = _pavo_pick_inquiry_fields(pavo_obj, pavo_data)

    return pavo_data, pavo_paid_total, pavo_inquiry_data, pavo_invoice_no, pavo_sale_number, pavo_terminal_serial, pos_reference


def _calculate_and_save_profit(process, operation_type, purchase_amount, sale_amount, qty):
    """
    Kar/Zarar Hesaplama ve maliyet mühürleme.
    - Eğer arayüzden maliyet 0 gelirse, veritabanından Has/TL maliyetini otomatik bulur.
    """
    if operation_type == 'SALE':
        try:
            p_cost = _dec(purchase_amount)
            p_sale = _dec(sale_amount)

            # FAZ 3: Maliyet 0 gelirse StockSnapshot WAC'tan dinamik bul
            if p_cost <= Decimal('0') and process.product:

                # Önce StockSnapshot'tan WAC değerlerini al
                _snap = StockSnapshot.objects.filter(
                    product=process.product,
                    store=process.store
                ).first()

                urun_has_maliyet = _snap.weighted_avg_cost_hs if _snap else Decimal('0')
                urun_tl_maliyet = _snap.weighted_avg_cost_eur if _snap else Decimal('0')

                # Fallback: Snapshot yoksa ürün kartından oku
                if not _snap or (urun_has_maliyet <= Decimal('0') and urun_tl_maliyet <= Decimal('0')):
                    urun_has_maliyet = getattr(process.product, 'buy_price_hs', 0)
                    urun_tl_maliyet = getattr(process.product, 'buy_price_eur', 0)

                # 1. Has Maliyeti varsa, işlem anındaki kur ile TL'ye çevir
                if urun_has_maliyet and _dec(urun_has_maliyet) > Decimal('0'):
                    kur = _dec(getattr(process, 'hs_rate_buy_eur', 0))
                    if kur <= Decimal('0'):
                        kur = _dec(getattr(process, 'hs_rate_sale_eur', 0))

                    if kur > Decimal('0'):
                        p_cost = _dec(urun_has_maliyet) * kur

                # 2. Direkt TL maliyeti tanımlıysa onu kullan
                elif urun_tl_maliyet and _dec(urun_tl_maliyet) > Decimal('0'):
                    p_cost = _dec(urun_tl_maliyet)

            # Maliyet ve Satış 0'dan büyükse veritabanına kârı yaz
            if p_cost > Decimal('0') and p_sale > Decimal('0'):
                cost_amount_eur = _dec(p_cost * qty, '0.01')
                gross_profit_val = (p_sale - p_cost) * qty

                process.gross_profit = _dec(gross_profit_val, '0.01')

                tax_amount = gross_profit_val * Decimal('0.20')
                process.net_profit = _dec(gross_profit_val - tax_amount, '0.01')
                process.cost_amount_eur = cost_amount_eur

                # cost_amount_hs: TL maliyetini Has'a çevir (hs_rate_buy_eur kullan)
                hs_rate = _dec(
                    getattr(process, 'hs_rate_buy_eur', None) or getattr(process, 'hs_rate_sale_eur', None) or 0)
                if hs_rate > Decimal('0'):
                    process.cost_amount_hs = _dec(cost_amount_eur / hs_rate, '0.001')
                else:
                    process.cost_amount_hs = Decimal('0.000')

                process.save(update_fields=['gross_profit', 'net_profit', 'cost_amount_eur', 'cost_amount_hs'])
        except Exception as e:
            # İsteğe bağlı: Hataları görmek için loglayabilirsiniz
            import logging
            logging.getLogger(__name__).error(f"Kâr hesaplama hatası: {e}")


def _process_payments_and_balances(
        request, process_no, total_amount, operation_type,
        is_manual, is_pos_flow, payment_type, pos_mode,
        customer, hs_rate_sale_eur, hs_rate_buy_eur, user
):
    """
    Ödeme ve Bakiye (Has Altın) İşlemleri.

    Faz 2 Güncellemesi:
        - Kredi kartı ve havale ödemelerinde bank_account_id doğrulanır
        - Payment kaydına bank_account FK ve reconciliation_status yazılır
        - Nakit ödemelerde reconciliation_status = NOT_REQUIRED kalır
        - Banka hesabı ID'leri request.POST'tan okunur:
            bank_account_card     → Kredi kartı POS hesabı
            bank_account_transfer → Havale/EFT banka hesabı
    """
    cash_amt = Decimal('0')
    card_amt = Decimal('0')
    transfer_amt = Decimal('0')
    paid_total = Decimal('0')
    is_output = (operation_type == 'PURCHASE')
    effective_payment_type = payment_type
    store = getattr(user, 'store', None)

    # Banka hesabı ID'lerini request'ten oku
    ba_cash_id = (request.POST.get('bank_account_cash') or '').strip() or None
    ba_card_id = (request.POST.get('bank_account_card') or '').strip() or None
    ba_transfer_id = (request.POST.get('bank_account_transfer') or '').strip() or None

    # Faz 4: Komisyon verilerini oku
    _installment_count = int(request.POST.get('installment_count') or 1)
    _commission_rate = request.POST.get('commission_rate', '').strip()
    _commission_amount = request.POST.get('commission_amount', '').strip()
    _net_amount = request.POST.get('net_amount', '').strip()
    _maturity_date_str = request.POST.get('maturity_date', '').strip()

    # 1. Ödeme Tutarları
    if is_manual:
        cash_amt = _dec(request.POST.get('manual_cash', '0'))
        card_amt = _dec(request.POST.get('manual_card', '0'))
        transfer_amt = _dec(request.POST.get('manual_transfer', '0'))
        paid_total = cash_amt + card_amt + transfer_amt
        effective_payment_type = 'MANUAL'

        # FAZ 16: Frontend → Backend toplam tutarsızlık loglama
        # Frontend getFastContext().gross düzeltildiği için bu fark artık
        # oluşmamalı. Eğer hâlâ fark varsa potansiyel frontend manipülasyonu
        # veya race condition tespit etmek için logla.
        _manual_diff = abs(paid_total - total_amount)
        if _manual_diff > Decimal('0.50'):
            log.warning(
                "FAZ16 TUTAR_FARKI: process_no=%s total_amount=%s paid_total=%s "
                "fark=%s (cash=%s card=%s transfer=%s)",
                process_no, total_amount, paid_total, _manual_diff,
                cash_amt, card_amt, transfer_amt,
            )
    elif is_pos_flow:
        if pos_mode == 'CASH':
            effective_payment_type = 'CASH'
            cash_amt = total_amount
        elif pos_mode == 'TRANSFER':
            effective_payment_type = 'TRANSFER'
            transfer_amt = total_amount
        else:
            effective_payment_type = 'CREDIT_CARD'
            card_amt = total_amount
        paid_total = total_amount
    else:
        if payment_type == 'CASH':
            cash_amt = total_amount
        elif payment_type == 'TRANSFER':
            transfer_amt = total_amount
        elif payment_type == 'CREDIT_CARD':
            card_amt = total_amount
        paid_total = total_amount

    # FAZ 18: Onaylı Kasa durumunu kontrol et
    _needs_approval = _get_approval_status(user)
    _is_approved = not _needs_approval

    # 2. Banka Hesabı Doğrulama (Faz 2 + Faz 5: CASH dahil)
    #    FAZ 18: Nakit için Akıllı Kasa Yönlendirmesi (0-1-N kuralı)
    ba_cash = None
    ba_card = None
    ba_transfer = None

    if cash_amt > 0 and store:
        if ba_cash_id:
            # Kullanıcı seçim yaptı → validator ile doğrula
            ba_cash = PaymentBankAccountValidator.validate(
                payment_type='CASH',
                bank_account_id=ba_cash_id,
                store=store,
            )
        else:
            # FAZ 18: Akıllı yönlendirme — otomatik kasa bul/oluştur
            _auto_cash, _cash_count = _resolve_or_create_cash_account(store, 'TRY')
            if _auto_cash:
                ba_cash = _auto_cash
            elif _cash_count >= 2:
                raise ValidationError(
                    "Birden fazla TRY nakit kasası bulundu. Lütfen kasa seçimi yapınız."
                )

    if card_amt > 0 and store:
        ba_card = PaymentBankAccountValidator.validate(
            payment_type='CREDIT_CARD',
            bank_account_id=ba_card_id,
            store=store,
        )

    if transfer_amt > 0 and store:
        ba_transfer = PaymentBankAccountValidator.validate(
            payment_type='TRANSFER',
            bank_account_id=ba_transfer_id,
            store=store,
        )

    # 3. Payment Kayıtları (Faz 2: bank_account + reconciliation_status eklendi)
    #    FAZ 17: Her ödeme kaydına döviz kuru bilgisi eklenir.
    #    FAZ 18: is_approved onay desteği eklendi.
    if cash_amt > 0:
        _cash_fx = _build_currency_extra(ba_cash, cash_amt)
        Payment.objects.create(
            process_no=process_no,
            payment_type='CASH',
            amount=cash_amt,
            is_output=is_output,
            bank_account=ba_cash,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
            **_cash_fx,
        )
    if card_amt > 0:
        # FAZ 15: Komisyon hesaplaması artık BACKEND'de yapılır.
        _cc_extra = {}
        if _installment_count > 1:
            _cc_extra['installment'] = _installment_count

        # Backend komisyon hesaplaması (güvenilir)
        if ba_card:
            try:
                _comm_svc = POSCommissionService()
                _comm_result = _comm_svc.calculate(
                    bank_account=ba_card,
                    amount=card_amt,
                    installment_count=_installment_count,
                    card_type='GENERIC',
                )
                _cc_extra['commission_rate_applied'] = _comm_result['rate']
                _cc_extra['commission_amount'] = _comm_result['commission']
                _cc_extra['net_amount'] = _comm_result['net_amount']
                _cc_extra['maturity_date'] = _comm_result['maturity_date']
            except Exception:
                _cc_extra['commission_rate_applied'] = Decimal('0')
                _cc_extra['commission_amount'] = Decimal('0')
                _cc_extra['net_amount'] = card_amt
                _cc_extra['maturity_date'] = None

        # FAZ 17: Döviz kuru bilgisini ekle
        _card_fx = _build_currency_extra(ba_card, card_amt)
        _cc_extra.update(_card_fx)

        Payment.objects.create(
            process_no=process_no,
            payment_type='CREDIT_CARD',
            amount=card_amt,
            is_output=is_output,
            bank_account=ba_card,
            reconciliation_status=Payment.ReconciliationStatus.PENDING,
            is_approved=_is_approved,
            **_cc_extra,
        )
    if transfer_amt > 0:
        _transfer_fx = _build_currency_extra(ba_transfer, transfer_amt)
        Payment.objects.create(
            process_no=process_no,
            payment_type='TRANSFER',
            amount=transfer_amt,
            is_output=is_output,
            bank_account=ba_transfer,
            reconciliation_status=Payment.ReconciliationStatus.PENDING,
            is_approved=_is_approved,
            **_transfer_fx,
        )

    # 4. Kasa Hareketi (Sadece Nakit)
    # FAZ 16: Para birimi ürünlerine (is_currency=True) stok hareketi oluşturulmaz.
    # TRY nakit giriş/çıkışları yalnızca Payment tablosu üzerinden takip edilir.
    # Bu sayede Stok Yönetimi'ndeki "295.837 adet TRY" gibi çifte sayım ortadan kalkar.
    if cash_amt > 0:
        try_prod = Products.objects.filter(name__icontains="TRY - Türk Lirası").first()
        if try_prod and not try_prod.is_currency:
            real_cash_mv = 'EXIT' if is_output else 'ENTRY'
            update_product_stock(try_prod, real_cash_mv, cash_amt, 0, 0, user, "Nakit (Hızlı)", process_no)

    # 5. Bakiye Hesaplama
    # FAZ 18.6: POS komisyonu müşterinin cari bakiyesini ETKİLEMEMELİDİR.
    # Müşteri komisyon bedelini ödedi diye hesabına "Alacaklı" olarak geçmemeli.
    # paid_total komisyon dahil tutardır (ör. 10.500 TL).
    # Komisyon tutarını çıkararak "net ödenen" ile karşılaştırıyoruz.
    # Böylece: balance_diff = total_amount - (paid_total - commission) = 10.000 - (10.500 - 500) = 0
    if customer:
        _total_commission = Decimal('0')
        _comm_val = request.POST.get('commission_amount', '').strip()
        if _comm_val:
            try:
                _total_commission = Decimal(str(_comm_val).replace(',', '.'))
            except Exception:
                _total_commission = Decimal('0')

        # Net ödenen = ödenen toplam - POS komisyonu (komisyon müşterinin maliyeti değil)
        _paid_net_of_commission = paid_total - _total_commission
        balance_diff = total_amount - _paid_net_of_commission

        if abs(balance_diff) > Decimal('0.01'):
            sale_rate = hs_rate_sale_eur
            buy_rate = hs_rate_buy_eur if hs_rate_buy_eur > 0 else sale_rate

            if not is_output:  # SATIŞ
                if balance_diff > 0:  # Eksik Ödeme -> Borç
                    hs_debt_gram = balance_diff / sale_rate
                    customer.payable_hs = _dec(customer.payable_hs) + _dec(hs_debt_gram, '0.001')
                elif balance_diff < 0:  # Fazla Ödeme -> Alacak
                    hs_credit_gram = abs(balance_diff) / buy_rate
                    customer.receivable_hs = _dec(customer.receivable_hs) + _dec(hs_credit_gram, '0.001')
            else:  # ALIŞ
                if balance_diff > 0:  # Biz Eksik Ödedik -> Müşteri Alacak
                    hs_credit_gram = balance_diff / buy_rate
                    customer.receivable_hs = _dec(customer.receivable_hs) + _dec(hs_credit_gram, '0.001')
                elif balance_diff < 0:  # Biz Fazla Ödedik -> Müşteri Borç
                    hs_debt_gram = abs(balance_diff) / sale_rate
                    customer.payable_hs = _dec(customer.payable_hs) + _dec(hs_debt_gram, '0.001')

            customer.save(update_fields=['payable_hs', 'receivable_hs'])

    return paid_total, effective_payment_type, cash_amt, card_amt, transfer_amt


# --- VIEW FONKSİYONLARI ---

@login_required(login_url='login')
def check_fast_stock(request):
    # Bu fonksiyon mantığı aynı kalmıştır, sadece importlar düzenlendi.
    if request.method != "POST":
        return JsonResponse({'ok': False, 'error': True, 'error_msg': 'Geçersiz istek.'}, status=405)

    payload = {}
    try:
        if (request.content_type or '').lower().startswith('application/json'):
            payload = json.loads((request.body or b'{}').decode('utf-8') or '{}') or {}
    except Exception:
        payload = {}

    fast_product_id = (
            (payload.get('fast_product_id') or payload.get('product_id')
             or request.POST.get('fast_product_id') or request.POST.get('product_id')) or ''
    ).strip()
    if not fast_product_id:
        return JsonResponse({'ok': False, 'error': True, 'error_msg': 'Ürün seçilmelidir.'}, status=400)

    try:
        product = Products.objects.select_related('category').get(pk=fast_product_id)
    except Products.DoesNotExist:
        return JsonResponse({'ok': False, 'error': True, 'error_msg': 'Ürün bulunamadı!'}, status=404)

    try:
        piece_raw = payload.get('piece') if payload else (request.POST.get('piece') or 0)
        piece = int(Decimal(str(piece_raw).replace(',', '.')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except Exception:
        piece = 0

    try:
        gram_raw = payload.get('gram') if payload else (request.POST.get('gram') or '0')
        gram = Decimal(str(gram_raw).replace(',', '.'))
    except Exception:
        gram = Decimal('0')

    store = getattr(request.user, 'store', None)
    if not store:
        return JsonResponse({'ok': False, 'error': True, 'error_msg': 'Mağaza bilgisi bulunamadı.'}, status=400)

    # Config
    config = _get_store_config(request.user)
    enforce_masak = config.enforce_masak_identity if config else True
    enforce_invoice = config.enforce_invoice_customer if config else True
    MASAK_LIMIT = Decimal('185000.00')
    INVOICE_LIMIT = Decimal('36000.00')

    try:
        gross_raw = payload.get('gross') if payload else (request.POST.get('gross') or '0')
        gross = Decimal(str(gross_raw).replace(',', '.'))
    except Exception:
        gross = Decimal('0')

    customer_id = ''
    try:
        customer_id = ((payload.get('customer_id') if payload else '') or request.POST.get('customer_id') or '').strip()
    except Exception:
        customer_id = ''

    if enforce_masak and gross >= MASAK_LIMIT:
        if not customer_id:
            return JsonResponse({'ok': False, 'error': True,
                                 'error_msg': '185.000 TL ve üzeri işlemlerde (MASAK) müşteri seçimi zorunludur.'},
                                status=400)
        # Diğer kontroller... (customer validation)

    elif enforce_invoice and gross >= INVOICE_LIMIT:
        if not customer_id:
            return JsonResponse({'ok': False, 'error': True,
                                 'error_msg': '36.000 TL ve üzeri işlemlerde fatura zorunluluğu nedeniyle müşteri seçimi zorunludur.'},
                                status=400)

    # FAZ 20.3: İşlem yönünü (SALE/PURCHASE) oku
    operation_type = ''
    try:
        operation_type = (
            (payload.get('operationType') if payload else '') or
            request.POST.get('operationType') or ''
        ).strip().upper()
    except Exception:
        operation_type = ''

    # ─── YOL 2 (SSOT Refactor): is_currency=True ürünler için Payment SSOT ───
    # Döviz ürün stoğunu StockSnapshot'tan değil, FX kasaların Payment bakiyesinden oku.
    # Bakiye Düzelt/Açılış işlemiyle %100 senkron kalır.
    if getattr(product, 'is_currency', False):
        currency_code = get_currency_code_from_product(product)
        if not currency_code:
            return JsonResponse({
                'ok': False, 'error': True,
                'error_msg': f"Geçersiz döviz ürünü: {product.name}"
            }, status=400)

        fx_balance = FXBalanceReader.get_balance(store, currency_code)

        # PURCHASE/BUY: Bakiye kontrolü yapılmaz (kasaya döviz giriyor)
        if operation_type == 'PURCHASE' or operation_type == 'BUY':
            return JsonResponse({
                'ok': True, 'in_stock': True, 'available': True,
                'mode': 'FX_BYPASS',
                'fx_balance': str(fx_balance),
                'fx_currency': currency_code,
                'message': ''
            })

        # SALE: Gerçek FX bakiye kontrolü
        try:
            requested = Decimal(str(piece))
        except Exception:
            requested = Decimal('0')

        sufficient = (requested > 0) and (fx_balance >= requested)
        return JsonResponse({
            'ok': True,
            'in_stock': sufficient,
            'available': sufficient,
            'mode': 'FX_BALANCE',
            'fx_balance': str(fx_balance),
            'fx_currency': currency_code,
            'requested': str(requested),
            'message': '' if sufficient else (
                f'Yetersiz {currency_code} bakiyesi: '
                f'Mevcut {fx_balance} {currency_code}, İstenen {requested} {currency_code}.'
            )
        })

    # FAZ 3: Inventories yerine StockSnapshot'tan oku
    snap = StockSnapshot.objects.filter(product=product, store=store).first()
    tot_pcs = snap.stock_pieces if snap else 0
    tot_wgt = snap.stock_gram if snap else Decimal('0')

    is_gram_bullion = bool(getattr(product, 'is_gram_bullion', False))

    # FAZ 20.3: ALIŞ (PURCHASE) işleminde stok kontrolü YAPILMAZ.
    # Müşteriden mal/döviz alırken kasada 0 stok olabilir.
    # Stok kontrolü SADECE SATIŞ (SALE) işleminde yapılır.
    if operation_type == 'PURCHASE' or operation_type == 'BUY':
        return JsonResponse({
            'ok': True, 'in_stock': True, 'available': True, 'mode': 'BYPASS',
            'stock_piece': int(tot_pcs), 'stock_gram': str(tot_wgt),
            'message': ''  # Alış işlemi — stok kontrolü bypass
        })

    # SATIŞ (SALE) işlemi — stok kontrolü yapılır
    # ÖNEMLİ: Hurda ürünler (is_scrap=True) UNIQUE moduna alınmaz.
    # Hurdalar gram bazlı ve havuzlanmış (milyem bazında tek kayıt) stok
    # modeliyle çalışır; bu nedenle aşağıdaki GRAM bloğuna düşmelidir.
    # Sadece BARKODLU ürünler UNIQUE modunda is_completed bayrağı ile
    # tezgah durumu kontrol edilir.
    if bool(getattr(product, 'barcode', None)) and not bool(getattr(product, 'is_scrap', False)):
        ok_unique = (getattr(product, 'is_completed', False) is False)
        return JsonResponse({
            'ok': True, 'in_stock': ok_unique, 'available': ok_unique, 'mode': 'UNIQUE',
            'stock_piece': 1 if ok_unique else 0, 'need_piece': 1, 'stock_gram': '0.000', 'need_gram': '0.000',
            'message': '' if ok_unique else 'Bu ürün satılmış görünüyor. Stok yetersiz.'
        })

    if is_gram_bullion:
        try:
            gram_q = gram.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
        except:
            gram_q = Decimal('0.000')
        try:
            stock_q = Decimal(str(tot_wgt).replace(',', '.')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
        except:
            stock_q = Decimal('0.000')
        ok = (gram_q > 0 and stock_q >= gram_q)
        return JsonResponse({
            'ok': True, 'in_stock': ok, 'available': ok, 'mode': 'GRAM',
            'stock_gram': str(stock_q), 'need_gram': str(gram_q),
            'message': '' if ok else 'Yetersiz Stok: Sistemdeki stok miktarından fazlasını satamazsınız.'
        })

    ok = (piece > 0 and int(tot_pcs) >= int(piece))
    return JsonResponse({
        'ok': True, 'in_stock': ok, 'available': ok, 'mode': 'PIECE',
        'stock_piece': int(tot_pcs), 'need_piece': int(piece),
        'message': '' if ok else 'Yetersiz Stok: Sistemdeki stok miktarından fazlasını satamazsınız.'
    })


@login_required(login_url='login')
def add_fast_process(request):
    """
    Hızlı İşlem (Fast Process) Ekleme View'ı.
    YENİ: create_invoice_from_process kullanımı eklendi.
    """
    if request.method != "POST":
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=405)

    try:
        with transaction.atomic():
            user = request.user
            store = user.store
            process_no = request.POST.get('process_no') or generate_process_no()

            fast_product_id = request.POST.get('fast_product_id')
            if not fast_product_id:
                return JsonResponse({'error': True, 'error_msg': 'Ürün seçilmelidir.'}, status=400)

            product = Products.objects.get(pk=fast_product_id)

            # Temel Parametreler
            operation_type = (request.POST.get('operationType') or 'SALE').upper()
            payment_type = (request.POST.get('paymentType') or 'CASH').upper()
            is_pos_flow = (payment_type == 'POS')
            is_manual = request.POST.get('is_manual_payment') == 'true'
            pos_mode = (request.POST.get('pos_mode') or '').upper().strip()
            installment = int(request.POST.get('installment') or 1)
            pos_reference = request.POST.get('pos_reference') or ''
            raw_pavo = request.POST.get('pavo_result_json') or ''
            sale_hs = request.POST.get("sale_hs") or ''
            buy_hs = request.POST.get("buy_hs") or ''
            buy_amount = request.POST.get("buy_amount") or ''

            customer_id = (request.POST.get('customer_id') or '').strip()
            customer = None
            if customer_id:
                try:
                    customer = Customers.objects.select_for_update().get(pk=customer_id)
                except:
                    customer = None

            # Miktarlar
            try:
                piece = int(request.POST.get('piece') or 0)
                gram = _dec(request.POST.get('gram'))
                purchase_amount = _dec(request.POST.get('buy_amount'))
                sale_amount = _dec(request.POST.get('sale_amount'))
                labor_net = _dec(request.POST.get('labor_amount'))
                manual_cash = _dec(request.POST.get('manual_cash', '0'))
            except:
                return JsonResponse({'error': True, 'error_msg': 'Sayısal alanlarda hata.'}, status=400)

            unit_price = purchase_amount if operation_type == 'PURCHASE' else sale_amount
            if unit_price <= 0:
                return JsonResponse({'error': True, 'error_msg': 'Birim fiyat geçersiz.'}, status=400)

            # ------------------------------------------------------------------
            # ONARIM FAZI 3 / ADIM 1 — WATCH/DIAMOND PIECE FALLBACK
            # ------------------------------------------------------------------
            # Barkod okuma ile Fast ekranina dusen Saat/Pirlanta urunlerinde
            # UI adet input'unu otomatik 1'e zorla, gram=0 yap. Frontend'de
            # adet alani yoksa backend bu fallback'i uygular.
            # is_gram_bullion=False zaten WATCH/DIAMOND'in yonunu adetli yapar.
            # Bu ek katman defansif koruma olarak durur.
            # ------------------------------------------------------------------
            try:
                _mat_type = getattr(product, 'material_type', 'GOLD') or 'GOLD'
                if _mat_type in ('WATCH', 'DIAMOND'):
                    if piece <= 0:
                        piece = 1
                        log.info(
                            f"fast add_process: WATCH/DIAMOND piece fallback "
                            f"(product_id={getattr(product, 'id', None)}, "
                            f"material_type={_mat_type}, piece=1)"
                        )
                    # gram sifirla: stok servisi WATCH/DIAMOND icin gram=0 bekler
                    gram = Decimal('0')
            except Exception as _mt_exc:
                log.warning(f"fast material_type fallback hatasi: {_mt_exc}")

            is_gram_bullion = bool(getattr(product, 'is_gram_bullion', False))
            if is_gram_bullion:
                try:
                    gram_q = gram.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                except:
                    gram_q = Decimal('0.000')
                piece = 0;
                qty = gram_q;
                metal_total = (gram_q * unit_price)
            else:
                gram_q = Decimal('0.000');
                gram = Decimal('0')
                qty = Decimal(piece);
                metal_total = (qty * unit_price)

            metal_total = _dec(metal_total, '0.01')
            if metal_total <= 0:
                return JsonResponse({'error': True, 'error_msg': 'Toplam tutar 0 olamaz.'}, status=400)

            # FAZ 20.3: Backend son güvenlik katmanı — SATIŞ'ta stok kontrolü
            # (check_fast_stock zaten kontrol ediyor ama manipülasyona karşı)
            _is_currency_prod = getattr(product, 'is_currency', False)
            if operation_type == 'SALE' and not _is_currency_prod:
                _snap = StockSnapshot.objects.filter(product=product, store=user.store).first()
                if _snap:
                    if is_gram_bullion and qty > (_snap.stock_gram or Decimal('0')):
                        return JsonResponse({
                            'error': True,
                            'error_msg': f'Yetersiz Stok: {product.name} için stokta {_snap.stock_gram} gr var, {qty} gr satılamaz.'
                        }, status=400)
                    elif not is_gram_bullion and int(qty) > (_snap.stock_pieces or 0):
                        return JsonResponse({
                            'error': True,
                            'error_msg': f'Yetersiz Stok: {product.name} için stokta {_snap.stock_pieces} adet var, {int(qty)} adet satılamaz.'
                        }, status=400)

            if operation_type != 'SALE': labor_net = Decimal('0.00')
            labor_net = _dec(labor_net, '0.01')
            labor_vat = _dec(labor_net * Decimal('0.20'), '0.01') if (
                    operation_type == 'SALE' and labor_net > 0) else Decimal('0.00')

            # FAZ 18.5: KUYUMCULUK INCLUSIVE FİYATLAMA
            # Satış fiyatı NİHAİ fiyattır — işçilik ve KDV fiyatın İÇİNDEDİR.
            # total_amount = Fiyat × Miktar. labor_net ve labor_vat yalnızca
            # Process kaydına bilgi amaçlı yazılır, toplam tutarı şişirmez.
            # Fiyata eklenecek tek değer POS komisyonudur (backend'de POSCommissionService ile).
            total_amount = metal_total

            if total_amount <= 0:
                return JsonResponse({'error': True, 'error_msg': 'Toplam tutar 0 olamaz.'}, status=400)

            # --- YASAL LİMİT KONTROLLERİ ---
            # ONARIM FAZI 2: product parametresi gecilerek yabanci fiat
            # fiyatlandirilmis urunlerin MASAK'tan kacmasi engellenir.
            config = _get_store_config(user)
            limit_error = _check_legal_limits(
                config, total_amount, payment_type, is_pos_flow, pos_mode, is_manual, manual_cash, customer,
                product=product,
            )
            if limit_error:
                return JsonResponse({'error': True, 'error_msg': limit_error}, status=400)

            # --- FAZ 3: HAS ALTIN FİYATLANDIRMA - PriceService ---
            try:
                hs_data = PriceService.get_price('GOLD_24K')
                hs_rate_sale_eur = _dec(hs_data.get('sell_tl', Decimal('0')), '0.01')
                hs_rate_buy_eur = _dec(hs_data.get('buy_tl', hs_rate_sale_eur), '0.01')
            except Exception:
                hs_rate_sale_eur = Decimal('0')
                hs_rate_buy_eur = Decimal('0')

            # Fallback: PriceService boş dönerse eski Products tablosundan oku
            if hs_rate_sale_eur <= 0:
                hs_prod = Products.objects.filter(name__icontains='Has Altın').only('sale_price_eur', 'buy_price_eur').first()
                if not hs_prod or not getattr(hs_prod, 'sale_price_eur', None):
                    return JsonResponse({'error': True, 'error_msg': 'Sistemde Has Altın ürünü tanımlı değil.'}, status=500)
                hs_rate_sale_eur = _dec(hs_prod.sale_price_eur, '0.01')
                hs_rate_buy_eur = _dec(getattr(hs_prod, 'buy_price_eur', hs_rate_sale_eur), '0.01')

            # Karat
            prod_karat = None
            try:
                if hasattr(product, 'karat') and product.karat:
                    prod_karat = int(product.karat)
                elif hasattr(product, 'milyem') and product.milyem:
                    prod_karat = int(
                        (Decimal(str(product.milyem)) / Decimal('1000') * Decimal('24')).quantize(Decimal('1'),
                                                                                                  rounding=ROUND_HALF_UP))
            except:
                prod_karat = None

            price_hs = Decimal('0.000')

            # ────────────────────────────────────────────────────────────────
            # FAZ S6 (PIVOT 2026-04-23): WATCH/DIAMOND için price_hs=0 garantisi
            # ────────────────────────────────────────────────────────────────
            # Frontend foreign panel sale_hs göndermez (boş veya 0 gelir).
            # Defansif olarak burada da material_type kontrolü ile price_hs=0
            # zorlanır — sale_hs yanlışlıkla doluysa bile sızmaz.
            # GOLD/SILVER mevcut akışı etkilenmez.
            # ────────────────────────────────────────────────────────────────
            if _mat_type in ('WATCH', 'DIAMOND'):
                price_hs = Decimal('0.000')  # Has yok
            elif is_gram_bullion or qty > 0:
                kar = Decimal(str(prod_karat or 24))

                # sale_hs boş gelirse Decimal hatası çıkmasın diye guard
                _sh = (sale_hs or '0').strip() or '0'
                try:
                    price_hs = (qty * Decimal(str(_sh)))
                except Exception:
                    price_hs = Decimal('0')

            price_hs = price_hs.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
            print(price_hs)
            # --- POS / PAVO İŞLEMLERİ ---
            pavo_invoice_no = ''
            pavo_sale_number = ''
            pavo_inquiry_data = {}
            pavo_terminal_serial = ''

            if is_pos_flow and operation_type == 'SALE':
                pavo_data, pavo_paid_total, pavo_inquiry_data, pavo_invoice_no, pavo_sale_number, pavo_terminal_serial, pos_reference = _handle_pavo_transaction(
                    raw_pavo, total_amount, pos_reference
                )
                total_amount = pavo_paid_total
            print(total_amount)
            amount = Decimal('0.000')
            if operation_type != 'SALE':
                print(buy_amount)
                print(buy_hs)
                # FAZ S6 (PIVOT): WATCH/DIAMOND alış/iade için de price_hs=0 garantisi
                if _mat_type in ('WATCH', 'DIAMOND'):
                    price_hs = Decimal('0.000')
                else:
                    _bh = (str(buy_hs) or '0').strip() or '0'
                    try:
                        price_hs = (qty * Decimal(_bh))
                    except Exception:
                        price_hs = Decimal('0')
                _ba = (str(buy_amount) or '0').strip() or '0'
                try:
                    amount = Decimal(_ba)
                except Exception:
                    amount = Decimal('0')

            target_value = amount if operation_type != 'SALE' else total_amount

            # --- PROCESS KAYDI ---
            process = Process.objects.create(
                process_no=process_no,
                product_id=fast_product_id,
                is_status='COMPLETED',
                process_type='FAST_PROCESS',
                transaction_type=operation_type,
                piece=int(qty if not is_gram_bullion else 0),
                gram=_dec(qty if is_gram_bullion else Decimal('0'), '0.001'),
                unit_price=_dec(unit_price, '0.01'),
                amount=_dec(total_amount, '0.01'),
                price_hs=price_hs,
                hs_rate_sale_eur=hs_rate_sale_eur,
                hs_rate_buy_eur=hs_rate_buy_eur,
                karat=(prod_karat or None),
                labor_amount=_dec(labor_net, '0.01'),
                employee=user,
                store=store,
                customer=customer if hasattr(Process, 'customer') else None
            )

            _calculate_and_save_profit(process, operation_type, purchase_amount, sale_amount, qty)

            # --- STOK HAREKETİ ---
            prod_mv = 'ENTRY' if operation_type == 'PURCHASE' else 'EXIT'
            update_product_stock(
                product, prod_mv,
                int(qty if not is_gram_bullion else 0),
                _dec(qty if is_gram_bullion else Decimal('0'), '0.001'),
                0, user, 'Hızlı işlem', process_no
            )

            # --- FAZ 18: DÖVİZ BOZMA (ÇİFT TARAFLI) KONTROLÜ ---
            # Eğer ürün is_currency=True ise (ör. USDTRY), normal ödeme akışı
            # yerine çift taraflı (double-entry) kasa işlemi yapılır.
            _is_currency_product = getattr(product, 'is_currency', False)

            if _is_currency_product:
                # Döviz ürünü: qty=döviz miktarı, unit_price=kur, total_amount=TL karşılığı
                _needs_approval = _get_approval_status(user)
                # FAZ 20.1: Kullanıcının UI'dan seçtiği kasa ID'lerini fonksiyona ilet
                _ui_try_id = (request.POST.get('bank_account_cash') or '').strip() or None
                _ui_fx_id  = (request.POST.get('bank_account_fx') or '').strip() or None
                paid_total_tl, _fx_acct, _try_acct = _process_currency_exchange(
                    process_no=process_no,
                    product=product,
                    qty=qty,
                    unit_price=unit_price,
                    total_amount_eur=total_amount,
                    operation_type=operation_type,
                    user=user,
                    needs_approval=_needs_approval,
                    try_bank_account_id=_ui_try_id,
                    fx_bank_account_id=_ui_fx_id,
                )
                paid_total = paid_total_tl
                effective_payment_type = 'CASH'
                cash_amt = total_amount
                card_amt = Decimal('0')
                transfer_amt = Decimal('0')
            else:
                # --- ÖDEME VE BAKİYE (Helper) — Normal akış ---
                paid_total, effective_payment_type, cash_amt, card_amt, transfer_amt = _process_payments_and_balances(
                    request, process_no, total_amount, operation_type,
                    is_manual, is_pos_flow, payment_type, pos_mode,
                    customer, hs_rate_sale_eur, hs_rate_buy_eur, user
                )

            # --- FATURA OLUŞTURMA (YENİ MODÜL KULLANIMI) ---
            inv = create_invoice_from_process(
                store=store,
                customer=customer,
                process=process,
                product=product,
                operation_type=operation_type,
                is_pos_flow=is_pos_flow,
                pavo_invoice_no=pavo_invoice_no,
                pavo_inquiry_data=pavo_inquiry_data,
                pavo_sale_number=pavo_sale_number,
                paid_total=paid_total,
                pos_reference=pos_reference,
                qty=qty,
                is_gram_bullion=is_gram_bullion,
                unit_price=unit_price,
                labor_net=labor_net
            )

            # Pavo Terminal Log
            try:
                if is_pos_flow:
                    terminal = None
                    if pavo_terminal_serial:
                        terminal = PavoTerminal.objects.filter(store=store, serial_number=pavo_terminal_serial).first()
                    PavoLocalSale.objects.create(
                        terminal=terminal, invoice=inv,
                        request_payload={}, response_payload=pavo_inquiry_data,
                        status='SUCCESS', amount=_dec(total_amount, '0.01'), currency='TRY'
                    )
            except:
                pass

            # Link ve Update
            pavo_link = (pavo_inquiry_data or {}).get('SaleInquieryLink') or ''
            invoice_url = pavo_link if pavo_link else request.build_absolute_uri(
                reverse('invoices:detail', kwargs={'record_id': inv.id}))

            try:
                upd = []
                if hasattr(process, 'invoice_url'): process.invoice_url = invoice_url; upd.append('invoice_url')
                if hasattr(process, 'invoice_no'): process.invoice_no = inv.invoice_no; upd.append('invoice_no')
                if upd: process.save(update_fields=upd)
            except:
                pass

            # --- BİLDİRİM ---
            if customer:
                try:
                    qty_str = f"{gram} gr" if is_gram_bullion else f"{piece} adet"
                    is_output = (operation_type == 'PURCHASE')
                    direction_text = "Ödeme Yapıldı" if is_output else "Ödeme Alındı"
                    if paid_total == 0:
                        direction_text = "Cariye İşlendi"
                    elif paid_total < total_amount:
                        direction_text = "Kısmi Ödeme"

                    item_data = [{"product_name": getattr(product, 'name', 'Ürün'), "qty_str": qty_str,
                                  "amount_eur": fmt_tl(total_amount)}]
                    payments_ctx = {
                        "cash": float(cash_amt), "credit_card": float(card_amt), "transfer": float(transfer_amt),
                        "paid_total_tl": float(paid_total), "has_any": paid_total > 0,
                        "direction_text": direction_text, "installment": installment
                    }
                    totals_ctx = {"net_tl_abs": fmt_tl(total_amount), "total_sales_eur": fmt_tl(total_amount)}
                    summary_note = ""
                    if total_amount != paid_total:
                        diff = total_amount - paid_total
                        if not is_output and diff > 0:
                            summary_note = f"Kalan {fmt_tl(diff)} TL karşılığı Has Altın cari hesabınıza borç işlenmiştir."

                    trigger_transaction_notifications(
                        request=request, process_no=process_no, customer=customer,
                        items=item_data, payments=payments_ctx, totals=totals_ctx, summary_note=summary_note
                    )
                except Exception as e:
                    log.error(f"Bildirim hatası: {e}")

            resp = {'result': True, 'message': 'Hızlı işlem başarıyla kaydedildi.', 'process_no': process_no}
            if inv:
                resp['invoice_id'] = str(inv.id)
                resp['invoice_no'] = inv.invoice_no
                resp['invoice_url'] = invoice_url
            return JsonResponse(resp)

    except Products.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Ürün bulunamadı!'}, status=404)
    except InsufficientFXBalanceError as fxe:
        # YOL 3 (Acil Guard) — Negatif FX bakiyeyi engeller
        return JsonResponse({
            'error': True,
            'error_code': 'INSUFFICIENT_FX_BALANCE',
            'error_msg': str(fxe),
            'details': {
                'currency': fxe.currency,
                'available': str(fxe.available),
                'requested': str(fxe.requested),
                'account_name': fxe.account_name,
            }
        }, status=400)
    except ValidationError as ve:
        return JsonResponse({'error': True, 'error_msg': ve.messages[0]}, status=400)
    except Exception as e:
        log.exception("Hızlı İşlem Hatası")
        return JsonResponse({'error': True, 'error_msg': str(e)}, status=500)
