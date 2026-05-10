import json
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import F
from django.db.models.functions import Greatest
# invoice_process stub — invoices app Juwelier Plus'ta yok
from apps.process.invoice_process import create_retail_bulk_invoice
# --- UYGULAMA İÇİ MODELLER ---
from apps.products.models import Products
from apps.customers.models import Customers, CustomerLedger
from apps.process.models import Process, Payment
from apps.custody.models import CustomerCustodyLedger
from apps.gold_purchases.models import GoldPurchases
from apps.scraps.models import Scraps
from apps.definitions.categories.models import Categories
from apps.settings.models import StoreConfiguration
# PavoTerminal, PavoLocalSale kaldırıldı — pavo app Juwelier Plus'ta yok

# --- PROCESS VE PAVO YARDIMCILARI ---
from apps.process.notifications import trigger_transaction_notifications, fmt_tl
from apps.process.views import generate_process_no, update_product_stock
from apps.process.fast_views import (
    _calculate_and_save_profit,
    _build_currency_extra,
    _get_approval_status,
    _process_currency_exchange,
    _process_barter_currency_entry,
    _resolve_or_create_cash_account,
)
from apps.helpers.numbers import parse_decimal_locale
from apps.bracelets.models import Bracelets

# --- R-FAZ 1: Hurda havuz servisi (Onarım Fazı 9 ile aynı temel) ---
from apps.scraps.views import (
    find_scrap_pool_by_selected_karat,
    update_scrap_pool_weighted_mileage,
    extract_scrap_karat_label,
    d_quantize,
)

# --- R-FAZ 2: Bilezik havuz servisi (B-Faz 1 ile aynı temel) ---
from apps.bracelets.views import (
    find_bracelet_pool_by_name,
    update_bracelet_pool_weighted_mileage,
)

# --- FAZ 3: StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
# pavo_process kaldırıldı — pavo app Juwelier Plus'ta yok
from apps.banking.services import (
    PaymentBankAccountValidator,
    POSCommissionService,
    FXBalanceGuard,
    FXBalanceReader,
    InsufficientFXBalanceError,
    get_currency_code_from_product,
)

# --- FAZ 33 + 33.2: Cari ledger SSOT — SATIŞ kuru ile borç yazımı ---
# Eski kod (FAZ 33 öncesi): doğrudan `CustomerLedger.objects.create()` ile
# Products.sale_price_eur kullanıyordu — kuru doğru ama audit kontrolleri
# (created_by, ip_address, user_agent) eksikti.
#
# FAZ 33 (deneme): get_current_has_rate() (ALIŞ kuru) + LedgerService
# sarmalaması — audit doğru ama kur YANLIŞ. Müşteriye satış SATIŞ kuruyla
# yapılırken (Process.price_hs = total_tl / sale_rate), cari ledger ALIŞ
# kuruyla yazılınca fiş ↔ cari hesap arası spread farkı oluşuyordu
# (kullanıcı saha geri bildirimi: cari 14.048 HS, fiş 13.993 HS).
#
# FAZ 33.2 (doğru çözüm): Müşteriye satış neyse, cari borç da o olsun.
# `Products.sale_price_eur` (SATIŞ kuru) tek SSOT olarak kullanılır;
# `Process.price_hs` ile aynı kuru paylaşan, audit alanları dolu, doğrudan
# CustomerLedger satırı yazılır. LedgerService.write_debt bypass edilir
# çünkü o servis hardcoded ALIŞ kuru çağırıyor (write_debt yalnız
# tahsilat sonrası overpayment / iade gibi ALIŞ kuru istenen senaryolar
# için uygundur).
from apps.customers.services.ledger import LedgerService

log = logging.getLogger(__name__)


# ==========================================
# YARDIMCI VE DÖNÜŞÜM FONKSİYONLARI
# ==========================================

def _dec(x, q='0.01'):
    """Güvenli Decimal dönüşümü (Fast views standardı)."""
    try:
        return Decimal(str(x)).quantize(Decimal(q), rounding=ROUND_HALF_UP)
    except:
        return Decimal('0.00')


# Troy Ons → Gram dönüşüm sabiti (Kitco fallback için)
_TROY_OZ_TO_GRAM = Decimal('31.1034768')


def _get_kitco_eur_per_gram():
    """
    KitcoPriceCache'ten EUR/GOLD/OZ değerlerini okur, gram cinsine çevirir.

    Almanya pazarı (juwelier_project) için Has Altın fiyat fallback'i:
    PriceService (PriceProvider/PriceQuote — Türkiye altyapısı) ve
    Products('Has Altın') (Türkiye fixture) boş kaldığında devreye girer.

    Returns:
        tuple (buy_eur_per_gram, sale_eur_per_gram) — bid/ask gram'a bölünmüş.
        Veri yoksa veya hata olursa (Decimal('0'), Decimal('0')).
    """
    try:
        from apps.live_board.models import KitcoPriceCache
        row = KitcoPriceCache.objects.filter(
            currency=KitcoPriceCache.Currency.EUR,
            metal_type=KitcoPriceCache.MetalType.GOLD,
            unit=KitcoPriceCache.Unit.OZ,
        ).first()
        if not row:
            return Decimal('0'), Decimal('0')
        bid_oz = Decimal(str(row.bid_price or 0))
        ask_oz = Decimal(str(row.ask_price or 0))
        if bid_oz <= 0 and ask_oz <= 0:
            return Decimal('0'), Decimal('0')
        buy_per_gram = (bid_oz / _TROY_OZ_TO_GRAM).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if bid_oz > 0 else Decimal('0')
        sale_per_gram = (ask_oz / _TROY_OZ_TO_GRAM).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if ask_oz > 0 else Decimal('0')
        return buy_per_gram, sale_per_gram
    except Exception:
        return Decimal('0'), Decimal('0')


def _get_store_config(user):
    """Mağaza konfigürasyonunu getirir."""
    store = getattr(user, 'store', None)
    if not store:
        return None
    config, _ = StoreConfiguration.objects.get_or_create(store=store)
    return config


def _is_unique_single_product(product: Products) -> bool:
    """
    Ürünün tekil/barkodlu olup olmadığını kontrol eder.

    ÖNEMLİ: Hurda ürünler (is_scrap=True) bu kontrole DAHİL DEĞİLDİR.
    Hurdalar gram bazlı, havuzlanmış (milyem bazında tek kayıt) stok
    modeliyle çalışır; bu nedenle barkodlu/tekil ürün gibi ele alınmaları
    yanlıştır. Hurda için stok kontrolü StockSnapshot.stock_gram üzerinden
    yapılır ve GoldPurchases (tezgah) kontrolüne girmez.
    """
    return bool(product.barcode)


# ============================================================================
# FAZ B2 / ONARIM FAZI 2: MASAK TL NORMALIZASYONU
# ============================================================================
# Frontend spoofing veya hatali Process.amount (farkli para biriminde) durumu
# icin defansif bir TL yeniden-hesaplama katmani. _check_legal_limits_retail
# artik hem gelen net_total_abs'i hem de Process queryset'inden turetilen
# TL toplami alabilir; ikisinin MAKSIMUMUNU kullanarak MASAK'i asla hafife
# almaz (FAIL-SAFE prensibi).
# ============================================================================

def _normalize_net_total_to_tl(processes, fallback_abs):
    """
    Process satirlarindan TL cinsinden kesin toplami turetir.

    Her Process satiri icin:
      - Urun TRY/HS/HG cinsinden fiyatlandirilmissa Process.amount oldugu gibi
        TL sayilir (zaten sistem varsayimi).
      - Urun USD/EUR/CAD/QAR cinsinden fiyatlandirilmissa PriceService ile
        anlik kur bulunup carpilir.

    Hata durumunda fallback_abs geri dondurulur (calisan sistemi bozmaz).

    Returns:
        Decimal: TL cinsinden net_total_abs (her zaman pozitif)
    """
    from decimal import Decimal as _D
    # Faz 13: SSOT — desteklenen tüm yabancı para birimleri (TRY hariç).
    from apps.banking.services import SUPPORTED_FX_CURRENCIES as _SSOT_FX
    FIAT_FOREIGN = {c for c in _SSOT_FX if c != 'TRY'}

    try:
        total_tl = _D('0.00')
        for p in processes or []:
            amount = _dec(getattr(p, 'amount', 0))
            # Satis (+) / Alis (-) ayrimi korunur; mutlak degeri MASAK'ta
            # kullanilir.
            sign = _D('1.00') if (p.transaction_type in ('SALE', 'ORDER_IN')) else _D('-1.00')

            product = getattr(p, 'product', None)
            price_cur = None
            if product is not None:
                price_cur = (getattr(product, 'price_currency', '') or '').upper()

            if price_cur in FIAT_FOREIGN:
                # Yabanci fiat -> TL'ye cevir.
                # fast_views'taki _get_exchange_rate_for_currency helper'i
                # USDTRY/EURTRY vb. Products kayitlarindan anlik kuru okur.
                rate = None
                try:
                    from apps.process.fast_views import _get_exchange_rate_for_currency
                    rate = _get_exchange_rate_for_currency(price_cur)
                except Exception as _rate_exc:
                    log.warning(
                        f"_normalize_net_total_to_tl: kur cozme hatasi "
                        f"({_rate_exc}). Process id={getattr(p, 'id', None)}"
                    )
                    rate = None

                if rate is None or rate == 0:
                    # Son care: kur bulunamadi. Process.amount'un zaten TL
                    # oldugunu varsay ve oldugu gibi topla (sistemin mevcut
                    # davranisi). Bu durumu logla ki operator fark etsin.
                    log.warning(
                        f"_normalize_net_total_to_tl: {price_cur} kur "
                        f"bulunamadi. Process amount TL gibi toplaniyor. "
                        f"Process id={getattr(p, 'id', None)}"
                    )
                    total_tl += amount * sign
                else:
                    total_tl += amount * _D(str(rate)) * sign
            else:
                total_tl += amount * sign

        return abs(total_tl)
    except Exception as exc:
        log.warning(
            f"_normalize_net_total_to_tl basarisiz, fallback kullaniliyor: {exc}"
        )
        return fallback_abs


def _get_masak_limits(config):
    """
    StoreConfiguration'dan MASAK/Fatura/Nakit limitlerini okur; yoksa
    yasal default'lari doner. Tum donus degerleri Decimal'dir.
    """
    def _cfg(name, default):
        if not config:
            return default
        val = getattr(config, name, None)
        if val is None:
            return default
        try:
            return Decimal(str(val))
        except Exception:
            return default

    # Default'lar Almanya AML (Geldwäschegesetz) baz alındı:
    # 10.000 EUR nakit kimlik tespiti eşiği. StoreConfiguration alanları
    # tanımlanırsa (örn. Türkiye pazarı için 30k/36k/185k TL) override olur.
    return {
        'CASH_LIMIT': _cfg('cash_limit_tl', Decimal('10000.00')),
        'INVOICE_LIMIT': _cfg('invoice_limit_tl', Decimal('0.00')),
        'MASAK_LIMIT': _cfg('masak_limit_tl', Decimal('10000.00')),
    }


def _check_legal_limits_retail(config, net_total_abs, is_output, payment_type, is_pos_flow, pos_mode, is_manual,
                               manual_cash, customer, processes=None):
    """
    Perakende için Yasal Limit Kontrolleri (2026)

    ONARIM FAZI 2: 'processes' parametresi opsiyoneldir. Verilirse TL
    normalizasyonu yeniden yapilir ve MASAK kontrolu her zaman asli TL
    tutari uzerinden (en yuksek deger) yapilir.
    """
    limits = _get_masak_limits(config)
    CASH_LIMIT = limits['CASH_LIMIT']
    INVOICE_LIMIT = limits['INVOICE_LIMIT']
    MASAK_LIMIT = limits['MASAK_LIMIT']

    # FAIL-SAFE: Eger processes verilmisse, TL'ye normalize edilen toplami
    # da hesapla ve buyuk olanini kullan. Bu, yabanci para birimli
    # urunlerin MASAK tabanindan kacmasini onler.
    if processes is not None:
        try:
            tl_total = _normalize_net_total_to_tl(processes, net_total_abs)
            if tl_total > net_total_abs:
                log.info(
                    f"MASAK TL normalizasyonu: orijinal={net_total_abs}, "
                    f"TL normalize={tl_total}. En yuksek deger kullanildi."
                )
                net_total_abs = tl_total
        except Exception as exc:
            log.warning(
                f"MASAK TL normalizasyonu basarisiz ({exc}). "
                f"Orijinal net_total_abs kullaniliyor."
            )

    # Türkiye yasal limitleri kaldırıldı (Almanya akışı)
    enforce_cash = False
    enforce_invoice = False
    enforce_masak = False
    enforce_customer_always = getattr(config, 'enforce_customer_always', False) if config else False

    # 0. Tutar Bağımsız Müşteri Zorunluluğu (Mağaza Ayarı)
    # Hızlı İşlem ile simetrik kontrol — fast_views._check_legal_limits ile aynı kural.
    if enforce_customer_always and not customer:
        return 'Mağaza ayarlarında her işlem için müşteri seçimi zorunlu kılınmıştır. Lütfen müşteri seçiniz.'

    # 1. Nakit Limiti (30.000 TL)
    if enforce_cash and net_total_abs > CASH_LIMIT:
        error_msg = '30.000 TL üzeri tutarlar yasa gereği Nakit işlem yapılamaz. Lütfen Kart veya Havale kullanınız.'

        # Manuelde nakit girilmişse ve limit aşılmışsa
        if is_manual and manual_cash > 0:
            return 'İşlem toplamı 30.000 TL\'yi geçtiği için yasa gereği hiç nakit tahsilat yapılamaz.'

        # Sadece nakit seçiliyse (Manuel değil, tek tuş Nakit)
        if not is_manual and not is_pos_flow and payment_type == 'CASH':
            return error_msg

        # POS üzerinden nakit deneniyorsa
        if is_pos_flow and pos_mode == 'CASH':
            return '30.000 TL üzeri tutarlar POS üzerinden Nakit olarak işlenemez.'

    # 2. Fatura Limiti (36.000 TL)
    if enforce_invoice and net_total_abs >= INVOICE_LIMIT:
        if not customer:
            return f'{INVOICE_LIMIT} TL ve üzeri işlemlerde fatura zorunluluğu nedeniyle müşteri seçimi zorunludur.'

    # 3. MASAK Limiti (185.000 TL)
    if enforce_masak and net_total_abs >= MASAK_LIMIT:
        if not customer:
            return f'{MASAK_LIMIT} TL ve üzeri işlemlerde MASAK gereği müşteri seçimi zorunludur.'

        missing = []
        if not (getattr(customer, 'identification_number', '') or '').strip(): missing.append('TCKN')
        if not getattr(customer, 'identification_front_image', None): missing.append('Kimlik Ön Yüz')
        if not getattr(customer, 'identification_back_image', None): missing.append('Kimlik Arka Yüz')
        if missing:
            return 'MASAK gereği müşteri kimlik bilgileri zorunludur. Eksikler: ' + ', '.join(missing)

    return None


def _handle_pavo_transaction(raw_pavo, total_amount, pos_reference):
    """
    Pavo POS yanıtını işler ve doğrular. (Fast views mantığı ile aynı)
    Dönenler: (pavo_data, updated_total_amount, pavo_inquiry_data, pavo_invoice_no, pavo_sale_number, pavo_terminal_serial, pos_reference)
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


def book_custody_tx(
        *, customer, store, process_no,
        direction,
        product=None,
        amount_hs=Decimal('0'),
        quantity_piece=0,
        quantity_gram=Decimal('0'),
        user=None,
        desc=None
):
    """Emanet defterine kayıt atar."""
    if (amount_hs <= 0) and (quantity_piece <= 0 and quantity_gram <= 0):
        return

    is_returned = (direction == CustomerCustodyLedger.CUSTODY_OUT)

    CustomerCustodyLedger.objects.create(
        customer=customer,
        store=store,
        product=product,
        process_no=process_no,
        custody_type=direction,
        amount_hs=amount_hs,
        quantity_piece=quantity_piece,
        quantity_gram=quantity_gram,
        description=(desc or ""),
        created_by=user,
        received_by=(user if direction == CustomerCustodyLedger.CUSTODY_IN else None),
        delivered_by=(user if direction == CustomerCustodyLedger.CUSTODY_OUT else None),
        is_returned=is_returned,
    )


@login_required(login_url='login')
@transaction.atomic
def add_scrap_to_process(request):
    if request.method != "POST":
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=400)

    store = request.user.store
    process_no = request.POST.get('process_no') or generate_process_no()

    # UI'dan gönderilen mevcut ürün id'sini alıyoruz
    product_id = request.POST.get('product_id')
    scrap_name = request.POST.get('scrap_name')

    # R-FAZ 1 — Sayfa bağlamı: GOLD/SILVER. Toptan scrap_add ile aynı kontrat.
    scrap_material_type = (request.POST.get('material_type') or 'GOLD').upper()
    if scrap_material_type not in ('GOLD', 'SILVER'):
        scrap_material_type = 'GOLD'

    try:
        gram = parse_decimal_locale(request.POST.get('gram'), default="0", places=3)
        gram = d_quantize(gram, 3)
        raw_mileage = parse_decimal_locale(request.POST.get('product_mileage'), default="0")
        product_mileage = Decimal(int(raw_mileage))
    except Exception:
        return JsonResponse({'error': True, 'error_msg': 'Sayısal değerlerde hata.'}, status=400)

    if gram <= 0:
        return JsonResponse({'error': True, 'error_msg': 'Gram 0 dan büyük olmalıdır.'}, status=400)

    try:
        category = Categories.objects.filter(name__icontains='Hurda').first()
        if not category:
            category = Categories.objects.create(name='Hurda', store=store)
    except Exception:
        return JsonResponse({'error': True, 'error_msg': 'Hurda kategorisi sorunu.'}, status=500)

    # FAZ 3: Has Altın fiyatını PriceService'den al (Fallback: Products tablosu)
    try:
        hs_data = PriceService.get_price('GOLD_24K')
        hs_buy_price_eur = _dec(hs_data.get('buy_tl', Decimal('0')))
    except Exception:
        hs_buy_price_eur = Decimal('0')

    # Formdan gelen manuel TL fiyatlarını ÖNCE oku —
    # böylece has kuru bulunamazsa bile kullanıcının elle girdiği fiyat çalışır.
    manual_unit_price = _dec(request.POST.get('unit_price', 0))
    manual_total_amount = _dec(request.POST.get('total_amount', 0))

    if hs_buy_price_eur <= 0:
        hs_product = Products.objects.filter(name__icontains='Has Altın').only('buy_price_eur', 'sale_price_eur').first()
        if hs_product:
            hs_buy_price_eur = _dec(getattr(hs_product, 'buy_price_eur', 0))
        # Almanya/juwelier_project fallback: Kitco EUR/gram (bid)
        if hs_buy_price_eur <= 0:
            kitco_buy, _kitco_sale = _get_kitco_eur_per_gram()
            if kitco_buy > 0:
                hs_buy_price_eur = kitco_buy
        # Has kuru hâlâ 0 ise ve kullanıcı manuel fiyat girmemişse reddet
        if hs_buy_price_eur <= 0 and manual_unit_price <= 0:
            return JsonResponse(
                {'error': True, 'error_msg': 'Has Altın alış fiyatı 0 olamaz. Lütfen Birim Fiyat (TL) alanını manuel girin.'},
                status=400
            )

    # Birim Has ve TL Hesaplamaları
    unit_buy_price_hs = round(product_mileage / Decimal('1000'), 3)
    calculated_hs = (gram * product_mileage) / Decimal('1000')

    if manual_unit_price > 0:
        unit_buy_price_eur = manual_unit_price
    else:
        unit_buy_price_eur = unit_buy_price_hs * hs_buy_price_eur

    if manual_total_amount > 0:
        calculated_amount_eur = manual_total_amount
    else:
        calculated_amount_eur = calculated_hs * hs_buy_price_eur

    # ----------------------------------------------------------------------
    # R-FAZ 1 — HAVUZ EŞLEŞTİRME (Onarım Fazı 9 ile aynı temel)
    # Havuz anahtarı = (store + category + material_type + canonical karat).
    # Milyem'den BAĞIMSIZ — "14 Ayar" 595/605 milyem aynı havuza düşer.
    # ----------------------------------------------------------------------
    existing_product = None
    if product_id:
        existing_product = Products.objects.filter(
            id=product_id, store=store, is_deleted=False,
        ).first()

    if not existing_product:
        existing_product = find_scrap_pool_by_selected_karat(
            store=store, category=category,
            scrap_name=scrap_name,
            fallback_mileage=product_mileage,
            is_scrap=True,
            material_type=scrap_material_type,
        )

    # Canonical isim — toptan scrap_add ile aynı kalıp.
    canonical_karat_label = extract_scrap_karat_label(
        scrap_name=scrap_name,
        fallback_mileage=product_mileage,
        material_type=scrap_material_type,
    )
    final_scrap_name = (
        canonical_karat_label
        or (scrap_name.strip() if scrap_name else '')
        or f"{int(product_mileage)} Milyem Hurda"
    )

    # R-FAZ 1 — BENZERSIZ STOK REFERANSI:
    # Process satırının UUID'i StockLedger.ref_id olarak kullanılır.
    # Aynı retail process_no'ya bağlı çoklu hurda satırlarının her biri
    # kendi UUID'i ile benzersiz iz bırakır → R-Faz 5'te cancel_row tek
    # satırı izole şekilde reverse edebilir.
    process_id = uuid.uuid4()
    stock_ref_id = str(process_id)

    # ----------------------------------------------------------------------
    # 2. STOK VE HAVUZ GÜNCELLEMESİ (`scrap_add` mantığı)
    # ----------------------------------------------------------------------
    if existing_product:
        product = existing_product

        # R-FAZ 1 — REVIVAL RESET (Onarım Fazı 6 / Bug 6 ile aynı semantik):
        # Soft-delete'lenmiş havuza yeniden giriş yapılıyorsa eski stok
        # kalıntısı (StockSnapshot.stock_gram > 0) WAC milyemini bozar.
        # Önce kalıntıyı sıfırla, sonra normal giriş işle.
        scrap_record = Scraps.objects.filter(store=store, product=product).first()
        # UAT REGRESYON FIX (2026-04-29): `Products.gram` legacy alan olarak
        # negatif kalabiliyor (örn. eski iptal akışlarından kalan stale veri).
        # Aktif + silinmemiş havuzlar `was_revival=False` üretiyor ve gram
        # sıfırlanmıyordu; complete_process'te `prod.save()` çağrısı
        # ValidationError fırlatıyordu ("Gram negatif olamaz."). Stale negatif
        # gram tespit edilirse de revival akışına sok → gram=0'a sıfırla.
        _stale_negative_gram = (
            getattr(product, 'gram', None) is not None
            and Decimal(str(product.gram)) < Decimal('0')
        )
        was_revival = (
            (scrap_record and (scrap_record.is_deleted or scrap_record.is_active is False))
            or product.is_active is False
            or _stale_negative_gram
        )

        if scrap_record:
            _reset_fields = []
            if scrap_record.is_deleted:
                scrap_record.is_deleted = False
                _reset_fields.append('is_deleted')
            if scrap_record.is_active is False:
                scrap_record.is_active = True
                _reset_fields.append('is_active')
            if _reset_fields:
                scrap_record.save(update_fields=_reset_fields)
        else:
            Scraps.objects.create(store=store, product=product, created_by=request.user)

        if product.is_active is False:
            Products.objects.filter(id=product.id).update(is_active=True)
            product.is_active = True

        if was_revival:
            try:
                _stale_snap = (
                    StockSnapshot.objects
                    .select_for_update()
                    .filter(product=product, store=store)
                    .first()
                )
                _stale_gram = (
                    Decimal(str(_stale_snap.stock_gram))
                    if (_stale_snap and _stale_snap.stock_gram is not None)
                    else Decimal('0')
                )
                _stale_pieces = int(_stale_snap.stock_pieces or 0) if _stale_snap else 0
                if _stale_gram > 0 or _stale_pieces > 0:
                    StockService.adjustment(
                        product=product, store=store,
                        actual_gram=Decimal('0'), actual_pieces=0,
                        ref_id=f"retail_scrap_revival_{product.id}_{stock_ref_id}",
                        user=request.user,
                        notes=(
                            "Perakende hurda havuzu yeniden açılışı: "
                            "önceki silme/iptal sonrası kalan stok temizlendi"
                        ),
                    )
            except Exception as _revival_err:
                log.error(
                    "retail_scrap_revival_reset failed (product_id=%s, ref=%s): %s",
                    product.id, stock_ref_id, _revival_err,
                )
            Products.objects.filter(id=product.id).update(
                gram=Decimal('0'), product_mileage=Decimal('0'),
            )
            product.gram = Decimal('0')
            product.product_mileage = Decimal('0')

        # R-FAZ 7 — STOK ERTELEME:
        # WAC milyem güncellemesi VE StockService.record_entry çağrısı
        # complete_process'e ertelendi. Sepete eklemede yalnızca havuz
        # eşleştirmesi + revival reset yapılır; gerçek stok hareketi
        # tahsilat anında bilezik/toptan deseniyle simetrik şekilde işler.
        # Burada Products.gram da artırılmaz — complete_process bunu
        # update_product_stock + R-Faz 6 Greatest+filter().update() ile yapar.

    else:
        # R-FAZ 7 — YENİ HAVUZ TASLAĞI:
        # Stok hareketi olmayan boş havuz (gram=0). Hayalet filtre
        # (StockSnapshot.stock_gram=0 + ever_sold=False) bu kaydı
        # listede gizler; complete_process'te ilk record_entry düştüğünde
        # filtre koşulu çözülür ve havuz görünür hale gelir.
        product = Products.objects.create(
            store=store,
            category=category,
            name=final_scrap_name,
            gram=Decimal('0'),
            product_mileage=product_mileage,
            buy_price_hs=unit_buy_price_hs,
            sale_price_hs=unit_buy_price_hs,
            is_scrap=True,
            is_active=True,
            is_completed=False,
            buy_price_eur=unit_buy_price_eur,
            material_type=scrap_material_type,
        )
        Scraps.objects.create(store=store, product=product, created_by=request.user)

    # İşlem Geçmişi (Process) Kaydı — taslak (IN_PROGRESS).
    # complete_process tahsilat anında bu satırı bulup hurda PURCHASE
    # dalında stok girişini ve WAC milyem güncellemesini yapacak.
    # waiting_stock=False (bilezik perakende ile tutarlı — ORDER_IN değil,
    # checkout-bekleyen taslak). Cart-time cancel'da cancel_stock_entry
    # eşleşme bulamayıp no-op geçer (raise_if_not_found=False).
    Process.objects.create(
        id=process_id,
        store=store,
        process_no=process_no,
        process_type='RETAIL',
        transaction_type='PURCHASE',
        product=product,
        employee=request.user,
        piece=1,
        gram=gram,
        process_mileage=str(product_mileage),
        price_hs=calculated_hs,
        amount=calculated_amount_eur,  # Kuyumcunun belirlediği toplam TL
        unit_price=unit_buy_price_eur,  # Kuyumcunun belirlediği birim TL
        is_status='IN_PROGRESS',
        waiting_stock=False,
    )

    return JsonResponse({
        'result': True,
        'message': 'Hurda girişi işleme eklendi ve stoğa yansıdı.',
        'process_no': process_no
    })


@login_required(login_url='login')
@transaction.atomic
def add_bracelet_to_retail_process(request):
    """
    Perakende ekranından Bilezik Girişi.

    R-FAZ 2 — HAVUZ ENTEGRASYONU (Bilezik B-Faz 1 ile aynı temel):
        Aynı isimdeki AKTİF bilezik havuzuna birikim yapılır; yoksa yeni
        havuz açılır. Her giriş kendi Process satırına UUID ile bağlanır
        (R-Faz 5 cancel için izolasyon temeli).

    Stok hareketi DAİMA `complete_process` tarafında, `update_product_stock`
    aracılığıyla gerçekleşir — bu satırda yalnızca Process draft satırı ve
    snapshot başlatma yapılır.
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek'}, status=405)

    try:
        store = request.user.store

        # Form Verileri
        process_no = request.POST.get('process_no') or generate_process_no()
        name = (request.POST.get('bracelet_name') or "Bilezik").strip()

        gram = parse_decimal_locale(request.POST.get('gram'), default="0", places=3)
        mileage = parse_decimal_locale(request.POST.get('product_mileage'), default="0")

        manual_unit_price = parse_decimal_locale(request.POST.get('unit_price'), default="0", places=2)
        manual_total_amount = parse_decimal_locale(request.POST.get('total_amount'), default="0", places=2)

        if gram <= 0:
            return JsonResponse({'result': False, 'error_msg': 'Gram 0 dan büyük olmalı.'}, status=400)

        # 1. Kategori Kontrolü
        category = Categories.objects.filter(name__icontains='Bilezik').first()
        if not category:
            category = Categories.objects.create(name='Bilezik', store=store)

        unit_hs_price = (Decimal(mileage) / Decimal('1000')).quantize(Decimal('0.001'))

        # ----------------------------------------------------------
        # R-FAZ 2 — HAVUZ EŞLEŞTİRME
        # Aynı isimdeki aktif bilezik havuzu varsa onu kullan, yoksa yarat.
        # ----------------------------------------------------------
        existing_product = find_bracelet_pool_by_name(
            store=store, category=category, name=name,
        )

        if existing_product:
            product = existing_product

            # R-FAZ 2 — REVIVAL RESET (B-Faz 1 paralelinde):
            # Soft-delete'lenmiş havuza yeniden giriş yapılıyorsa stok
            # kalıntısını sıfırla; aksi halde sonraki update_product_stock
            # eski stok üzerine biner ve WAC bozulur.
            bracelet_record = Bracelets.objects.filter(store=store, product=product).first()
            was_revival = (
                (bracelet_record and (bracelet_record.is_deleted or bracelet_record.is_active is False))
                or product.is_active is False
            )

            if bracelet_record:
                _reset_fields = []
                if bracelet_record.is_deleted:
                    bracelet_record.is_deleted = False
                    _reset_fields.append('is_deleted')
                if bracelet_record.is_active is False:
                    bracelet_record.is_active = True
                    _reset_fields.append('is_active')
                if _reset_fields:
                    bracelet_record.save(update_fields=_reset_fields)
            else:
                Bracelets.objects.create(store=store, product=product, created_by=request.user)

            if product.is_active is False:
                Products.objects.filter(id=product.id).update(is_active=True)
                product.is_active = True

            if was_revival:
                try:
                    _stale_snap = (
                        StockSnapshot.objects
                        .select_for_update()
                        .filter(product=product, store=store)
                        .first()
                    )
                    _stale_gram = (
                        Decimal(str(_stale_snap.stock_gram))
                        if (_stale_snap and _stale_snap.stock_gram is not None)
                        else Decimal('0')
                    )
                    _stale_pieces = int(_stale_snap.stock_pieces or 0) if _stale_snap else 0
                    if _stale_gram > 0 or _stale_pieces > 0:
                        StockService.adjustment(
                            product=product, store=store,
                            actual_gram=Decimal('0'), actual_pieces=0,
                            ref_id=f"retail_bracelet_revival_{product.id}_{process_no}",
                            user=request.user,
                            notes=(
                                "Perakende bilezik havuzu yeniden açılışı: "
                                "önceki silme/iptal sonrası kalan stok temizlendi"
                            ),
                        )
                except Exception as _revival_err:
                    log.error(
                        "retail_bracelet_revival_reset failed (product_id=%s, process_no=%s): %s",
                        product.id, process_no, _revival_err,
                    )
                Products.objects.filter(id=product.id).update(
                    gram=Decimal('0'), product_mileage=Decimal('0'),
                )
                product.gram = Decimal('0')
                product.product_mileage = Decimal('0')

            # Snapshot satırı zaten var (gerek varsa get_or_create — defensive)
            StockSnapshot.objects.get_or_create(
                product=product, store=store,
                defaults={
                    'stock_gram': Decimal('0.000'),
                    'stock_pieces': 0,
                    'weighted_avg_cost_hs': Decimal('0.000'),
                    'weighted_avg_cost_eur': Decimal('0.00'),
                }
            )

        else:
            # YENİ HAVUZ — bilezik kategorisi, is_gram_bullion=True.
            product = Products.objects.create(
                store=store,
                category=category,
                name=name,
                gram=gram,
                product_mileage=str(mileage),
                buy_price_hs=unit_hs_price,
                sale_price_hs=unit_hs_price,
                is_gram_bullion=True,
                created_by=request.user,
                created_on=timezone.now()
            )

            Bracelets.objects.create(
                store=store, product=product, created_by=request.user
            )

            StockSnapshot.objects.get_or_create(
                product=product, store=store,
                defaults={
                    'stock_gram': Decimal('0.000'),
                    'stock_pieces': 0,
                    'weighted_avg_cost_hs': Decimal('0.000'),
                    'weighted_avg_cost_eur': Decimal('0.00'),
                }
            )

        # R-FAZ 2 — Process satırı UUID ile yaratılır; cancel_row R-Faz 5'te
        # str(p.id) üzerinden tekil ledger izlemesi yapacak.
        process_id = uuid.uuid4()
        total_hs = (gram * mileage) / Decimal('1000')

        Process.objects.create(
            id=process_id,
            store=store,
            process_no=process_no,
            process_type='RETAIL',
            transaction_type='PURCHASE',
            product=product,
            employee=request.user,
            piece=1,
            gram=gram,
            process_mileage=str(mileage),
            price_hs=total_hs,
            unit_price=manual_unit_price,
            amount=manual_total_amount,
            is_status='IN_PROGRESS'
        )

        return JsonResponse({
            'result': True,
            'process_no': process_no,
            'message': 'Bilezik listeye eklendi.'
        })

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


@login_required(login_url='login')
def get_sales(request):
    try:
        order_column = request.GET.get('order_column', 'date')
        order_direction = request.GET.get('order_direction', 'asc')

        if order_direction == 'desc':
            order_column = f"-{order_column}"

        qs = Process.objects.filter(
            is_deleted=False,
            is_status='IN_PROGRESS',
            process_type='RETAIL',
            employee=request.user
        ).select_related('product').order_by(order_column)

        vat_rate = Decimal('0.20')
        data = []
        for p in qs:
            labor_val = _dec(getattr(p, 'labor_amount', 0) or 0)
            vat_val = (labor_val * vat_rate).quantize(Decimal('0.01')) if labor_val else Decimal('0')
            data.append({
                'id': str(p.id),
                'product__name': p.product.name if p.product else '',
                'product__id': p.product.id if p.product else None,
                'gram': float(p.gram or 0),
                'piece': int(p.piece or 0),
                'process_mileage': p.process_mileage or '',
                'is_custody': (p.process_mileage == 'CUSTODY'),
                'transaction_type': p.transaction_type,
                'process_type': p.process_type,
                'process_no': p.process_no,
                'unit_price': float(_dec(p.unit_price or 0)),
                'amount': float(_dec(p.amount or 0)),
                'date': p.date.isoformat() if p.date else '',
                'price_hs': float(_dec(p.price_hs or 0, '0.001')),
                'labor_price': float(labor_val),
                'labor_vat': float(vat_val),
            })

        return JsonResponse({"data": data}, safe=False)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required(login_url='login')
def check_retail_compliance(request):
    """
    Ön kontrol fonksiyonu. (Helper fonksiyonu kullanır)
    """
    if request.method != "POST":
        return JsonResponse({'ok': False, 'error_msg': 'Geçersiz istek.'}, status=405)

    try:
        process_no = request.POST.get('process_no')
        customer_id = request.POST.get('customer_id')

        store = request.user.store
        processes = Process.objects.filter(store=store, process_no=process_no, process_type='RETAIL')

        if not processes.exists():
            return JsonResponse({'ok': False, 'error_msg': 'İşlem bulunamadı.'}, status=404)

        total_sales = sum(_dec(p.amount) for p in processes if p.transaction_type in ['SALE', 'ORDER_IN'])
        total_purchases = sum(_dec(p.amount) for p in processes if p.transaction_type in ['PURCHASE', 'RETURN'])

        grand_total = total_sales - total_purchases
        net_total_abs = abs(grand_total)
        is_output = (grand_total < 0)

        customer = None
        if customer_id:
            try:
                customer = Customers.objects.get(pk=customer_id, store=store)
            except Customers.DoesNotExist:
                pass

        # Geçici config kontrolü (Payment detayları olmadığı için 'CASH' varsayımıyla sadece Müşteri/Limit uyarısı vereceğiz)
        config = _get_store_config(request.user)
        # Ön kontrolde ödeme tipi bilinmiyor, bu yüzden sadece MASAK ve Fatura limiti için müşteri uyarısı veriyoruz
        # ONARIM FAZI 2: processes queryset'i gecerek MASAK TL normalizasyonunu aktiflestir.
        limit_err = _check_legal_limits_retail(
            config, net_total_abs, is_output,
            payment_type='UNKNOWN', is_pos_flow=False, pos_mode='', is_manual=False, manual_cash=0,
            customer=customer,
            processes=processes,
        )

        if limit_err:
            return JsonResponse({'ok': False, 'error_msg': limit_err})

        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error_msg': str(e)})


@login_required(login_url='login')
def add_process(request):
    if request.method != "POST":
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=400)

    store = request.user.store
    process_no = request.POST.get('process_no') or generate_process_no()

    record_id = request.POST.get('record_id')
    product_id = request.POST.get('product_id')

    unit_price = _dec(request.POST.get('unit_price'))
    sale_price_hs = _dec(request.POST.get('sale_price_hs'), '0.001')
    labor_price = _dec(request.POST.get('labor_price'))
    piece = int(request.POST.get('piece') or 0)
    gram = _dec(request.POST.get('gram'), '0.001')

    line_custody_raw = (
            request.POST.get('line_custody')
            or request.POST.get('is_custody_line')
            or request.POST.get('is_custody')
            or request.POST.get('custody')
    )
    line_is_custody = (
        str(line_custody_raw).lower() in ('1', 'true', 'on', 'yes')
        if line_custody_raw is not None else False
    )

    operation_type = (request.POST.get('operationType') or '').upper()
    waiting_stock = False
    customer_id = request.POST.get('customer_id')

    order_checked = (request.POST.get('order') == 'on')
    stock_checked = (request.POST.get('stock') == 'on')

    customer = None
    if customer_id:
        try:
            customer = Customers.objects.get(id=customer_id)
        except Customers.DoesNotExist:
            customer = None

    try:
        product = Products.objects.get(id=product_id)
    except Products.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Ürün bulunamadı.'}, status=404)

    # ------------------------------------------------------------------
    # ONARIM FAZI 3 / ADIM 1 — WATCH/DIAMOND PIECE FALLBACK
    # ------------------------------------------------------------------
    # UI barkod okuma akisinda WATCH/DIAMOND urunleri icin adet (piece)
    # input alani yoksa POST'ta piece=0 gelebilir. Bu durumda
    # StockService._validate_material_type_quantities() ValueError firlatir
    # (piece_count >= 1 zorunlu). Backend bu durumu sessiz bir fallback ile
    # kapatir: piece=1 atar, gram=0 yapar. Ayni ekranda cift tarama riskini
    # onlemek icin is_unique + barcode kontrolu zaten yukarida mevcut.
    # ------------------------------------------------------------------
    try:
        _mat_type = getattr(product, 'material_type', 'GOLD') or 'GOLD'
        if _mat_type in ('WATCH', 'DIAMOND'):
            if piece <= 0:
                piece = 1
                log.info(
                    f"retail add_process: WATCH/DIAMOND piece fallback "
                    f"uygulandi (product_id={product_id}, piece=1)"
                )
            # Saat/Pirlanta gram bazli degil - gram her zaman 0
            if gram and gram != Decimal('0'):
                gram = Decimal('0.000')
    except Exception as _mt_exc:
        log.warning(f"retail material_type fallback hatasi: {_mt_exc}")

    is_unique = _is_unique_single_product(product)

    if operation_type == 'SALE' and (product.barcode or is_unique) and getattr(product, 'is_completed', False):
        return JsonResponse(
            {'error': True,
             'error_msg': 'Bu ürün zaten satılmış. Tekrar satış yapılamaz; yalnızca iade/alış yapılabilir.'},
            status=400
        )

    record = Process.objects.get(id=record_id) if record_id else Process()
    # FAZ 3: Has Altın fiyatını PriceService'den al
    try:
        _hs_data = PriceService.get_price('GOLD_24K')
        hs_sale = _dec(_hs_data.get('sell_tl', Decimal('0')))
        hs_buy = _dec(_hs_data.get('buy_tl', Decimal('0')))
    except Exception:
        hs_sale = Decimal('0')
        hs_buy = Decimal('0')

    # Fallback 1: PriceService boş dönerse Products tablosundan oku (Türkiye legacy)
    if hs_sale <= 0:
        hs_product = Products.objects.filter(name__icontains='Has Altın').only('buy_price_eur', 'sale_price_eur').first()
        if hs_product and _dec(getattr(hs_product, 'sale_price_eur', 0)) > 0:
            hs_sale = _dec(hs_product.sale_price_eur)
            hs_buy = _dec(getattr(hs_product, 'buy_price_eur', None) or hs_sale)

    # Fallback 2 (Almanya/juwelier_project): KitcoPriceCache'ten EUR/gram türet
    if hs_sale <= 0:
        kitco_buy, kitco_sale = _get_kitco_eur_per_gram()
        if kitco_sale > 0:
            hs_sale = kitco_sale
            hs_buy = kitco_buy if kitco_buy > 0 else kitco_sale

    # Hâlâ 0 ise hiçbir kaynak yok demektir — kullanıcıyı yönlendir
    if hs_sale <= 0:
        return JsonResponse(
            {'error': True,
             'error_msg': 'Has Altın satış kuru tanımlı değil. PriceService/Products/Kitco kaynaklarının tümü boş.'},
            status=500
        )

    prod_karat = None
    try:
        if hasattr(product, 'karat') and product.karat:
            prod_karat = int(product.karat)
        elif hasattr(product, 'milyem') and product.milyem:
            prod_karat = int(round((Decimal(product.milyem) / Decimal('1000')) * 24))
    except Exception:
        prod_karat = None

    record.process_no = process_no
    record.product_id = product_id
    record.process_type = 'RETAIL'
    if customer:
        record.customer = customer

    record.hs_rate_sale_eur = hs_sale
    record.hs_rate_buy_eur = hs_buy
    if prod_karat:
        record.karat = prod_karat

    if stock_checked:
        if product.category.name in ('Döviz', 'Altın', 'Ziynet'):
            record.transaction_type = 'STOCK_IN'
        else:
            return JsonResponse({'error': True, 'error_msg': 'Lütfen bu kategorideki stok girişini panelden yapınız.'},
                                status=400)
    elif order_checked:
        if product.category.name in ('Döviz', 'Altın', 'Ziynet', 'Bilezik'):
            record.transaction_type = 'ORDER_IN'
        else:
            return JsonResponse({'error': True, 'error_msg': 'Bu kategorideki ürünler için sipariş oluşturulamıyor.'},
                                status=400)
    else:
        record.transaction_type = operation_type

    # ────────────────────────────────────────────────────────────────────
    # FAZ S6 (PIVOT 2026-04-23): WATCH/DIAMOND için döviz bazlı satış branch
    # ────────────────────────────────────────────────────────────────────
    # Mevcut Altın/Gümüş akışı tamamen korunur (else bloğu).
    # WATCH/DIAMOND için:
    #   - price_hs = 0 (Has yok — Products.clean() zaten sale_price_hs=0 garantili)
    #   - amount = frontend'in gönderdiği total_price (foreign panel hesapladı)
    #   - unit_price = frontend'in gönderdiği unit_price (TL birim)
    #   - gram = 0 (yukarıda fallback ile zaten 0'lanmıştı)
    #
    # Frontend tarafı (recalcAllForeign): fx_unit_price × fx_daily_rate = unit_price
    # Burada unit_price ve total_price ZATEN TL cinsinden geliyor — branch
    # sadece price_hs hesabını atlar.
    # ────────────────────────────────────────────────────────────────────
    if _mat_type in ('WATCH', 'DIAMOND'):
        # Adet bazlı, döviz × kur = TL akışı
        if piece <= 0:
            piece = 1
        gram = Decimal('0')
        price_hs = Decimal('0')  # Has yok
        # total_price frontend'den geldi; yoksa unit_price × piece
        post_total = _dec(request.POST.get('total_price'))
        if post_total > 0:
            total_amount = post_total
        else:
            total_amount = (Decimal(piece) * unit_price) if unit_price else Decimal('0')
    else:
        # ──── MEVCUT ALTIN/GÜMÜŞ AKIŞI — DOKUNULMADI ──────────────────
        # Fiyat ve Miktar Hesaplamaları
        if piece > 0:
            gram = Decimal('0')
            price_hs = sale_price_hs * Decimal(piece)
        else:
            piece = 0
            price_hs = sale_price_hs * gram

        if product.category.name == 'Döviz':
            # FAZ 54: Döviz için Has Altın dönüşümü — PriceService SSOT
            # Yukarıda (1019-1035) hesaplanan hs_sale / hs_buy kullanılır;
            # Products tablosunu ikinci kez sorgulamak SSOT ihlalidir.
            qty_fx = Decimal(piece) if piece else gram
            if operation_type == 'SALE':
                if hs_sale <= 0:
                    return JsonResponse({'error': True, 'error_msg': 'Has Altın satış kuru tanımlı değil.'}, status=500)
                price_hs = (_dec(product.buy_price_eur) * qty_fx) / hs_sale
            elif operation_type in ('PURCHASE', 'RETURN'):
                # PriceService sell-only konfigürasyonda hs_buy=0 gelebilir;
                # son çare olarak hs_sale'e düş (yukarıda > 0 garantili).
                effective_hs_buy = hs_buy if hs_buy > 0 else hs_sale
                if effective_hs_buy <= 0:
                    return JsonResponse({'error': True, 'error_msg': 'Has Altın alış kuru tanımlı değil.'}, status=500)
                price_hs = (_dec(product.sale_price_eur) * qty_fx) / effective_hs_buy

        qty = Decimal(piece) if piece else gram
        goods_amount = (qty * unit_price) if unit_price else Decimal('0')
        total_amount = goods_amount

    # Stok Kontrolleri
    if operation_type == 'SALE':
        try:
            if product.barcode or is_unique:
                # Barkodlu/tekil ürünler için GoldPurchases durumu kontrol edilir.
                # StockSnapshot yerine is_deleted + is_status (Tezgahta) kaynak alınır.
                gp = GoldPurchases.objects.filter(
                    product=product, is_deleted=False, is_status=True
                ).first()
                if not gp:
                    return JsonResponse(
                        {'error': True,
                         'error_msg': 'Bu ürün tezgahta değil veya daha önce satılmış.'},
                        status=400,
                    )
            elif getattr(product, 'is_currency', False):
                # ─── YOL 2 (SSOT Refactor): Döviz ürün için Payment SSOT bakiye kontrolü ───
                # StockSnapshot kullanılmaz — gerçek bakiye FX kasaların Payment toplamından okunur.
                currency_code = get_currency_code_from_product(product)
                if not currency_code:
                    return JsonResponse({
                        'error': True,
                        'error_msg': f'Geçersiz döviz ürünü: {product.name}'
                    }, status=400)

                fx_balance = FXBalanceReader.get_balance(store, currency_code)
                requested = Decimal(str(piece or 0))

                if order_checked:
                    waiting_stock = True
                elif requested <= 0:
                    return JsonResponse({
                        'error': True,
                        'error_msg': f'{currency_code} miktarı geçerli olmalıdır.'
                    }, status=400)
                elif fx_balance < requested:
                    return JsonResponse({
                        'error': True,
                        'error_code': 'INSUFFICIENT_FX_BALANCE',
                        'error_msg': (
                            f'Yetersiz {currency_code} bakiyesi: '
                            f'Mevcut {fx_balance} {currency_code}, İstenen {requested} {currency_code}.'
                        ),
                    }, status=400)
            else:
                # FAZ 3: StockSnapshot'tan stok kontrolü
                _snap = StockSnapshot.objects.filter(product=product, store=store).first()
                tot_pcs = _snap.stock_pieces if _snap else 0
                tot_wgt = _snap.stock_gram if _snap else Decimal('0')

                if product.is_gram_bullion:
                    if order_checked:
                        waiting_stock = True
                    else:
                        if tot_wgt < gram:
                            return JsonResponse({'error': True, 'error_msg': 'Yetersiz gram stoğu!'}, status=400)
                else:
                    if order_checked:
                        waiting_stock = True
                    else:
                        if tot_pcs < piece:
                            return JsonResponse({'error': True, 'error_msg': 'Yetersiz adet stoğu!'}, status=400)
        except Exception:
            return JsonResponse({'error': True, 'error_msg': 'Stok kontrolü sırasında hata.'}, status=500)

    elif operation_type == 'PURCHASE':
        if (product.barcode or is_unique) and getattr(product, 'is_completed', False) is False:
            return JsonResponse(
                {'error': True, 'error_msg': 'Stoktaki tekil/barkodlu ürün için tekrar alış yapılamaz.'}, status=400)

    elif operation_type == 'RETURN':
        if (product.barcode or is_unique) and getattr(product, 'is_completed', False) is False:
            return JsonResponse({'error': True, 'error_msg': 'Stokta olan tekil/barkodlu ürün için iade yapılamaz.'},
                                status=400)

    record.piece = int(piece) if piece else 0
    record.gram = _dec(gram, '0.001')
    record.unit_price = _dec(unit_price)
    record.price_hs = _dec(price_hs, '0.001')
    record.amount = _dec(total_amount)
    record.process_mileage = 'CUSTODY' if line_is_custody else '0'
    record.labor_amount = _dec(labor_price)
    record.employee = request.user
    record.store = store
    record.waiting_stock = waiting_stock

    try:
        record.save()
        return JsonResponse({'result': True, 'message': 'İşlem başarıyla kaydedildi.', 'process_no': record.process_no})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


@login_required(login_url='login')
def complete_process(request):
    """
    Perakende İşlem Tamamlama.
    - Pavo POS Entegrasyonu
    - Otomatik Fatura Oluşturma (Invoice Process Entegrasyonu)
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=405)

    _log = logging.getLogger(__name__)

    try:
        with transaction.atomic():
            user = request.user
            store = user.store
            config = _get_store_config(user)

            # 1. PARAMETRELER
            process_no = request.POST.get('process_no')
            if not process_no: return JsonResponse({'error': True, 'error_msg': 'İşlem no yok.'}, status=400)

            def _to_bool(val):
                return str(val).lower() in ('1', 'true', 'on', 'yes')

            custody_flag = _to_bool(request.POST.get('custody'))
            is_custody = _to_bool(request.POST.get('is_custody'))
            customer_id = request.POST.get('customer_id')

            is_manual = _to_bool(request.POST.get('is_manual_payment'))
            payment_type = (request.POST.get('paymentType') or 'CASH').upper()
            is_pos_flow = (payment_type == 'POS')
            pos_mode = (request.POST.get('pos_mode') or '').upper().strip()

            raw_pavo = request.POST.get('pavo_result_json') or ''
            pos_reference = request.POST.get('pos_reference') or ''
            installment = int(request.POST.get('installment') or 1)

            raw_status = request.POST.get('cash_status')
            cash_status = int(raw_status) if raw_status in ('0', '1') else None

            # 2. PROSESLERİ KİLİTLE VE ÇEK
            locked_ids = list(
                Process.objects.filter(process_no=process_no, process_type='RETAIL').select_for_update().values_list(
                    'id', flat=True))
            if not locked_ids: return JsonResponse({'error': True, 'error_msg': 'İşlem bulunamadı.'}, status=404)
            procs = Process.objects.filter(id__in=locked_ids).select_related('product')

            # Kontroller
            has_order_in = procs.filter(transaction_type='ORDER_IN').exists()
            custody_flag = custody_flag or procs.filter(process_mileage='CUSTODY').exists() or is_custody

            if not customer_id:
                if has_order_in or custody_flag or (is_manual or cash_status is None):
                    return JsonResponse({'error': True, 'error_msg': 'Bu işlem türü için müşteri zorunludur.'},
                                        status=400)

            customer = None
            if customer_id:
                customer = Customers.objects.select_for_update().get(id=customer_id)
                procs.update(customer=customer)

            # 3. TOPLAMLAR
            calc_sales = sum((_dec(p.amount) or 0) for p in procs if p.transaction_type in ['SALE', 'ORDER_IN'])
            calc_purchases = sum((_dec(p.amount) or 0) for p in procs if p.transaction_type in ['PURCHASE', 'RETURN'])
            grand_total = calc_sales - calc_purchases
            abs_total = abs(grand_total)
            is_output = (cash_status == 0) if cash_status is not None else (grand_total < 0)

            # 4. LİMİT KONTROLLERİ
            manual_cash = _dec(request.POST.get('manual_cash', '0'))
            limit_channel = pos_mode if is_pos_flow else (request.POST.get('channel', 'CASH').upper())

            # ONARIM FAZI 2: processes=procs gecilerek yabanci fiat para birimli
            # urunlerin MASAK tabanindan kacmasi engellenir (fail-safe TL norm).
            limit_err = _check_legal_limits_retail(config, abs_total, is_output, limit_channel, is_pos_flow, pos_mode,
                                                   is_manual, manual_cash, customer, processes=procs)
            if limit_err: return JsonResponse({'error': True, 'error_msg': limit_err}, status=400)

            # 5. POS İŞLEME
            pavo_inquiry_data = {}
            pavo_paid_total = Decimal('0')
            pavo_terminal_serial = ''
            pavo_invoice_no = ''
            pavo_sale_number = ''

            if is_pos_flow and abs_total > 0:
                pavo_data, pavo_paid_total, pavo_inquiry_data, pavo_invoice_no, pavo_sale_number, pavo_terminal_serial, pos_reference = _handle_pavo_transaction(
                    raw_pavo, abs_total, pos_reference
                )
                # POS tutarı esas alınıyor mu? Genelde sepet tutarı değişmez ama burada paid_total için kullanacağız.

            # R-FAZ 3 NOTU: Kâr hesaplama bloku stok güncelleme döngüsünden
            # SONRAYA taşındı. Maliyet artık `StockSnapshot.weighted_avg_cost_eur`
            # üzerinden okunur (Products.buy_price_eur yerine) → güncel WAC ile
            # tutarlı.

            # 6. STOK VE EMANET
            ENTRY_SET = {'PURCHASE', 'STOCK_IN', 'RETURN', 'ORDER_IN'}
            net_hs = Decimal('0')

            for p in procs:
                mv = 'ENTRY' if p.transaction_type in ENTRY_SET else 'EXIT'
                prod = p.product
                phs = _dec(p.price_hs)
                net_hs += (phs if mv == 'ENTRY' else -phs)

                if prod and p.transaction_type == 'PURCHASE':
                    # FAZ 3: WAC hesaplaması artık StockService.record_entry() tarafından
                    # otomatik yapılıyor. Burada sadece Products tablosundaki
                    # global fiyat referanslarını güncelliyoruz (geriye dönük uyumluluk).
                    #
                    # UAT REGRESYON FIX (2026-04-29): Eski kod `prod.save(update_fields=...)`
                    # ile yazıyordu. Products.save() override'ı her durumda full_clean()
                    # tetikliyor → clean() Python instance'ın TÜM alanlarını (özellikle
                    # `gram`'ı) doğruluyor. Eğer `Products.gram` veritabanında negatif
                    # kalmışsa (legacy alan; "negatif kalsa bile patlamaz" prensibi
                    # uygulanan diğer akışların aksine) ValidationError fırlatıyor:
                    #     {'gram': "Gram negatif olamaz."}
                    # Çözüm: `Products.objects.filter(id=...).update(...)` ile atomic
                    # SQL UPDATE — instance dirty olmaz, full_clean() tetiklenmez.
                    # `update_scrap_pool_weighted_mileage` (scraps/views.py ONARIM
                    # FAZI 5 / ADIM A) ile tutarlı pattern.
                    is_gram = getattr(prod, 'is_gram_bullion', False)
                    new_qty = _dec(p.gram) if is_gram else _dec(p.piece)

                    if new_qty > Decimal('0'):
                        # StockSnapshot'tan güncel WAC bilgisini oku
                        _snap = StockSnapshot.objects.filter(product=prod, store=store).first()
                        if _snap:
                            # Products tablosundaki referans fiyatları WAC ile güncelle
                            _new_buy_hs = _snap.weighted_avg_cost_hs.quantize(Decimal('0.001'))
                            _new_buy_tl = _snap.weighted_avg_cost_eur.quantize(Decimal('0.01'))
                            Products.objects.filter(id=prod.id).update(
                                buy_price_hs=_new_buy_hs,
                                buy_price_eur=_new_buy_tl,
                            )
                            # Python instance senkronu (loop'un sonraki adımlarında
                            # tutarlı okuma için)
                            prod.buy_price_hs = _new_buy_hs
                            prod.buy_price_eur = _new_buy_tl

                if (custody_flag and not procs.filter(process_mileage='CUSTODY').exists()) or (
                        p.process_mileage == 'CUSTODY'):
                    book_custody_tx(
                        customer=customer, store=store, process_no=process_no,
                        direction=CustomerCustodyLedger.CUSTODY_IN,
                        product=prod, amount_hs=phs, quantity_piece=int(p.piece or 0), quantity_gram=_dec(p.gram),
                        user=request.user, desc=f"Perakende {p.transaction_type}"
                    )

                    # --- YENİ: SATIN ALINIP BIRAKILAN ÜRÜNÜ HAVUZA GERİ SOKMA ---
                    if mv == 'EXIT' and prod:
                        # DÜZELTME: Maliyeti satış fiyatından değil, anlık stok maliyetinden (WAC) alıyoruz.
                        _snap_for_custody = StockSnapshot.objects.filter(product=prod, store=store).first()
                        custody_wac_hs = _snap_for_custody.weighted_avg_cost_hs if _snap_for_custody else (
                                    prod.buy_price_hs or Decimal('0'))
                        custody_wac_tl = _snap_for_custody.weighted_avg_cost_eur if _snap_for_custody else (
                                    prod.buy_price_eur or Decimal('0'))

                        StockService.record_entry(
                            product=prod,
                            store=store,
                            quantity_gram=_dec(p.gram),
                            quantity_pieces=int(p.piece or 0),
                            reason=StockLedger.Reason.CUSTODY_IN,
                            ref_type='process_custody',
                            ref_id=str(p.id),
                            unit_cost_hs=custody_wac_hs,  # <--- Artık WAC değerini alıyor
                            unit_cost_eur=custody_wac_tl,  # <--- Artık WAC değerini alıyor
                            user=request.user,
                            notes="Satış yapıldı ancak müşteri emanete bıraktı."
                        )

                if prod:
                    is_scrap_product = bool(getattr(prod, 'is_scrap', False))
                    if _is_unique_single_product(prod) and not is_scrap_product:
                        # ── FAZ 9.6: Tekil barkodlu ürün — is_completed + StockService ──
                        # Satış (EXIT) → is_completed=True ("Satıldı")
                        # Alış (ENTRY) → is_completed=False ("Tezgahta")
                        prod.is_completed = (mv == 'EXIT')
                        prod.save(update_fields=['is_completed'])
                        # R-FAZ 5: process_id=p.id → StockLedger ref_id per-line
                        # olur, böylece cancel_stock_entry tek satırı reverse eder.
                        update_product_stock(prod, mv, p.piece, p.gram, p.waiting_stock,
                                             request.user, "Perakende", process_no,
                                             process_id=p.id)
                    elif is_scrap_product:
                        # R-FAZ 7: Hurda PURCHASE/SALE her iki yön de
                        # complete_process'te işlenir. Önceki sürüm "ALIS"ı
                        # add_scrap_to_process anında yazıyordu — Process
                        # IN_PROGRESS iken stok hareket ediyor + müşteri
                        # bilgisi henüz set olmamış oluyor (Tedarikçisiz
                        # görünüm bug'ı). Bilezik/toptan deseniyle simetri
                        # için her iki yön de buraya alındı.
                        if mv == 'EXIT':
                            update_product_stock(prod, mv, p.piece, p.gram, p.waiting_stock,
                                                 request.user, "Perakende", process_no,
                                                 process_id=p.id)
                            # R-FAZ 6: Hurda SATIŞ — legacy `Products.gram` alanı
                            # `StockSnapshot.stock_gram` ile senkron kalsın diye
                            # düşülür (Greatest 0 floor — negatif olamaz).
                            Products.objects.filter(id=prod.id).update(
                                gram=Greatest(F('gram') - _dec(p.gram), Decimal('0')),
                            )
                        else:  # mv == 'ENTRY' (PURCHASE — müşteriden alınan hurda)
                            # 1) Havuz WAC milyem güncellemesi (Process.process_mileage'dan).
                            try:
                                _hp_mileage = Decimal(int(_dec(p.process_mileage or 0)))
                            except (InvalidOperation, ValueError):
                                _hp_mileage = Decimal('0')
                            _hp_gram = _dec(p.gram)
                            if _hp_gram > 0 and _hp_mileage > 0:
                                try:
                                    update_scrap_pool_weighted_mileage(
                                        product=prod, store=store,
                                        new_gram=_hp_gram, new_mileage=_hp_mileage,
                                    )
                                except Exception as _hp_err:
                                    _log.warning(
                                        "Hurda havuz milyem WAC güncelleme hatası "
                                        "(process_id=%s): %s", p.id, _hp_err,
                                    )
                            # 2) StockService.record_entry — process_id=p.id ile per-line ref.
                            # FAZ 15 / WAC FIX: unit_cost_hs = milyem / 1000.
                            # Önceki sürüm unit_cost_hs geçirmiyordu → StockLedger.unit_cost_hs=0
                            # yazılıyordu. Kısmi iptalde recalculate_scrap_pool_mileage_after_cancel
                            # weighted_sum_hs=0 üretip Products.product_mileage'ı sıfırlıyordu.
                            _hp_unit_cost_hs = (
                                (_hp_mileage / Decimal('1000')).quantize(Decimal('0.001'))
                                if _hp_mileage > 0 else Decimal('0.000')
                            )
                            update_product_stock(prod, mv, p.piece, p.gram, p.waiting_stock,
                                                 request.user, "Perakende", process_no,
                                                 unit_cost_hs=_hp_unit_cost_hs,
                                                 process_id=p.id)
                            # 3) R-FAZ 6: legacy Products.gram artırma + buy_price_eur yenileme.
                            _hp_unit_buy_tl = _dec(getattr(p, 'unit_price', 0) or 0)
                            if _hp_unit_buy_tl > 0:
                                Products.objects.filter(id=prod.id).update(
                                    gram=Greatest(F('gram') + _hp_gram, Decimal('0')),
                                    buy_price_eur=_hp_unit_buy_tl,
                                )
                            else:
                                Products.objects.filter(id=prod.id).update(
                                    gram=Greatest(F('gram') + _hp_gram, Decimal('0')),
                                )
                    else:
                        # Genel dal — bilezik (is_gram_bullion) ve diğer non-scrap, non-unique ürünler.
                        # FAZ 15 / WAC FIX: ENTRY ise process_mileage'dan unit_cost_hs türet.
                        # Bilezik havuzunda kısmi iptalde milyem=0 hatasını önler.
                        _gen_unit_cost_hs = Decimal('0.000')
                        if mv == 'ENTRY':
                            try:
                                _gen_mileage = Decimal(int(_dec(p.process_mileage or 0)))
                            except (InvalidOperation, ValueError):
                                _gen_mileage = Decimal('0')
                            if _gen_mileage > 0:
                                _gen_unit_cost_hs = (
                                    _gen_mileage / Decimal('1000')
                                ).quantize(Decimal('0.001'))
                        update_product_stock(prod, mv, p.piece, p.gram, p.waiting_stock,
                                             request.user, "Perakende", process_no,
                                             unit_cost_hs=_gen_unit_cost_hs,
                                             process_id=p.id)

            # ─────────────────────────────────────────────────
            # R-FAZ 3/7 — POOL WAC MİLYEM (BİLEZİK ALIŞ TAMAMLANDIKTAN SONRA)
            # ─────────────────────────────────────────────────
            # R-FAZ 7 itibariyle hurda WAC milyemi de complete_process içinde
            # güncellenir, ama stok döngüsünün ENTRY dalında (yukarıda) inline
            # işlenir. Bu blok bilezik (gram_bullion + non-scrap) için kalır.
            for _bp in procs:
                if _bp.transaction_type != 'PURCHASE' or not _bp.product:
                    continue
                _bp_prod = _bp.product
                _is_scrap = bool(getattr(_bp_prod, 'is_scrap', False))
                if _is_scrap:
                    continue  # Hurda WAC zaten ana stok döngüsünde güncellendi
                _is_gram_bullion = bool(getattr(_bp_prod, 'is_gram_bullion', False))
                _bp_gram = _dec(_bp.gram)
                try:
                    _bp_mileage = Decimal(int(_dec(_bp.process_mileage or 0)))
                except (InvalidOperation, ValueError):
                    _bp_mileage = Decimal('0')
                if _is_gram_bullion and _bp_gram > 0 and _bp_mileage > 0:
                    try:
                        update_bracelet_pool_weighted_mileage(
                            product=_bp_prod, store=store,
                            new_gram=_bp_gram, new_mileage=_bp_mileage,
                        )
                    except Exception as _bp_err:
                        _log.warning(
                            "Bilezik havuz milyem WAC güncelleme hatası "
                            "(process_id=%s): %s", _bp.id, _bp_err,
                        )

            # ─────────────────────────────────────────────────
            # R-FAZ 3 — KAR HESAPLAMA (Stok güncellemeden SONRA, WAC üzerinden)
            # ─────────────────────────────────────────────────
            # Maliyet bazı `StockSnapshot.weighted_avg_cost_eur` (per gram veya
            # per piece). Önceki sürüm `Products.buy_price_eur` okuyordu — bu
            # son giriş fiyatıdır, ağırlıklı ortalama değildir; ardışık
            # alımlardan sonra kâr çarpıtılıyordu.
            for p in procs:
                if p.transaction_type == 'SALE' and p.product:
                    sale_amount_per_unit = _dec(p.unit_price) if getattr(p, 'unit_price', None) else _dec(p.amount)

                    # WAC TL: per-gram veya per-piece (StockSnapshot tasarımı).
                    _profit_snap = StockSnapshot.objects.filter(
                        product=p.product, store=store,
                    ).first()
                    if _profit_snap and _profit_snap.weighted_avg_cost_eur:
                        purchase_amount_per_unit = _dec(_profit_snap.weighted_avg_cost_eur)
                    else:
                        # Snapshot yoksa fallback: Products.buy_price_eur
                        purchase_amount_per_unit = _dec(getattr(p.product, 'buy_price_eur', 0))

                    qty = _dec(p.gram) if getattr(p.product, 'is_gram_bullion', False) else _dec(p.piece)

                    try:
                        _calculate_and_save_profit(
                            process=p,
                            operation_type='SALE',
                            purchase_amount=purchase_amount_per_unit,
                            sale_amount=sale_amount_per_unit,
                            qty=qty
                        )
                    except Exception as e:
                        _log.warning(f"Kâr hesaplama hatası (Process ID: {p.id}): {e}")

            # ─────────────────────────────────────────────────
            # FAZ 19: TAKAS vs DÖVİZ BOZMA AYRIMI
            # ─────────────────────────────────────────────────
            # Sepetteki döviz (is_currency=True) ALIŞ kalemlerini tespit et
            _currency_purchase_items = [
                p for p in procs
                if p.transaction_type == 'PURCHASE'
                and p.product
                and getattr(p.product, 'is_currency', False)
            ]

            # Sepette fiziksel ürün SATIŞI var mı? (altın, ziynet vb.)
            _has_physical_sale = any(
                p.transaction_type in ('SALE', 'ORDER_IN')
                and p.product
                and not getattr(p.product, 'is_currency', False)
                for p in procs
            )

            _needs_approval_fx = _get_approval_status(request.user)
            # FAZ 20.1: UI'dan seçilen kasa ID'leri
            _ui_try_id = (request.POST.get('bank_account_cash') or '').strip() or None
            _ui_fx_id  = (request.POST.get('bank_account_fx') or '').strip() or None

            for _cp in _currency_purchase_items:
                _cp_prod = _cp.product
                _cp_is_gram = getattr(_cp_prod, 'is_gram_bullion', False)
                _cp_qty = _dec(_cp.gram) if _cp_is_gram else _dec(_cp.piece)
                _cp_tl = _dec(_cp.amount)
                _cp_rate = _dec(_cp.unit_price) if _cp_qty > 0 else Decimal('0')

                if _has_physical_sale:
                    # ═══ TAKAS: Döviz + fiziksel ürün aynı sepette ═══
                    # Döviz kasasına TEK YÖNLÜ GİRİŞ (TRY ÇIKIŞ YOK)
                    # Çünkü karşılığında TL değil, altın verildi.
                    _process_barter_currency_entry(
                        process_no=process_no,
                        product=_cp_prod,
                        qty=_cp_qty,
                        unit_price=_cp_rate,
                        total_amount_eur=_cp_tl,
                        user=request.user,
                        needs_approval=_needs_approval_fx,
                    )
                else:
                    # ═══ SAF DÖVİZ BOZMA: Çift taraflı (Double-Entry) ═══
                    # Döviz kasasına GİRİŞ + TRY kasasından ÇIKIŞ
                    _process_currency_exchange(
                        process_no=process_no,
                        product=_cp_prod,
                        qty=_cp_qty,
                        unit_price=_cp_rate,
                        total_amount_eur=_cp_tl,
                        operation_type='PURCHASE',
                        user=request.user,
                        needs_approval=_needs_approval_fx,
                        try_bank_account_id=_ui_try_id,
                        fx_bank_account_id=_ui_fx_id,
                    )

            # Aynı mantık: Döviz SATIŞ kalemleri (müşteriye döviz verme)
            _currency_sale_items = [
                p for p in procs
                if p.transaction_type in ('SALE', 'ORDER_IN')
                and p.product
                and getattr(p.product, 'is_currency', False)
            ]

            _has_physical_purchase = any(
                p.transaction_type == 'PURCHASE'
                and p.product
                and not getattr(p.product, 'is_currency', False)
                for p in procs
            )

            for _cs in _currency_sale_items:
                _cs_prod = _cs.product
                _cs_is_gram = getattr(_cs_prod, 'is_gram_bullion', False)
                _cs_qty = _dec(_cs.gram) if _cs_is_gram else _dec(_cs.piece)
                _cs_tl = _dec(_cs.amount)
                _cs_rate = _dec(_cs.unit_price) if _cs_qty > 0 else Decimal('0')

                if _has_physical_purchase:
                    # TAKAS: Döviz kasasından TEK YÖNLÜ ÇIKIŞ
                    # (karşılığında TL değil, fiziksel ürün alındı)
                    # Faz 13: SSOT — get_currency_code_from_product tüm desteklenen
                    # para birimlerini kapsar (USD/EUR/GBP/CAD/QAR/SAR/CHF/AUD).
                    # Eskiden bu noktada hardcoded ['USD','EUR','GBP','CAD','QAR']
                    # listesi vardı; SAR/CHF/AUD takas çıkışı sessizce atlanıyordu.
                    _fx_cur = get_currency_code_from_product(_cs_prod)
                    if _fx_cur:
                        _fx_acct, _ = _resolve_or_create_cash_account(store, _fx_cur)
                        if _fx_acct:
                            Payment.objects.create(
                                process_no=process_no,
                                payment_type='CASH',
                                amount=_cs_tl,
                                currency_amount=_cs_qty,
                                exchange_rate=_cs_rate,
                                is_output=True,  # ÇIKIŞ
                                bank_account=_fx_acct,
                                reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
                                is_approved=not _needs_approval_fx,
                                reference=f'[{_fx_cur}] Takas — Döviz Çıkışı',
                            )
                else:
                    # SAF DÖVİZ BOZMA: Çift taraflı
                    _process_currency_exchange(
                        process_no=process_no,
                        product=_cs_prod,
                        qty=_cs_qty,
                        unit_price=_cs_rate,
                        total_amount_eur=_cs_tl,
                        operation_type='SALE',
                        user=request.user,
                        needs_approval=_needs_approval_fx,
                        try_bank_account_id=_ui_try_id,
                        fx_bank_account_id=_ui_fx_id,
                    )

            # ─────────────────────────────────────────────────
            # 7. ÖDEME KAYITLARI — Fark tutarı tahsilatı
            # FAZ 19: abs_total zaten calc_sales - calc_purchases farkıdır.
            # Döviz kalem girişleri yukarıda ayrıca işlendi.
            # Bu bölüm sadece FARK tutarını (Nakit/Kart/Havale) tahsil eder.
            # ─────────────────────────────────────────────────
            cash_amt = Decimal('0');
            card_amt = Decimal('0');
            transfer_amt = Decimal('0')

            if is_manual:
                cash_amt = manual_cash
                card_amt = _dec(request.POST.get('manual_card', '0'))
                transfer_amt = _dec(request.POST.get('manual_transfer', '0'))
            elif is_pos_flow:
                if pos_mode == 'CASH':
                    cash_amt = pavo_paid_total
                elif pos_mode == 'TRANSFER':
                    transfer_amt = pavo_paid_total
                else:
                    card_amt = pavo_paid_total
            elif cash_status is not None:
                channel = request.POST.get('channel', 'cash').lower()
                if channel == 'cash':
                    cash_amt = abs_total
                elif channel == 'transfer':
                    transfer_amt = abs_total
                else:
                    card_amt = abs_total

            paid_total = cash_amt + card_amt + transfer_amt

            # Faz 2 + Faz 5: Banka hesabı ID'lerini request'ten oku ve doğrula
            ba_cash_id = (request.POST.get('bank_account_cash') or '').strip() or None
            ba_card_id = (request.POST.get('bank_account_card') or '').strip() or None
            ba_transfer_id = (request.POST.get('bank_account_transfer') or '').strip() or None
            ba_cash = None
            ba_card = None
            ba_transfer = None

            # Faz 4: Komisyon verilerini oku
            _installment_count = int(request.POST.get('installment_count') or 1)
            _commission_rate = request.POST.get('commission_rate', '').strip()
            _commission_amount = request.POST.get('commission_amount', '').strip()
            _net_amount = request.POST.get('net_amount', '').strip()
            _maturity_date_str = request.POST.get('maturity_date', '').strip()

            if cash_amt > 0 and store:
                ba_cash = PaymentBankAccountValidator.validate(
                    payment_type='CASH',
                    bank_account_id=ba_cash_id,
                    store=store,
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

            # FAZ 18: Onaylı Kasa durumunu kontrol et
            _needs_approval = _get_approval_status(request.user)
            _is_approved = not _needs_approval

            # FAZ 17+18: Ödeme kayıtları (döviz kuru + onay desteği)
            if cash_amt > 0:
                _cash_fx = _build_currency_extra(ba_cash, cash_amt)
                Payment.objects.create(
                    process_no=process_no,
                    payment_type='CASH',
                    amount=cash_amt,
                    is_output=is_output,
                    date=timezone.now(),
                    bank_account=ba_cash,
                    reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
                    is_approved=_is_approved,
                    **_cash_fx,
                )
                # FAZ 16: Para birimi ürünlerine (is_currency=True) stok hareketi oluşturulmaz.
                try_prod = Products.objects.filter(name__icontains="TRY - Türk Lirası").first()
                if try_prod and not try_prod.is_currency:
                    update_product_stock(try_prod, ('EXIT' if is_output else 'ENTRY'), cash_amt, 0, 0, request.user,
                                         "Perakende Nakit", process_no)
            if card_amt > 0:
                _cc_extra = {}
                if _installment_count > 1:
                    _cc_extra['installment'] = _installment_count

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

                _card_fx = _build_currency_extra(ba_card, card_amt)
                _cc_extra.update(_card_fx)

                Payment.objects.create(
                    process_no=process_no,
                    payment_type='CREDIT_CARD',
                    amount=card_amt,
                    is_output=is_output,
                    date=timezone.now(),
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
                    date=timezone.now(),
                    bank_account=ba_transfer,
                    reconciliation_status=Payment.ReconciliationStatus.PENDING,
                    is_approved=_is_approved,
                    **_transfer_fx,
                )

            # 8. PAVO LOG
            if is_pos_flow and pavo_inquiry_data:
                try:
                    terminal = PavoTerminal.objects.filter(store=store,
                                                           serial_number=pavo_terminal_serial).first() if pavo_terminal_serial else None
                    PavoLocalSale.objects.create(
                        terminal=terminal, invoice=None,  # Fatura aşağıda oluşacak
                        request_payload={}, response_payload=pavo_inquiry_data,
                        status='SUCCESS', amount=paid_total, currency='TRY'
                    )
                except:
                    pass

            # 9. NETLEŞTİRME (Bakiye)
            # FAZ 18.6: POS komisyonu müşterinin cari bakiyesini ETKİLEMEMELİDİR.
            # paid_total komisyon dahil tutardır. Komisyon tutarını çıkararak
            # "net ödenen" ile karşılaştırıyoruz.
            if customer:
                _total_commission_retail = Decimal('0')
                _comm_val_r = request.POST.get('commission_amount', '').strip()
                if _comm_val_r:
                    try:
                        _total_commission_retail = Decimal(str(_comm_val_r).replace(',', '.'))
                    except Exception:
                        _total_commission_retail = Decimal('0')

                _paid_net_of_commission = paid_total - _total_commission_retail
                balance_diff = abs_total - _paid_net_of_commission
                if abs(balance_diff) > Decimal('0.01'):
                    # ──────────────────────────────────────────────────────
                    # FAZ 33.2 — SATIŞ KURU İLE BORÇ YAZIMI (2026-05-01)
                    # ──────────────────────────────────────────────────────
                    # FAZ 33 (deneme) borç yazımını `get_current_has_rate()`
                    # (ALIŞ kuru) + LedgerService.write_debt sarmalamasıyla
                    # yapıyordu. Saha geri bildirimi: 95.000 TL satışta
                    # cari ekran 14.048 HS, fiş 13.993 HS gösteriyordu —
                    # spread farkı kullanıcıyı yanıltıyordu.
                    #
                    # Doğru mimari: Müşteriye satış SATIŞ kuruyla yapılırsa
                    # (Process.price_hs = total_tl / sale_rate), cari borç
                    # da AYNI kurla yazılmalı. Böylece:
                    #   • Fiş HS  = Cari borç HS  = 13.993  (tek değer)
                    #   • amount_eur = balance_diff = 95.000 (TL korunur)
                    #   • exchange_rate_eur = sale_rate (ledger kendi
                    #     kurunu saklar, tahsilatta görüntü için kullanılır)
                    #
                    # LedgerService.write_debt iç olarak get_current_has_rate
                    # (ALIŞ) hardcoded çağırdığı için bypass edilir;
                    # CustomerLedger.objects.create direct kullanılır.
                    # Audit alanları (created_by, ip_address, user_agent)
                    # request.META'dan toplanır → manipülasyon koruması
                    # ve denetim izi LedgerService kullanımıyla aynı.
                    # ──────────────────────────────────────────────────────
                    new_debt_hs = Decimal('0')
                    new_credit_hs = Decimal('0')

                    # FAZ 33.2: SATIŞ kurunu Products tablosundan oku.
                    # PriceService önce denenir (Redis + StorePriceCache);
                    # boş dönerse Products('Has Altın') kaydı fallback
                    # olarak kullanılır. Bu sıralama add_to_cart_retail
                    # akışıyla simetriktir → cart-add ile complete-process
                    # arası kur tutarlılığı korunur.
                    sale_rate = Decimal('0')
                    try:
                        hs_data = PriceService.get_price('GOLD_24K')
                        if hs_data:
                            _sell_tl = hs_data.get('sell_tl') or 0
                            if _sell_tl and Decimal(_sell_tl) > 0:
                                sale_rate = Decimal(_sell_tl)
                    except Exception:
                        sale_rate = Decimal('0')
                    if sale_rate <= 0:
                        _hs_prod = (
                            Products.objects
                            .filter(name__icontains='Has Altın')
                            .only('sale_price_eur')
                            .first()
                        )
                        if _hs_prod:
                            sale_rate = _dec(getattr(_hs_prod, 'sale_price_eur', 0), '0.01')

                    # Almanya/juwelier_project fallback: Kitco EUR/gram (ask)
                    if sale_rate <= 0:
                        _kitco_buy, _kitco_sale = _get_kitco_eur_per_gram()
                        if _kitco_sale > 0:
                            sale_rate = _kitco_sale

                    if sale_rate <= 0:
                        # Kur okunamadı; netleştirme kaydı atlanır
                        # (eski FAZ 33 davranışıyla aynı fail-safe).
                        log.error(
                            f"complete_process: SATIŞ kuru okunamadı, "
                            f"netleştirme kaydı atlandı. process_no="
                            f"{procs[0].process_no if procs else '?'}"
                        )
                    else:
                        if not is_output:  # SATIŞ
                            if balance_diff > 0:
                                new_debt_hs = _dec(balance_diff / sale_rate, '0.001')
                            elif balance_diff < 0:
                                new_credit_hs = _dec(abs(balance_diff) / sale_rate, '0.001')
                        else:  # ALIŞ
                            if balance_diff > 0:
                                new_credit_hs = _dec(balance_diff / sale_rate, '0.001')
                            elif balance_diff < 0:
                                new_debt_hs = _dec(abs(balance_diff) / sale_rate, '0.001')

                        _grp_process_no = (procs[0].process_no if procs else '') or ''
                        _direction_label = 'PURCHASE' if is_output else 'SALE'

                        # Audit context — manipülasyon koruması için
                        # CustomerLedger.created_by/ip_address/user_agent
                        # alanları request.META'dan toplanır.
                        _ip_addr = (
                            request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                            or request.META.get('REMOTE_ADDR') or None
                        )
                        _user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:255]

                        if new_debt_hs > 0:
                            # FAZ 38 — TL "round-trip" yuvarlama hatası giderildi.
                            # Eski: _debt_eur = new_debt_hs * sale_rate (HS quantize
                            # sonrası ters çarpım → satıştaki kesin TL'yi
                            # ezerek küsurat sapması yaratıyordu, ör. 11.383
                            # TL → HS quantize → 11.385,49 TL).
                            # Yeni: balance_diff (gerçek TL) doğrudan saklanır.
                            _debt_eur = abs(balance_diff).quantize(Decimal('0.01'))
                            CustomerLedger.objects.create(
                                customer=customer,
                                store=store,
                                transaction_type=CustomerLedger.DEBT,
                                amount_hs=new_debt_hs,
                                amount_eur=_debt_eur,
                                exchange_rate_eur=sale_rate,
                                currency=CustomerLedger.CURRENCY_HS,
                                process_no=_grp_process_no,
                                description=(
                                    f"Perakende {_direction_label} ödeme farkı "
                                    f"(müşteri borçlandı) - {_grp_process_no}"
                                ),
                                requires_approval=False,
                                is_approved=True,
                                created_by=request.user,
                                ip_address=_ip_addr,
                                user_agent=_user_agent,
                            )
                        if new_credit_hs > 0:
                            # FAZ 38 — Aynı round-trip yuvarlama düzeltmesi.
                            _credit_eur = abs(balance_diff).quantize(Decimal('0.01'))
                            CustomerLedger.objects.create(
                                customer=customer,
                                store=store,
                                transaction_type=CustomerLedger.CREDIT,
                                amount_hs=new_credit_hs,
                                amount_eur=_credit_eur,
                                exchange_rate_eur=sale_rate,
                                currency=CustomerLedger.CURRENCY_HS,
                                process_no=_grp_process_no,
                                description=(
                                    f"Perakende {_direction_label} ödeme farkı "
                                    f"(mağaza borçlandı) - {_grp_process_no}"
                                ),
                                requires_approval=False,
                                is_approved=True,
                                created_by=request.user,
                                ip_address=_ip_addr,
                                user_agent=_user_agent,
                            )

            # --- 10. FATURA OLUŞTURMA (YENİ ENTEGRASYON) ---
            generated_invoice = None
            if customer:
                # Pavo (POS) bilgilerini paketle
                pavo_meta = {
                    'invoice_no': pavo_invoice_no,
                    'inquiry_data': pavo_inquiry_data,
                    'sale_number': pavo_sale_number,
                    'terminal_serial': pavo_terminal_serial
                }

                # YENİ FONKSİYONU ÇAĞIRIYORUZ
                # Bu fonksiyon hem satış faturasını hem de gerekirse gider pusulasını oluşturur.
                generated_invoice = create_retail_bulk_invoice(
                    store=store,
                    customer=customer,
                    processes=procs,  # Kilitlenmiş güncel process listesi
                    is_pos_flow=is_pos_flow,  # POS kullanıldı mı?
                    pavo_data=pavo_meta,  # POS'tan dönen fatura no vb.
                    payment_total=paid_total  # Kasaya giren toplam para (Nakit + Kart + Havale)
                )

                # invoices:detail kaldırıldı — invoices app Juwelier Plus'ta yok
                # generated_invoice her zaman None döner (stub), URL kaydı atlanır
                if generated_invoice:
                    pass
            # 11. BİTİR VE BİLDİRİM
            procs.update(is_status='COMPLETED', date=timezone.now())

            if customer:
                try:
                    mail_items = [{"product_name": (p.product.name if p.product else "-"),
                                   "amount_eur": fmt_tl(abs(_dec(p.amount)))} for p in procs]
                    payments_ctx = {
                        "cash": float(cash_amt), "transfer": float(transfer_amt), "credit_card": float(card_amt),
                        "paid_total_tl": float(paid_total), "has_any": bool(paid_total > 0),
                        "direction_text": "İşlem Tamamlandı", "installment": installment
                    }
                    totals_ctx = {"net_tl_abs": fmt_tl(abs_total), "net_hs": f"{(net_hs or 0):.3f}"}

                    trigger_transaction_notifications(
                        request=request, process_no=process_no, customer=customer,
                        items=mail_items, payments=payments_ctx, totals=totals_ctx
                    )
                except:
                    pass

            resp = {'result': True, 'process_no': process_no, 'message': 'İşlem başarıyla tamamlandı.'}
            if generated_invoice:
                resp['invoice_id'] = str(generated_invoice.id)
                resp['invoice_no'] = generated_invoice.invoice_no

            return JsonResponse(resp)

    except Customers.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Müşteri bulunamadı.'}, status=404)
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
        _log.exception("Hata")
        return JsonResponse({'error': True, 'error_msg': str(e)}, status=500)
