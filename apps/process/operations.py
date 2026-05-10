# file: apps/process/views/operations.py

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.db.models import Q

# Modeller
from apps.stores.models import Stores
from apps.products.models import Products
from apps.process.models import Process, Payment
from apps.suppliers.models import Suppliers, SupplierLedger
from apps.customers.models import Customers, CustomerLedger
from apps.customers.services.ledger import LedgerService
from apps.customers.services.exceptions import InvalidLedgerStateError
from apps.customers.services.audit import extract_audit_context
from apps.gold_purchases.models import GoldPurchases
# --- FAZ 3: StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.custody.models import CustomerCustodyLedger

# R-FAZ 5: Evrensel cancel utility ve havuz yeniden hesaplayıcılar.
from apps.stock_management.services.cancel_service import cancel_stock_entry
from apps.scraps.views import recalculate_scrap_pool_mileage_after_cancel
from apps.bracelets.views import recalculate_bracelet_pool_mileage_after_cancel
from django.db.models import F as _F
from django.db.models.functions import Greatest as _Greatest

import logging as _logging
logger = _logging.getLogger(__name__)


class StockReversalRequired(Exception):
    """
    FAZ 52 — Cancel akışında stok reversal başarısız olduğunda raise edilir.
    cancel_row/cancel_group bunu yakalayıp transaction'ı rollback eder ve
    kullanıcıya açık hata döner. Eski sessiz fail davranışını ortadan kaldırır.
    """
    pass


# ==========================================
# 1. YARDIMCI FONKSİYONLAR (Helpers)
# ==========================================

def _q2(x: Any) -> Decimal:
    """Para birimleri için 2 haneli hassasiyet."""
    return Decimal(str(x or "0")).quantize(Decimal("0.01"))


def _q3(x: Any) -> Decimal:
    """Has ve Gram altın için 3 haneli hassasiyet."""
    return Decimal(str(x or "0")).quantize(Decimal("0.001"))


def _current_store(request) -> Stores | None:
    """Kullanıcının aktif mağazasını bulur."""
    store = getattr(request.user, "store", None)
    if store:
        return store
    sid = getattr(request.user, "store_id", None) or request.session.get("active_store_id")
    if sid:
        try:
            return Stores.objects.get(id=sid)
        except Stores.DoesNotExist:
            pass
    try:
        return Stores.objects.first()
    except Exception:
        return None


def _parse_meta(desc: str) -> Dict[str, Any]:
    """
    Veritabanındaki 'description' alanındaki JSON verisini ayrıştırır.
    OB (Açıktan Bağlama) ve CONV (Çeviri) işlemleri için gereklidir.
    """
    if not desc:
        return {}
    s = str(desc)
    # OB formatı: "OB|{json...}"
    if s.startswith("OB|"):
        try:
            return {"kind": "OB", **json.loads(s[3:])}
        except Exception:
            return {"kind": "OB"}
    # CONV formatı: "CONV|{json...}"
    if s.startswith("CONV|"):
        try:
            return {"kind": "CONV", **json.loads(s[5:])}
        except Exception:
            return {"kind": "CONV"}
    return {}


def _fmt_tr(val: Any, currency: str = "") -> str:
    """Sayıları Türkçe formata (binlik nokta, ondalık virgül) çevirir ve gereksiz sıfırları atar."""
    if val is None: return "0"
    try:
        d_val = Decimal(str(val))
    except (InvalidOperation, ValueError):
        return "0"

    c = str(currency).upper()
    # Has ve Gram için 3, diğerleri için 2 küsurat baz alalım
    if c in ["HS", "GR"]:
        s = f"{d_val:,.3f}"
    else:
        s = f"{d_val:,.2f}"

    # Virgül ve nokta yer değiştirme (1,234.56 -> 1.234,56)
    s = s.replace(',', 'X').replace('.', ',').replace('X', '.')

    # Ondalık kısımdaki gereksiz sıfırları ve tek kalan virgülü at
    if ',' in s:
        s = s.rstrip('0').rstrip(',')

    return s


# ==========================================
# 2. STOK TERS İŞLEM MANTIĞI (Inventory Revert)
# ==========================================

def _revert_process_stock(p: Process, user) -> dict:
    """
    BİR İŞLEM İPTAL EDİLDİĞİNDE STOKLARI DÜZELTİR.

    R-FAZ 5 — Yeniden Mimarileştirildi:
        Önceki sürüm StockService.record_entry/record_exit ile YENİ bir
        karşı-hareket yazıyor; ref_type 'cancel_sale' / 'cancel_purchase'
        — `_cancel` suffix'i ile bitmediği için
        `recalculate_scrap_pool_mileage_after_cancel` orijinal-reversal
        eşlemesini kuramıyor (havuz milyemi geri sarılmıyor) ve cari
        (SupplierLedger) tarafı dokunulmuyor.

        Yeni sürüm: `cancel_stock_entry(ref_type, ref_id)` ile orijinal
        kayda 1:1 reversal yazılır; ref_type her zaman `<orig>_cancel`
        olur ve `recalculate_*_pool_mileage_after_cancel` doğru çalışır.

    FAZ 52 — Sessiz Fail Eliminasyonu (A-1):
        Eski davranış: exception → return None; eşleşme yok → return None.
        cancel_row dönüş değerini kontrol etmiyordu, SupplierLedger reversal
        koşulsuz çalışıyordu → cari geri sarılıyor stok stale kalıyordu.

        Yeni davranış: dict döner; cancel_row başarısızlıkta
        StockReversalRequired raise edip transaction'ı rollback eder.

    Hangi (ref_type, ref_id) çiftiyle aranır?
        - HURDA PURCHASE (perakende, R-Faz 7 sonrası): ref_type='process',
          ref_id=str(p.id) — complete_process update_product_stock ile yazıyor.
        - HURDA PURCHASE (legacy, R-Faz 7 öncesi): ref_type='scrap_add',
          ref_id=str(p.id) — add_scrap_to_process anında yazılmıştı.
        - HURDA SALE & diğer perakende SATIŞ/ALIŞ: ref_type='process',
          ref_id=str(p.id) (R-Faz 5'te update_product_stock per-line yazıyor)
        - BİLEZİK PURCHASE (perakende havuz): ref_type='bracelet_add',
          ref_id=str(p.id) — apps/bracelets/views.py:bracelet_add() satır
          553/662/770 üzerinden StockService.record_entry ile yazıyor.
          FAZ 25 öncesi bu fallback yoktu → İşlemler ekranından bilezik
          iptali sessiz fail veriyordu.
        - BİLEZİK SALE & diğer perakende: ref_type='process' + str(p.id).
        - waiting_stock=True olan satırlar stoğa girmediği için sadece
          incoming_stock alanına dokunulduysa cancel_stock_entry boş geçer.

    Returns:
        dict {
          'success': bool — True = stok güvenli durumda (geri sarıldı veya
                            geri sarılması gerekmeyen kayıt; ör. kasa kalemi,
                            taslak, waiting_stock).
          'reason': str — 'reverted' | 'no_product' | 'waiting_stock' |
                          'no_match_draft' | 'no_match_completed' | 'exception'
          'cancelled_count': int — geri sarılan StockLedger satır sayısı.
          'error': str | None — exception mesajı.
          'expected': bool — bu Process için stok eşleşmesi BEKLENİYOR muydu
                              (COMPLETED ise True, IN_PROGRESS ise False).
        }
    """
    result = {
        'success': True,
        'reason': '',
        'cancelled_count': 0,
        'error': None,
        'expected': False,
    }

    if not p.product or not p.store:
        # Kasa kalemi (bank_account_id is not None, product=None) veya
        # eksik kayıt — stok hareketi yoktu, reversal'a gerek yok.
        result['reason'] = 'no_product'
        return result

    if p.waiting_stock:
        # Bekleyen sipariş — gerçek stok hareketi yoktu, reversal'a gerek yok.
        result['reason'] = 'waiting_stock'
        return result

    # FAZ 52 (A-1): Stok eşleşmesi BEKLENİR mi? COMPLETED işlemler için
    # eşleşme yokluğu HATA; IN_PROGRESS taslak iptali için eşleşme yokluğu
    # OLAĞAN. expected=True caller (cancel_row) tarafından sertleştirme
    # kararına temel oluşturur.
    _was_completed = (p.is_status or '').upper() == 'COMPLETED'
    result['expected'] = _was_completed

    tx_type = (p.transaction_type or "").upper()
    is_scrap_product = bool(getattr(p.product, 'is_scrap', False))

    # FAZ 25 — Bilezik tespiti (WAC bloğundaki ile aynı mantık).
    # apps/bracelets/views.py:bracelet_add() StockLedger'a `ref_type='bracelet_add'`
    # ile yazıyor (satır 553, 662, 770). `is_scrap_product=False` olduğundan
    # önceki sürüm yalnızca `('process',)` deniyordu → eşleşme bulunamıyor →
    # `cancel_stock_entry` boş dönüyor → reversal yazılmıyor → stok geri sarılmıyor.
    _cat_name_detect = ''
    try:
        if p.product.category:
            _cat_name_detect = (p.product.category.name or '').lower()
    except Exception:
        pass
    is_bracelet_product = (
        'bilezik' in _cat_name_detect
        or bool(getattr(p.product, 'is_gram_bullion', False))
    )

    # R-FAZ 7 / FAZ 25 — ref_type fallback:
    # Yeni (post-R-Faz 7) tüm satırlar 'process' ile yazılır. Geriye dönük
    # uyum için hurda PURCHASE'da 'scrap_add', bilezik PURCHASE'da
    # 'bracelet_add' fallback'i denenir.
    if is_scrap_product and tx_type == 'PURCHASE':
        _ref_type_candidates = ('process', 'scrap_add')
    elif is_bracelet_product and tx_type == 'PURCHASE':
        _ref_type_candidates = ('process', 'bracelet_add')
    else:
        _ref_type_candidates = ('process',)

    # ----------------------------------------------------------------
    # FAZ 18 — REF_ID FALLBACK (Toptan-Hurda Köprüsü):
    # add_scrap_to_wholesale_process StockLedger'a `ref_id=sp_process_no`
    # ile yazıyor (apps/scraps/views.py satır 1118-1131, 1161-1174); bu
    # ID `Process.process_no` ile aynı, fakat `Process.id` (UUID) ile
    # FARKLI. Önceki sürümde `_revert_process_stock` yalnızca
    # `ref_id=str(p.id)` (UUID) arıyordu → toptan hurda iptalinde
    # sessiz fail: SupplierLedger pasifleşiyor (operations.py satır 693
    # üzerinden direkt update) ama StockLedger reversal yazılmıyor →
    # stok geri alınmıyor.
    #
    # Çözüm: ref_id adayları listesi. `str(p.id)` önce denenir
    # (retail + R-Faz 7 sonrası tüm yeni kayıtlar için); eşleşme
    # bulunamazsa `p.process_no` ile tekrar denenir (toptan hurda
    # `'scrap_add'` kayıtları için). Çift-iptal riski yoktur:
    # cancel_stock_entry yalnızca ilgili (ref_type, ref_id) çiftine ait
    # ledger satırlarını bulur ve aynı satır iki farklı ref_id altında
    # yer alamaz.
    # ----------------------------------------------------------------
    _ref_id_candidates = [str(p.id)]
    if getattr(p, 'process_no', None) and p.process_no != str(p.id):
        _ref_id_candidates.append(p.process_no)

    _cancel_hit = False
    _last_error = None  # FAZ 52: son exception (sonraki kombinasyonu denemeden önce kaydet)

    for _ref_type in _ref_type_candidates:
        if _cancel_hit:
            break
        for _ref_id in _ref_id_candidates:
            try:
                _result = cancel_stock_entry(
                    ref_type=_ref_type,
                    ref_id=_ref_id,
                    user=user,
                    reverse_supplier_ledger=False,
                    notes=f"Process İptali - {p.process_no or p.id}",
                    raise_if_not_found=False,
                )
            except Exception as _err:
                # FAZ 52 (A-1): Eskiden burada `return` yapıyordu — sessiz fail.
                # Şimdi exception'ı kaydedip sonraki kombinasyonu deniyoruz; tüm
                # adaylar tükenirse caller'a (cancel_row) raporlanacak.
                logger.error(
                    "_revert_process_stock cancel_stock_entry failed "
                    "(process_id=%s, ref_type=%s, ref_id=%s): %s",
                    p.id, _ref_type, _ref_id, _err,
                )
                _last_error = str(_err)
                continue
            if isinstance(_result, dict) and _result.get('cancelled_stock_count', 0) > 0:
                _cancel_hit = True
                result['cancelled_count'] = int(_result.get('cancelled_stock_count', 0))
                break

    if _cancel_hit:
        result['reason'] = 'reverted'
    elif _last_error:
        # FAZ 52 (A-1): Tüm aday kombinasyonları exception verdi → fail.
        result['success'] = False
        result['reason'] = 'exception'
        result['error'] = _last_error
        logger.error(
            "_revert_process_stock: TÜM ref kombinasyonları exception verdi "
            "(process_id=%s, process_no=%s, son_hata=%s)",
            p.id, p.process_no, _last_error,
        )
    elif _was_completed:
        # FAZ 52 (A-1): COMPLETED process bekleniyor ama eşleşme yok →
        # ya stok hiç yazılmadı (data corruption) ya da daha önce iptal
        # edildi (idempotent çift iptal). Caller'a fail raporlanır;
        # cancel_row idempotency için process zaten CANCELED ise bu
        # noktaya gelinmediği için (`_can_cancel_process` filtresi)
        # data corruption sayılmalı.
        result['success'] = False
        result['reason'] = 'no_match_completed'
        logger.error(
            "_revert_process_stock: COMPLETED process için ledger eşleşmesi YOK "
            "(process_id=%s, process_no=%s, tx=%s, is_scrap=%s) — beklenmeyen "
            "veri durumu, cancel_row rollback edecek.",
            p.id, p.process_no, tx_type, is_scrap_product,
        )
    else:
        # IN_PROGRESS taslak iptali — stok hareketi hiç yazılmamıştı.
        result['reason'] = 'no_match_draft'
        logger.info(
            "_revert_process_stock: ledger eşleşmesi yok (process_id=%s, "
            "process_no=%s, tx=%s, is_scrap=%s) — taslak iptali veya "
            "tekrar-iptal olabilir.",
            p.id, p.process_no, tx_type, is_scrap_product,
        )

    # ----------------------------------------------------------------
    # Havuz milyem WAC geri sarma — yalnızca PURCHASE iptallerinde
    # anlamlıdır (SALE iptali stok geri ekler ama WAC'ı etkilemez).
    # Yine de idempotent olduğu için her durumda çağrılabilir.
    # ----------------------------------------------------------------
    try:
        if is_scrap_product:
            recalculate_scrap_pool_mileage_after_cancel(p.product, p.store)
        else:
            cat_name = ((getattr(p.product, 'category', None) and p.product.category.name) or '').lower()
            if 'bilezik' in cat_name or getattr(p.product, 'is_gram_bullion', False):
                recalculate_bracelet_pool_mileage_after_cancel(p.product, p.store)
    except Exception as _recalc_err:
        logger.error(
            "_revert_process_stock recalc failed (product_id=%s, process_id=%s): %s",
            getattr(p.product, 'id', None), p.id, _recalc_err,
        )

    # ----------------------------------------------------------------
    # R-FAZ 6: Legacy `Products.gram` alanını ledger ile senkronla.
    # cancel_stock_entry yalnızca StockSnapshot ve StockLedger'ı tutar;
    # Products.gram ayrı bir ölü-veri alanı. PURCHASE iptalinde gram
    # düşmeli, SALE iptalinde gram artmalı (negatife düşmeyecek floor ile).
    # ----------------------------------------------------------------
    try:
        _gram_dec = Decimal(str(p.gram or 0))
        if _gram_dec > 0:
            if tx_type == 'PURCHASE':
                Products.objects.filter(id=p.product.id).update(
                    gram=_Greatest(_F('gram') - _gram_dec, Decimal('0')),
                )
            elif tx_type == 'SALE':
                Products.objects.filter(id=p.product.id).update(
                    gram=_F('gram') + _gram_dec,
                )
    except Exception as _gram_err:
        logger.error(
            "_revert_process_stock Products.gram sync failed (product_id=%s, process_id=%s): %s",
            getattr(p.product, 'id', None), p.id, _gram_err,
        )

    # ────────────────────────────────────────────────────────────────
    # FAZ 42 — Barkodlu Tekil Ürün `is_completed` Reset.
    # ────────────────────────────────────────────────────────────────
    # retail_views.py:1399-1404 satışta `Products.is_completed = True`
    # yapıyor (mv == 'EXIT' için tekil barkodlu ürünler). Eski iptal
    # akışında bu flag resetlenmediği için "Barkodlu Ürünler" listesi
    # (gold_purchases/views.py:973: `sold_q = Q(is_status=False) |
    # Q(product__is_completed=True)`) iptal edilen ürünü hâlâ "Satıldı"
    # gösteriyordu — kasa & cari geri alındığı halde barkodlu ürün
    # zombiye dönüyordu.
    #
    # Çözüm: SALE/ORDER_IN iptalinde idempotent olarak False'a çek.
    # PURCHASE iptalinde zaten False olduğundan dokunmuyoruz.
    # ────────────────────────────────────────────────────────────────
    try:
        if tx_type in ('SALE', 'ORDER_IN'):
            Products.objects.filter(
                id=p.product.id,
                is_completed=True,
            ).update(is_completed=False)
    except Exception as _ic_err:
        logger.error(
            "_revert_process_stock is_completed reset failed "
            "(product_id=%s, process_id=%s): %s",
            getattr(p.product, 'id', None), p.id, _ic_err,
        )

    # FAZ 52 (A-1): Caller (cancel_row) sonucu kontrol etsin diye dict döner.
    # WAC/gram/is_completed sync hataları success bayrağını DEĞİŞTİRMEZ —
    # bunlar idempotent yan etkiler; ana stok reversal başarısı zaten
    # yukarıdaki bloklarda set edildi.
    return result


def _mark_process_canceled(p: Process) -> None:
    """Process tablosundaki kaydı 'İPTAL' olarak işaretler."""
    if hasattr(p, "is_status"):
        p.is_status = "CANCELED"
    if hasattr(p, "is_deleted"):
        p.is_deleted = True
    p.save(update_fields=["is_status", "is_deleted"])


def _reverse_cashbox_for_process(*, process_no: str, audit: dict) -> int:
    """
    FAZ 52 (S-03) — Kasa İptali Karşı Girişi.

    Verilen process_no'ya bağlı tüm aktif (parent=None, REVERSAL DEĞİL)
    CashboxLedger satırlarını bulur ve her biri için bir REVERSAL satırı
    yazar. Bakiye snapshot orijinalin yönüne göre hesaplanır:
      - Original is_inflow=True (INCOME/TRANSFER_IN/DAILY_OPEN) →
        REVERSAL outflow olarak sayıldığı için bakiye DÜŞER.
      - Original is_outflow (EXPENSE/TRANSFER_OUT) → REVERSAL outflow
        kategorisinde ama parent ile eşleşip net etkisi (out - out =
        out farkı oluşturmaz çünkü parent zaten outflow). Açıklama:
        get_balance() outflow_types arasında REVERSAL'ı içerir →
        REVERSAL eklenirse bakiye DÜŞER. Bu sebeple original EXPENSE
        bir kayıt için REVERSAL yazmak bakiyeyi 2× düşürür → YANLIŞ.

    Bu nedenle yalnızca INCOME yönlü (kasa girişi) orijinaller için
    REVERSAL yazıyoruz; EXPENSE yönlü orijinaller için ayrı bir
    pozitif düzeltme satırı (henüz desteklenmiyor) gerekir. Toptan
    kasa kalemi PURCHASE = ENTRY = INCOME olduğu için raporlanan
    hata bu yolla düzelir; SALE = EXPENSE durumu kapsam dışı —
    cancel_row PROC dalına özel uyarı log'u ile bilgilendirilir.

    Idempotency: Aynı parent için ikinci REVERSAL yazılmaz (parent FK
    üzerinden kontrol). reversed_count döner.
    """
    if not process_no:
        return 0

    try:
        from apps.banking.models import CashboxLedger
    except Exception:
        return 0

    _audit = audit or {}
    _reversed_count = 0

    # Sadece original satırlar (parent=None) ve REVERSAL olmayanlar.
    _originals = CashboxLedger.objects.filter(
        process_no=process_no,
        parent__isnull=True,
    ).exclude(
        movement_type=CashboxLedger.MovementType.REVERSAL,
    )

    for _orig in _originals:
        # Idempotent: bu original için zaten REVERSAL yazıldı mı?
        if CashboxLedger.objects.filter(
            parent=_orig,
            movement_type=CashboxLedger.MovementType.REVERSAL,
        ).exists():
            continue

        # Yön kontrolü: yalnızca is_inflow (INCOME) için REVERSAL yaz.
        # EXPENSE yönlü originaller (SALE → kasadan çıkış) için
        # REVERSAL eklemek bakiyeyi yanlış yönde değiştirir → atla
        # ve audit logu yaz.
        if not _orig.is_inflow:
            logger.warning(
                "_reverse_cashbox_for_process: outflow-original atlandı "
                "(process_no=%s, cb_id=%s, movement_type=%s) — kapsam dışı.",
                process_no, _orig.pk, _orig.movement_type,
            )
            continue

        # Bakiye snapshot: REVERSAL outflow_types içinde sayıldığı için
        # kasanın bakiyesi orijinal tutar kadar düşer.
        try:
            _new_bal = (
                Decimal(str(_orig.cashbox.get_balance(_orig.currency)))
                - Decimal(str(_orig.amount))
            ).quantize(Decimal('0.01'))
        except Exception:
            _new_bal = Decimal('0.00')

        try:
            CashboxLedger.objects.create(
                cashbox=_orig.cashbox,
                store=_orig.store,
                movement_type=CashboxLedger.MovementType.REVERSAL,
                amount=_orig.amount,
                currency=_orig.currency,
                amount_eur_equivalent=_orig.amount_eur_equivalent,
                exchange_rate=_orig.exchange_rate,
                balance_snapshot=_new_bal,
                related_payment=_orig.related_payment,
                parent=_orig,
                process_no=process_no,
                description=(
                    f'İPTAL: cancel_row/cancel_group → process_no={process_no} '
                    f'(parent #{_orig.pk})'
                )[:255],
                created_by=_audit.get('actor'),
                ip_address=_audit.get('ip_address'),
                user_agent=_audit.get('user_agent') or '',
            )
            _reversed_count += 1
        except Exception as _err:
            logger.error(
                "_reverse_cashbox_for_process: REVERSAL yazımı başarısız "
                "(process_no=%s, cb_id=%s): %s",
                process_no, _orig.pk, _err,
            )

    if _reversed_count:
        logger.info(
            "_reverse_cashbox_for_process: process_no=%s → %d CashboxLedger "
            "REVERSAL yazıldı.",
            process_no, _reversed_count,
        )
    return _reversed_count


# ==========================================
# 3. İPTAL GÜVENLİK KONTROLLERİ (Safety Checks)
# ==========================================

def _can_cancel_process(p: Process) -> Tuple[bool, str]:
    """Bir ürün işleminin iptal edilip edilemeyeceğini kontrol eder."""
    status = str(getattr(p, "is_status", "")).upper()
    if status == "CANCELED":
        return (False, "Zaten iptal edilmiş")
    if getattr(p, "is_deleted", False):
        return (False, "Silinmiş kayıt")
    return (True, "")


def _can_cancel_ledger(l: SupplierLedger) -> Tuple[bool, str]:
    """Bir cari kaydın iptal edilip edilemeyeceğini kontrol eder."""
    if not getattr(l, "is_active", True):
        return (False, "Zaten iptal edilmiş")
    return (True, "")


def _can_cancel_ob_group(proc_no: str) -> Tuple[bool, str]:
    """
    OB (Açıktan Bağlama) grubunun iptal edilip edilemeyeceğini kontrol eder.
    """
    # Sadece AKTİF kayıtları getir
    rows = list(SupplierLedger.objects.filter(process_no=proc_no, is_active=True))

    # HİÇ KAYIT YOKSA
    if len(rows) == 0:
        return (False, "İptal edilecek aktif kayıt bulunamadı.")

    # TEK KAYIT KALDIYSA (Sizin durumunuz: USD gitmiş, HS kalmış)
    if len(rows) == 1:
        # Yetim kalmış satırın temizlenmesine izin veriyoruz.
        # Bu satır, "Borç Çevir" gibi işlemlerle diğer bacağı kapatılmış bir işlemin kalıntısıdır.
        return (True, "")

    # İKİ KAYIT VARSA (Normal Durum) - Meta kontrolü ve Tutar Eşleşmesi
    if len(rows) == 2:
        desc = rows[0].description or rows[1].description or ""
        meta = _parse_meta(desc)

        # Meta yoksa eski usul devam et
        if meta.get("kind") != "OB":
            return (True, "")

        try:
            f_cur = str(meta.get("from_cur", "")).upper()
            t_cur = str(meta.get("to_cur", "")).upper()
            f_amt = Decimal(str(meta.get("from_amt", "0")))
            t_amt = Decimal(str(meta.get("to_amt", "0")))
        except Exception:
            return (True, "")

        r1_amt = Decimal(str(rows[0].amount_value or 0))
        r1_cur = str(rows[0].currency).upper()
        r2_amt = Decimal(str(rows[1].amount_value or 0))
        r2_cur = str(rows[1].currency).upper()

        TOLERANCE = Decimal("0.05")

        # Satırların orijinal tutarlarla eşleşip eşleşmediğini kontrol et
        # Eğer Borç Çevir işlemi tutarın bir kısmını yediyse, burada uyumsuzluk çıkabilir.
        # Ancak kullanıcı iptal etmek istiyorsa ve grup tam ise izin verelim.
        # Güvenlik için sadece "para birimleri tutuyor mu" diye bakmak yeterli olabilir.

        return (True, "")

    return (False, "OB grubu bozulmuş (fazla satır).")


# ==========================================
# 4. GÖRÜNÜM FONKSİYONLARI (Views)
# ==========================================

@login_required
@require_GET
def operations_index(request):
    """İşlemler panosunu render eder."""
    return render(request, "management/transactions_board/operations_index.html")


@login_required
@require_GET
def ops_detail(request):
    """
    AJAX Endpoint: İşlem tablosunu doldurur.
    Process (Ürün hareketleri) ve Ledger (Cari hareketleri) verilerini birleştirir.
    """
    store = _current_store(request)
    if not store:
        return JsonResponse({"error_msg": "Mağaza bulunamadı."}, status=400)

    # --- Filtre Parametreleri ---
    kind = (request.GET.get("kind") or "ALL").upper()
    scope = (request.GET.get("scope") or "TODAY").upper()
    process_no = (request.GET.get("process_no") or "").strip()
    supplier_id = (request.GET.get("supplier_id") or "").strip()

    # --- Tarih Hesaplama ---
    now = timezone.now()
    start, end = None, None

    if scope == "TODAY":
        dfrom = request.GET.get("date_from")
        dto = request.GET.get("date_to")
        if dfrom:
            y, m, d = [int(x) for x in dfrom.split("-")]
            start = timezone.make_aware(datetime(y, m, d, 0, 0, 0))
        else:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if dto:
            y, m, d = [int(x) for x in dto.split("-")]
            end = timezone.make_aware(datetime(y, m, d, 23, 59, 59)) + timedelta(seconds=1)
        else:
            end = start + timedelta(days=1)

    elif scope == "RANGE":
        dfrom = request.GET.get("date_from")
        dto = request.GET.get("date_to")
        if dfrom:
            y, m, d = [int(x) for x in dfrom.split("-")]
            start = timezone.make_aware(datetime(y, m, d, 0, 0, 0))
        if dto:
            y, m, d = [int(x) for x in dto.split("-")]
            end = timezone.make_aware(datetime(y, m, d, 23, 59, 59)) + timedelta(seconds=1)

    # 1. PROCESS (Ürün Hareketleri) Sorgusu
    purchase_has = Decimal("0")
    sale_has = Decimal("0")
    proc_rows = []
    procs = []

    # Eğer OB veya CONV seçildiyse ürün tablosu getirme (sadece cari işlem getir)
    if kind not in ["OB", "CONV"]:
        qs = Process.objects.filter(store=store)
        if start: qs = qs.filter(date__gte=start)
        if end: qs = qs.filter(date__lt=end)
        if process_no: qs = qs.filter(process_no=process_no)

        if kind != "ALL":
            if kind == "FAST":
                qs = qs.filter(process_type="FAST_PROCESS")
            else:
                qs = qs.filter(process_type=kind)

        procs = list(qs.select_related('product', 'bank_account').order_by("-date", "-id")[:500])

    # Process satırlarını hazırla
    for p in procs:
        tx = (getattr(p, "transaction_type", None) or "SALE").upper()

        # FAZ 23: Kasa kalemi tespiti (product=None, bank_account doluysa)
        is_cash_item = (not p.product) and getattr(p, 'bank_account_id', None)

        # Has/Gram İstatistik Hesabı
        gram = Decimal(str(getattr(p, "gram", 0) or 0))
        piece = Decimal(str(getattr(p, "piece", 0) or 0))
        has_g = Decimal("0")

        if not is_cash_item:
            if gram > 0:
                kar = getattr(p, "karat", 24) or 24
                has_g = gram * (Decimal(str(kar)) / 24)
            elif piece > 0 and p.product:
                phs = Decimal(str(getattr(p.product, "product_hs", 0) or 0))
                has_g = piece * phs

        if has_g > 0:
            if tx in {"PURCHASE", "STOCK_IN", "ORDER_IN"}:
                purchase_has += has_g
            elif tx in {"SALE"}:
                sale_has += has_g

        # Para birimi ve Gram durumu
        curr = "TRY"
        is_gram = False

        if is_cash_item:
            # Kasa kalemi: payment_currency'den para birimini al
            curr = str(getattr(p, "payment_currency", "") or "TRY").upper()
        elif p.product:
            curr = str(getattr(p.product, "currency", "TRY") or "TRY").upper()
            is_gram = getattr(p.product, "is_gram_bullion", False)

        # Tutar değerlerini Decimal olarak alalım
        price_hs_val = Decimal(str(getattr(p, "price_hs", 0) or 0))
        amount_val = Decimal(str(getattr(p, "amount", 0) or 0))

        # --- Doğru Tutarı Seçme ---
        if is_cash_item:
            # Kasa kalemi: amount alanı zaten doğru tutarı taşır
            display_amount = amount_val
            formatted_amount = float(_q2(display_amount))
        elif curr == "HS" or is_gram or price_hs_val > 0:
            display_amount = price_hs_val
            curr = "HS"  # Ekranda PB kısmında HS yazması için garantiliyoruz
            formatted_amount = float(_q3(display_amount))  # Has 3 hane
        else:
            display_amount = amount_val
            formatted_amount = float(_q2(display_amount))  # Döviz/TL 2 hane

        can, reason = _can_cancel_process(p)
        gram = Decimal(str(getattr(p, "gram", 0) or 0))
        piece = Decimal(str(getattr(p, "piece", 0) or 0))

        # Miktar gösterimi
        if is_cash_item:
            qty_disp = "-"
        elif gram > 0:
            qty_disp = f"{_fmt_tr(gram, 'GR')} gr"
        else:
            qty_disp = f"{int(piece)} Ad"

        proc_rows.append({
            "row_id": str(p.id),
            "date": _proc_datetime(p).strftime("%Y-%m-%d %H:%M"),
            "process_type": _proc_kind_tr(p),
            "tx": tx,
            "name": _product_name(p),
            "qty": qty_disp,
            "amount": formatted_amount,  # Düzeltilmiş tutar
            "amount_eur": float(_q2(amount_val)),
            "currency": curr,  # Düzeltilmiş para birimi
            "process_no": _proc_no(p),
            "can_cancel": can,
            "disable_reason": reason,
            "is_status": str(getattr(p, "is_status", "") or ""),
        })

    cash_in, cash_out = _collect_payments_for_store(store, start, end)

    # 2. LEDGER (Cari/OB) Sorgusu
    bind_rows = []
    # YENİ: OB ve CONV türlerini de dahil et
    show_bind = kind in {"ALL", "WHOLESALE", "OB", "CONV"}

    if show_bind:
        from django.db.models import Q

        # Temel Sorgu
        lq = SupplierLedger.objects.filter(supplier__store=store, is_active=True)

        # Türlere göre prefix filtreleme
        if kind == "OB":
            lq = lq.filter(process_no__startswith='OB')
        elif kind == "CONV":
            lq = lq.filter(process_no__startswith='C')
        else:
            # ALL veya WHOLESALE durumunda ikisini de getir
            lq = lq.filter(Q(process_no__startswith='OB') | Q(process_no__startswith='C'))

        if supplier_id: lq = lq.filter(supplier__id=supplier_id)
        if start: lq = lq.filter(created_on__gte=start)
        if end: lq = lq.filter(created_on__lt=end)
        if process_no: lq = lq.filter(process_no=process_no)

        grouped_ops = {}
        normals = []

        for lg in lq.order_by("-created_on", "-id")[:500]:
            pn = getattr(lg, "process_no", "") or ""
            # HEM OB HEM DE C (Çeviri) İŞLEMLERİNİ GRUPLA!
            if pn.startswith("OB") or pn.startswith("C"):
                grouped_ops.setdefault(pn, []).append(lg)
            else:
                normals.append(lg)

        # Normal Cari Kayıtlarını Listeye Ekle (Kaldıysa)
        for lg in normals:
            q_gram = getattr(lg, "quantity_gram", Decimal("0")) or Decimal("0")
            q_piece = getattr(lg, "quantity_piece", 0) or 0
            qty_txt = f"{_fmt_tr(q_gram, 'GR')} gr" if q_gram > 0 else f"{int(q_piece)} Ad"

            can, reason = _can_cancel_ledger(lg)
            sup_name = lg.supplier.company_name if lg.supplier else "-"

            bind_rows.append({
                "row_id": str(lg.id),
                "date": lg.created_on.strftime("%Y-%m-%d %H:%M"),
                "supplier": sup_name,
                "kind": str(getattr(lg, "transaction_type", "ENTRY")).upper(),
                "qty": qty_txt,
                "amount": float(_q3(getattr(lg, "amount_value", 0))),
                "currency": str(getattr(lg, "currency", "HS")).upper(),
                "process_no": getattr(lg, "process_no", "") or "-",
                "can_cancel": can,
                "tooltip": reason
            })

        # GRUPLANMIŞ İŞLEMLERİ (OB ve C) OKUYUP FORMATLA
        for pn, rows in grouped_ops.items():

            # İşlemin tüm detayları description alanındaki JSON'da kayıtlı.
            # Satırlardan herhangi birinden bu metayı alabiliriz.
            m = {}
            for r in rows:
                parsed = _parse_meta(r.description or "")
                if parsed and "from_cur" in parsed:
                    m = parsed
                    break

            if m:
                # Meta JSON datasından verileri çekiyoruz
                f_cur = str(m.get("from_cur", "")).upper()
                t_cur = str(m.get("to_cur", "")).upper()

                try:
                    f_amt = Decimal(str(m.get("from_amt", "0")))
                    t_amt = Decimal(str(m.get("to_amt", "0")))
                except:
                    t_amt, f_amt = Decimal("0"), Decimal("0")

                kind_str = "BORÇ ÇEVİRİ" if pn.startswith("C") else "AÇIKTAN BAĞLAMA"

                can_ob, reason_ob = _can_cancel_ob_group(pn)
                dt = max([r.created_on for r in rows])
                sup_name = rows[0].supplier.company_name if rows[0].supplier else "-"
                qty_display = f"{_fmt_tr(f_amt, f_cur)} {f_cur} → {_fmt_tr(t_amt, t_cur)} {t_cur}"

                bind_rows.append({
                    "row_id": pn,
                    "date": dt.strftime("%Y-%m-%d %H:%M"),
                    "supplier": sup_name,
                    "kind": kind_str,
                    "qty": qty_display,
                    "amount": float(_q3(t_amt)),
                    "currency": t_cur,
                    "process_no": pn,
                    "can_cancel": can_ob,
                    "tooltip": reason_ob
                })
            else:
                for lg in rows:
                    bind_rows.append({
                        "row_id": str(lg.id),
                        "date": lg.created_on.strftime("%Y-%m-%d %H:%M"),
                        "supplier": lg.supplier.company_name if lg.supplier else "-",
                        "kind": str(getattr(lg, "transaction_type", "ENTRY")).upper(),
                        "amount": float(_q3(getattr(lg, "amount_value", 0))),
                        "currency": str(getattr(lg, "currency", "HS")).upper(),
                        "process_no": pn,
                        "can_cancel": False,
                        "tooltip": "Veri yapısı okunamadı"
                    })

    return JsonResponse({
        "stats": {
            "purchase_has": float(_q3(purchase_has)),
            "sale_has": float(_q3(sale_has)),
            "cash_in": float(_q2(cash_in)),
            "cash_out": float(_q2(cash_out)),
        },
        "proc": proc_rows,
        "bind": bind_rows
    }, safe=False)


@login_required
@require_POST
def cancel_row(request):
    """
    Güçlendirilmiş İptal Fonksiyonu.

    HURDA SSOT (Refactor):
        Hurda PURCHASE (is_scrap=True, transaction_type='PURCHASE') iptalinde
        merkezi cancel_scrap_purchase servisi çağrılır. Böylece:
          - StockLedger reversal
          - SupplierLedger soft-disable (process_no + source_process_id)
          - Process status güncelleme
          - WAC/milyem recalculate
        hepsi tek @transaction.atomic içinde, merkezi servis tarafından yürütülür.
        Diğer işlem türleri mevcut _revert_process_stock + manuel SL akışını korur.
    """
    row_id = (request.POST.get("row_id") or "").strip()
    row_type = (request.POST.get("row_type") or "PROC").upper()

    if not row_id:
        return JsonResponse({"result": False, "error_msg": "Kayıt ID bulunamadı."}, status=400)

    try:
        # =========================================================
        # SENARYO 1: ÜRÜN İŞLEMİ (PROCESS TABLOSU)
        # =========================================================
        if row_type == "PROC":
            p = get_object_or_404(Process, id=row_id)
            # Standart kontroller
            can, reason = _can_cancel_process(p)
            if not can:
                return JsonResponse({"result": False, "error_msg": reason}, status=400)

            with transaction.atomic():
                # --- YENİ EKLENEN KISIM: EMANET (CUSTODY) İPTALİ ---
                # Eğer bu satış ürünü emanete alındıysa, o emanet kaydını buluyoruz
                c_rec = CustomerCustodyLedger.objects.filter(
                    process_no=p.process_no,
                    product=p.product,
                    custody_type=CustomerCustodyLedger.CUSTODY_IN
                ).first()

                if c_rec:
                    # Müşteri ürünü zaten dükkandan teslim almışsa satış iptalini engelliyoruz!
                    if c_rec.is_returned:
                        return JsonResponse({"result": False,
                                             "error_msg": f"'{p.product.name}' isimli ürün emanetten teslim alınmış. Satış iptali için önce emanet teslimini geri almalısınız!"},
                                            status=400)

                    # R-FAZ 5: Custody reversal — cancel_stock_entry ile
                    # 1:1 ref eşleşmeli reverse (ref_type='process_custody'
                    # complete_process tarafında ref_id=str(p.id) ile yazıldı).
                    try:
                        cancel_stock_entry(
                            ref_type='process_custody',
                            ref_id=str(p.id),
                            user=request.user,
                            reverse_supplier_ledger=False,
                            notes=f"Custody İptali - {p.process_no or p.id}",
                            raise_if_not_found=False,
                        )
                    except Exception as _custody_err:
                        logger.error(
                            "cancel_row custody reversal failed (process_id=%s): %s",
                            p.id, _custody_err,
                        )

                    # R-FAZ 5: Soft-delete — CustomerCustodyLedger is_deleted/is_active alanlarına sahip.
                    c_rec.is_deleted = True
                    c_rec.is_active = False
                    c_rec.save(update_fields=['is_deleted', 'is_active'])
                # --------------------------------------------------
                # Stok + Tedarikçi Cari + Process durumu
                # ────────────────────────────────────────────────────────────
                # HURDA / BİLEZİK SSOT:
                #   Hurda PURCHASE  → apps.scraps.services.cancel_scrap_purchase
                #   Bilezik PURCHASE → apps.bracelets.services.cancel_bracelet_purchase
                # Her iki servis de stok reversal + SupplierLedger soft-disable
                # + Process status + WAC recalculate işlemini atomik yapar.
                # Diğer işlem türleri mevcut _revert_process_stock akışını korur.
                # ────────────────────────────────────────────────────────────
                _tx_type_upper = (p.transaction_type or '').upper()
                _is_scrap_purchase = (
                    bool(getattr(p.product, 'is_scrap', False))
                    and _tx_type_upper == 'PURCHASE'
                )

                _is_bracelet_purchase = False
                if not _is_scrap_purchase and _tx_type_upper == 'PURCHASE':
                    from apps.bracelets.services.cancel_bracelet_service import (
                        is_bracelet_product,
                    )
                    _is_bracelet_purchase = is_bracelet_product(p.product)

                if _is_scrap_purchase:
                    # FAZ 53 — cancel_scrap_purchase exception kontrolü
                    from apps.scraps.services.cancel_scrap_service import (
                        cancel_scrap_purchase,
                        CancelNotAllowedError,
                        CancelScrapError,
                    )
                    try:
                        cancel_scrap_purchase(
                            process_id=p.id, user=request.user,
                        )
                    except CancelScrapError as _sc_err:
                        raise StockReversalRequired(str(_sc_err)) from _sc_err
                elif _is_bracelet_purchase:
                    # FAZ 53 — cancel_bracelet_purchase dönüş kontrolü
                    # Eski davranış: dönüş değeri yok sayılıyor; servis silent
                    # fail vererek stock_reversed=0 dönse bile transaction
                    # commit ediyordu. Yeni davranış: stock_reversed=0 +
                    # COMPLETED ise StockReversalRequired raise edilir
                    # (servis kendi içinde de CancelBraceletError fırlatabilir).
                    from apps.bracelets.services.cancel_bracelet_service import (
                        cancel_bracelet_purchase,
                        CancelNotAllowedError,
                        CancelBraceletError,
                    )
                    try:
                        _br_res = cancel_bracelet_purchase(
                            process_id=p.id, user=request.user,
                        )
                    except CancelBraceletError as _br_err:
                        raise StockReversalRequired(str(_br_err)) from _br_err

                    _br_stock = int(_br_res.get('stock_reversed') or 0)
                    if (_br_stock == 0
                            and (p.is_status or '').upper() == 'COMPLETED'
                            and Decimal(str(p.gram or 0)) > 0
                            and not bool(getattr(p, 'waiting_stock', False))):
                        raise StockReversalRequired(
                            f"Bilezik stok geri sarılamadı "
                            f"(process_no={p.process_no or p.id}). "
                            "StockLedger eşleşmesi yok; işlem iptal edilmedi."
                        )
                else:
                    # Hurda/bilezik dışı işlem: mevcut stok reversal + manuel SL
                    # FAZ 52 (A-3+A-5): _revert_process_stock artık dict döner.
                    # Stok reversal exception verir veya COMPLETED process için
                    # eşleşme bulamazsa StockReversalRequired raise edilir;
                    # transaction tamamen rollback olur ve kullanıcıya açık
                    # hata döner. Eskiden silent fail → SL geri sarılır
                    # stok stale kalırdı (toptan ziynet hatası).
                    _stock_res = _revert_process_stock(p, request.user)
                    if not _stock_res.get('success', True):
                        raise StockReversalRequired(
                            f"Stok geri sarılamadı: {_stock_res.get('reason')}"
                            + (
                                f" — {_stock_res.get('error')}"
                                if _stock_res.get('error') else ''
                            )
                            + f" (process_no={p.process_no or p.id}). "
                            + "İşlem iptal edilmedi; lütfen stok durumunu "
                            + "kontrol edin veya teknik destekle iletişime geçin."
                        )
                    _mark_process_canceled(p)

                    # FAZ 21 / Bug 2B — Satır-bazlı SupplierLedger pasifleştirme.
                    # FAZ 51 (R-02): Append-only REVERSAL helper'ına yönlendirildi.
                    # Helper hem orijinali pasifleştirir hem REVERSAL audit
                    # satırı yazar. Bakiye davranışı değişmez.
                    if p.process_no:
                        from apps.suppliers.services import (
                            reverse_supplier_ledger_for_process,
                        )
                        _sl_audit = extract_audit_context(request)
                        _sl_reason = f'Process iptal — {p.process_no}'
                        _sl_res = reverse_supplier_ledger_for_process(
                            audit=_sl_audit, reason=_sl_reason,
                            process_id=p.id,
                        )
                        _row_passive_count = _sl_res.get('reversed_count', 0)

                        if _row_passive_count == 0:
                            _other_active_proc = Process.objects.filter(
                                process_no=p.process_no,
                            ).exclude(
                                Q(is_deleted=True) | Q(is_status='CANCELED') | Q(transaction_type='CANCELED'),
                            ).exclude(id=p.id).exists()
                            if not _other_active_proc:
                                _sl_legacy = reverse_supplier_ledger_for_process(
                                    audit=_sl_audit, reason=_sl_reason,
                                    process_no=p.process_no,
                                )
                                logger.info(
                                    "cancel_row PROC: legacy fallback ile process_no=%s "
                                    "SupplierLedger %d satır REVERSAL (son aktif satır).",
                                    p.process_no,
                                    _sl_legacy.get('reversed_count', 0),
                                )
                        else:
                            logger.info(
                                "cancel_row PROC: source_process_id=%s ile %d "
                                "SupplierLedger satırı REVERSAL yazıldı.",
                                p.id, _row_passive_count,
                            )

                    # R-FAZ 4 + FAZ 35: CustomerLedger iptali.
                    # CustomerLedger satırı checkout başına TEK kez yazılır (aggregate
                    # balance_diff). Tek satır iptalinde grupta hâlâ aktif Process
                    # kalıyorsa müşteri bakiyesi olduğu gibi tutulur — kalan satıra
                    # ilişkin ödeme farkı geçerli. Tüm grup iptal olduğunda
                    # (son satır da temizlendiyse) CustomerLedger REVERSAL'a çekilir.
                    #
                    # FAZ 35: append-only REVERSAL pattern. Eski mass `is_active=False`
                    # mutation kaldırıldı; LedgerService.reverse_entry() ile her satır
                    # için iz kaydı + Kasa/P&L senkronizasyonu sağlanır. Onay
                    # gerektiren büyük tutarlar PENDING durumda yazılır (aktör yetkisiz
                    # ise). Çift iptal idempotent (zaten REVERSAL varsa atlanır).
                    _remaining_active = Process.objects.filter(
                        process_no=p.process_no,
                    ).exclude(
                        Q(is_deleted=True) | Q(is_status='CANCELED') | Q(transaction_type='CANCELED'),
                    ).exclude(id=p.id).exists()
                    if not _remaining_active:
                        _cancel_audit = extract_audit_context(request)
                        _cancel_reason = f'Process iptal — {p.process_no}'
                        _cl_qs = CustomerLedger.objects.filter(
                            process_no=p.process_no, is_active=True,
                        ).exclude(transaction_type=CustomerLedger.REVERSAL)
                        _rev_count = 0
                        _skip_count = 0
                        for _cl_entry in _cl_qs:
                            try:
                                LedgerService.reverse_entry(
                                    original=_cl_entry,
                                    audit=_cancel_audit,
                                    reason=_cancel_reason,
                                )
                                _rev_count += 1
                            except InvalidLedgerStateError as _exc:
                                # Zaten REVERSAL var veya geçersiz state — log ve atla
                                _skip_count += 1
                                logger.warning(
                                    "cancel_row PROC: CustomerLedger #%s reverse_entry atlandı: %s",
                                    _cl_entry.pk, _exc,
                                )
                        if _rev_count or _skip_count:
                            logger.info(
                                "cancel_row PROC: process_no=%s → %d CustomerLedger REVERSAL yazıldı (%d atlandı).",
                                p.process_no, _rev_count, _skip_count,
                            )

                        # ────────────────────────────────────────────────
                        # FAZ 41 — Paired Tahsilat (TAH-) Reversal'ı.
                        # ────────────────────────────────────────────────
                        # Sorun: CollectionService.collect_and_close()
                        # tahsilat sırasında `process_no = TAH-YYYYMMDDHHMMSS`
                        # üretir; bu kod Process tablosuna YAZILMAZ.
                        # Mevcut cancel_row mantığı yalnızca satışın
                        # process_no'su (P02814...) üzerinden CustomerLedger
                        # arar. Tahsilat (TAH-...) prefix'iyle yazılan
                        # COLLECTION_* satırları reverse edilmediği için:
                        #   - Müşteri "alacaklı" yanıltıcı pozisyona düşer
                        #   - Kasada karşılığı olmayan giriş kalır
                        #   - Tahsilat Payment kaydı aktif görünür
                        #
                        # Çözüm: Satış DEBT'i reverse edildikten sonra,
                        # bu satıştan SONRA yapılmış (created_on >= sale_date)
                        # ve hâlâ aktif COLLECTION_* satırlarını FIFO ile
                        # reverse et — toplam reverse miktarı satışın
                        # DEBT toplamını AŞMASIN (önceki tahsilatları
                        # koruma garantisi).
                        #
                        # NOT: Bu bir heuristic'tir — birden fazla satış
                        # arka arkaya iptal edilirken aynı tahsilat birden
                        # fazla kez "paired" sayılmasın diye is_active=True
                        # ve transaction_type != REVERSAL filtresi
                        # idempotency sağlar.
                        if p.customer_id:
                            _sale_debt_total_hs = (
                                CustomerLedger.objects
                                .filter(
                                    process_no=p.process_no,
                                    transaction_type__in=Customers.DEBT_INCREASING_TYPES,
                                )
                                .aggregate(s=Sum('amount_hs'))['s']
                                or Decimal('0')
                            )
                            _sale_debt_total_hs = Decimal(str(_sale_debt_total_hs or 0))
                            _sale_date = p.date or timezone.now()
                            if _sale_debt_total_hs > Decimal('0'):
                                _coll_qs = (
                                    CustomerLedger.objects
                                    .filter(
                                        customer_id=p.customer_id,
                                        is_active=True,
                                        created_on__gte=_sale_date,
                                        transaction_type__in=Customers.DEBT_DECREASING_TYPES,
                                        related_payment__isnull=False,
                                    )
                                    .exclude(transaction_type=CustomerLedger.REVERSAL)
                                    .select_related('related_payment')
                                    .order_by('created_on')
                                )
                                _consumed_hs = Decimal('0')
                                _coll_rev = 0
                                _coll_skip = 0
                                _paired_payment_ids = []
                                for _coll in _coll_qs:
                                    if _consumed_hs >= _sale_debt_total_hs:
                                        break
                                    _rp = _coll.related_payment
                                    # Sadece TAH- prefix'li tahsilat
                                    # Payment'larını dikkate al — satış
                                    # parçalı ödemeleri (process_no=satış)
                                    # zaten yukarıda ele alındı.
                                    if not (_rp and (_rp.process_no or '').startswith('TAH-')):
                                        continue
                                    try:
                                        LedgerService.reverse_entry(
                                            original=_coll,
                                            audit=_cancel_audit,
                                            reason=(
                                                f'Satış iptali nedeniyle '
                                                f'tahsilat geri alındı — {p.process_no}'
                                            ),
                                        )
                                        _consumed_hs += Decimal(str(_coll.amount_hs or 0))
                                        _coll_rev += 1
                                        _paired_payment_ids.append(_rp.id)
                                    except InvalidLedgerStateError as _exc:
                                        _coll_skip += 1
                                        logger.warning(
                                            "cancel_row PROC: paired tahsilat "
                                            "CustomerLedger #%s reverse_entry atlandı: %s",
                                            _coll.pk, _exc,
                                        )
                                # Reverse edilen tahsilat Payment'larını da
                                # is_cancelled=True yap (Ödemeler sekmesinde
                                # görünmesin — propagate_reversal_side_effects
                                # CashboxLedger.REVERSAL'ı zaten yazdı).
                                if _paired_payment_ids:
                                    Payment.objects.filter(
                                        id__in=_paired_payment_ids,
                                        is_cancelled=False,
                                    ).update(
                                        is_cancelled=True,
                                        cancelled_at=timezone.now(),
                                    )
                                if _coll_rev or _coll_skip:
                                    logger.info(
                                        "cancel_row PROC: process_no=%s → "
                                        "satışla paired %d tahsilat reverse edildi "
                                        "(%d atlandı, ~%s HS tüketildi).",
                                        p.process_no, _coll_rev, _coll_skip,
                                        _consumed_hs,
                                    )

                    # --- FAZ 15: Payment Rollback ---
                    _cancelled_payment_count = Payment.objects.filter(
                        process_no=p.process_no,
                        is_cancelled=False,
                    ).update(
                        is_cancelled=True,
                        cancelled_at=timezone.now(),
                    )
                    if _cancelled_payment_count > 0:
                        logger.info(
                            "cancel_row PROC: process_no=%s → %d Payment kaydı iptal edildi.",
                            p.process_no, _cancelled_payment_count,
                        )

                    # FAZ 52 (S-03): Kasa İptali Karşı Girişi.
                    # Wholesale kasa kalemi (bank_account_id is not None)
                    # iptalinde Payment.is_cancelled=True yapıyoruz; ama
                    # CashboxLedger SSOT olduğu için karşı bir REVERSAL
                    # satırı yazılmazsa kasa bakiyesi şişiyordu. Helper
                    # idempotent: aynı parent için ikinci REVERSAL yazmaz.
                    # Outflow-yönlü originaller (SALE) kapsam dışı (yön
                    # düzeltmesi ileri faza bırakıldı).
                    try:
                        _cb_audit = extract_audit_context(request)
                        _reverse_cashbox_for_process(
                            process_no=p.process_no,
                            audit=_cb_audit,
                        )
                    except Exception as _cb_err:
                        logger.error(
                            "cancel_row PROC: CashboxLedger reversal failed "
                            "(process_no=%s): %s",
                            p.process_no, _cb_err,
                        )

                # ── Yön B: Barkodlu ürün alım iptali → GoldPurchases + Product soft-delete ──
                if p.product and p.transaction_type == 'PURCHASE':
                    _gp_qs = GoldPurchases.objects.filter(
                        product=p.product, is_deleted=False
                    )
                    if _gp_qs.exists():
                        _gp_qs.update(is_deleted=True)
                        p.product.is_deleted = True
                        p.product.barcode = ''
                        p.product.save(update_fields=['is_deleted', 'barcode'])

                # ── FAZ 19 / Bulgu C — Hurda/Bilezik Havuz Temizleme Symmetry ──
                # apps/process/views.py:delete() (perakende trash) iptalde havuz
                # boşaldıysa Bracelets/Scraps satırını ve Products.is_active'i
                # soft-delete ediyor. operations.py:cancel_row PROC dalında bu
                # mantık eksikti → İşlemler ekranından iptal edilen hurda/bilezik
                # alımı sonrası Perakende ürün katalog listesinde "hayalet kayıt"
                # olarak görünüyordu (is_active=True kaldığı için).
                # Şimdi her iki iptal yolunda da aynı temizlik uygulanır.
                if p.product and p.store and p.transaction_type == 'PURCHASE':
                    _cat_name = ''
                    try:
                        if p.product.category:
                            _cat_name = (p.product.category.name or '').lower()
                    except Exception:
                        _cat_name = ''
                    _is_hurda = 'hurda' in _cat_name
                    _is_bilezik = 'bilezik' in _cat_name

                    if _is_hurda or _is_bilezik:
                        try:
                            _snap_after = StockSnapshot.objects.filter(
                                product=p.product, store=p.store
                            ).first()
                            _pool_empty = (
                                (not _snap_after) or
                                (_snap_after.stock_gram is None) or
                                (_snap_after.stock_gram <= Decimal('0'))
                            )
                            _has_other_active = Process.objects.filter(
                                product=p.product, store=p.store,
                                transaction_type='PURCHASE', is_deleted=False,
                            ).exclude(is_status='CANCELED').exclude(id=p.id).exists()

                            if _pool_empty and not _has_other_active:
                                Products.objects.filter(id=p.product.id).update(is_active=False)
                                if _is_bilezik:
                                    from apps.bracelets.models import Bracelets
                                    Bracelets.objects.filter(
                                        product=p.product, store=p.store, is_deleted=False
                                    ).update(is_deleted=True, is_active=False)
                                elif _is_hurda:
                                    from apps.scraps.models import Scraps
                                    Scraps.objects.filter(
                                        product=p.product, store=p.store, is_deleted=False
                                    ).update(is_deleted=True, is_active=False)
                                logger.info(
                                    "cancel_row PROC: havuz boşaldı → product_id=%s, "
                                    "kategori=%s soft-delete edildi.",
                                    p.product.id, _cat_name,
                                )
                        except Exception:
                            logger.exception(
                                "cancel_row PROC: havuz cleanup başarısız "
                                "(process_id=%s, product_id=%s)",
                                p.id, getattr(p.product, 'id', None),
                            )

            return JsonResponse({"result": True, "msg": "Ürün işlemi iptal edildi."})

        # =========================================================
        # SENARYO 2: CARİ / BAĞLAMA / ÇEVİRİ (SUPPLIERLEDGER TABLOSU)
        # =========================================================
        if row_type == "BIND":
            target_process_no = None

            # A) Gelen ID doğrudan bir Grup Numarası mı? (OB... veya C...)
            # UUID uzunluğu genelde 36 karakterdir. Farklıysa işlem nosudur.
            if len(row_id) != 36:
                target_process_no = row_id

            # B) Gelen ID bir UUID mi? (Tekil Satır ID'si)
            else:
                try:
                    ledger_item = SupplierLedger.objects.get(id=row_id)

                    # KRİTİK NOKTA:
                    # Satırın bir "process_no"su varsa, bu bir GRUP işlemidir (Çeviri, OB, Toptan vb.)
                    # Bu durumda sadece satırı değil, o numaraya ait GRUBU hedefliyoruz.
                    if ledger_item.process_no:
                        target_process_no = ledger_item.process_no
                    else:
                        # İşlem numarası yoksa (Manuel ekleme), sadece bu satırı sil.
                        with transaction.atomic():
                            ledger_item.is_active = False
                            ledger_item.save(update_fields=["is_active"])
                        return JsonResponse({"result": True, "msg": "Tekil cari satırı silindi."})

                except SupplierLedger.DoesNotExist:
                    return JsonResponse({"result": False, "error_msg": "Kayıt bulunamadı."}, status=404)

            # --- TOPLU TEMİZLİK (CLEANUP) ---
            # Eğer bir Process No bulduysak (OB..., C..., veya P...), o numaraya ait
            # NE VARSA (Giriş, Çıkış, Tek kalmış, Çift kalmış fark etmez) hepsini pasife çek.
            if target_process_no:
                with transaction.atomic():

                    # --- YENİ EKLENEN KOD BAŞLANGICI: BORÇ ÇEVİRİ İPTAL İADESİ ---
                    if target_process_no.startswith("C"):
                        group_rows = SupplierLedger.objects.filter(process_no=target_process_no)
                        meta_data = {}
                        supplier_obj = None

                        # İşlemin orjinal bilgilerini JSON(description) üzerinden bulalım
                        for r in group_rows:
                            if not supplier_obj and r.supplier:
                                supplier_obj = r.supplier
                            parsed = _parse_meta(r.description or "")
                            if parsed and "from_cur" in parsed:
                                meta_data = parsed
                                break

                        if meta_data and supplier_obj:
                            f_cur = str(meta_data.get("from_cur", ""))
                            f_amt = Decimal(str(meta_data.get("from_amt", "0")))
                            orig_tx_type = meta_data.get("tx_type")  # Asıl kaynağın yönü (ENTRY veya EXIT)

                            if f_cur and f_amt > 0 and orig_tx_type:
                                # Kayıp bakiyeyi sisteme "İptal İadesi" olarak geri ekliyoruz
                                SupplierLedger.objects.create(
                                    supplier=supplier_obj,
                                    transaction_type=orig_tx_type,
                                    amount_value=f_amt,
                                    currency=f_cur,
                                    process_no=f"REV-{target_process_no}",
                                    description=f"Çeviri İptal İadesi | Orj: {target_process_no}",
                                    is_active=True
                                )

                    updated_count = SupplierLedger.objects.filter(
                        process_no=target_process_no,
                        is_active=True
                    ).update(is_active=False)

                if updated_count > 0:
                    return JsonResponse(
                        {"result": True, "msg": f"İşlem grubu temizlendi ({updated_count} satır)."})
                else:
                    return JsonResponse({
                        "result": False,
                        "error_msg": "Silinecek aktif kayıt bulunamadı (Zaten silinmiş olabilir)."
                    }, status=400)

    except StockReversalRequired as _stock_err:
        # FAZ 52 (A-5): Stok reversal başarısız → transaction rollback edildi
        # (atomic block içinde raise → otomatik rollback). Kullanıcıya 409
        # Conflict ile açık mesaj dön. Eski davranışta {"result": True} dönüyordu
        # → kullanıcı hatadan habersiz kalıyordu.
        logger.warning(
            "cancel_row: StockReversalRequired → işlem iptal edilmedi (msg=%s)",
            str(_stock_err),
        )
        return JsonResponse({
            "result": False,
            "error_msg": str(_stock_err),
            "stock_reversal_failed": True,
        }, status=409)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"result": False, "error_msg": f"Sistem Hatası: {str(e)}"}, status=500)

    return JsonResponse({"result": False, "error_msg": "Bilinmeyen işlem türü."}, status=400)


@login_required
@require_POST
def cancel_group(request):
    """
    TOPLU İPTAL: Bir Process No'ya ait tüm stok hareketlerini ve cari kayıtlarını iptal eder.
    Toptan işlemlerde tüm faturayı iptal etmek için kullanılır.
    """
    pn = (request.POST.get("process_no") or "").strip()
    if not pn:
        return JsonResponse({"result": False, "error_msg": "İşlem Numarası zorunlu."}, status=400)

    # Kayıtları Bul
    rows = list(Process.objects.filter(process_no=pn))
    ledger_rows = list(SupplierLedger.objects.filter(process_no=pn, is_active=True))

    if not rows and not ledger_rows:
        return JsonResponse({"result": False, "error_msg": "Bu numaraya ait kayıt bulunamadı."}, status=404)

    # FAZ 52 (A-3+A-5+S-04): StockReversalRequired guard + sentinel pattern.
    # Stok başarısızlığında transaction.set_rollback(True) + flag kontrolü ile
    # `with` bloğundan temiz çıkış; eski davranışta kısmi başarı oluşabiliyordu
    # (bazı satırların stoğu geri sarılmadan SL toplu pasifleştiriliyordu).
    _stock_failure_msg = None

    with transaction.atomic():
        # 1. Process Satırlarını İptal Et ve Stoğu Düzelt
        for p in rows:
            can, _ = _can_cancel_process(p)
            if can:
                # --- YENİ EKLENEN KISIM: EMANET (CUSTODY) İPTALİ ---
                c_rec = CustomerCustodyLedger.objects.filter(
                    process_no=p.process_no,
                    product=p.product,
                    custody_type=CustomerCustodyLedger.CUSTODY_IN
                ).first()

                if c_rec:
                    if c_rec.is_returned:
                        # Transaction'ı rollback yapmak ve işlemi durdurmak için Exception fırlatıyoruz.
                        raise Exception(
                            f"'{p.product.name}' isimli ürün emanetten teslim alınmış. Toplu iptal yapılamaz. Önce emanet teslimini geri almalısınız!")

                    # R-FAZ 5: Custody reversal — cancel_stock_entry ile.
                    try:
                        cancel_stock_entry(
                            ref_type='process_custody',
                            ref_id=str(p.id),
                            user=request.user,
                            reverse_supplier_ledger=False,
                            notes=f"Custody Toplu İptal - {p.process_no or p.id}",
                            raise_if_not_found=False,
                        )
                    except Exception as _custody_err:
                        logger.error(
                            "cancel_group custody reversal failed (process_id=%s): %s",
                            p.id, _custody_err,
                        )
                    # Soft-delete — CustomerCustodyLedger is_deleted/is_active alanlarına sahip.
                    c_rec.is_deleted = True
                    c_rec.is_active = False
                    c_rec.save(update_fields=['is_deleted', 'is_active'])
                # --------------------------------------------------
                # HURDA / BİLEZİK SSOT: PURCHASE iptali → ilgili merkezi servis,
                # diğer işlem türleri → mevcut akış.
                _tx_type_upper = (p.transaction_type or '').upper()
                _is_scrap_purchase = (
                    bool(getattr(p.product, 'is_scrap', False))
                    and _tx_type_upper == 'PURCHASE'
                )
                _is_bracelet_purchase = False
                if not _is_scrap_purchase and _tx_type_upper == 'PURCHASE':
                    from apps.bracelets.services.cancel_bracelet_service import (
                        is_bracelet_product,
                    )
                    _is_bracelet_purchase = is_bracelet_product(p.product)

                if _is_scrap_purchase:
                    # FAZ 53 — cancel_scrap_purchase exception kontrolü
                    from apps.scraps.services.cancel_scrap_service import (
                        cancel_scrap_purchase, CancelScrapError,
                    )
                    try:
                        cancel_scrap_purchase(
                            process_id=p.id, user=request.user,
                        )
                    except CancelScrapError as _sc_err:
                        _stock_failure_msg = (
                            f"Toplu iptal: hurda stok geri sarılamadı "
                            f"(process_no={p.process_no or p.id}) — {_sc_err}"
                        )
                        transaction.set_rollback(True)
                        break
                elif _is_bracelet_purchase:
                    # FAZ 53 — cancel_bracelet_purchase dönüş + exception kontrolü
                    # cancel_row ile simetri: stock_reversed=0 + COMPLETED ise
                    # sentinel pattern ile tüm grup rollback olur.
                    from apps.bracelets.services.cancel_bracelet_service import (
                        cancel_bracelet_purchase,
                        CancelBraceletError,
                    )
                    try:
                        _br_res = cancel_bracelet_purchase(
                            process_id=p.id, user=request.user,
                        )
                    except CancelBraceletError as _br_err:
                        _stock_failure_msg = (
                            f"Toplu iptal: bilezik stok geri sarılamadı "
                            f"(process_no={p.process_no or p.id}) — {_br_err}"
                        )
                        transaction.set_rollback(True)
                        break
                    _br_stock = int(_br_res.get('stock_reversed') or 0)
                    if (_br_stock == 0
                            and (p.is_status or '').upper() == 'COMPLETED'
                            and Decimal(str(p.gram or 0)) > 0
                            and not bool(getattr(p, 'waiting_stock', False))):
                        _stock_failure_msg = (
                            f"Toplu iptal: bilezik stok geri sarılamadı "
                            f"(process_no={p.process_no or p.id}). "
                            "StockLedger eşleşmesi yok."
                        )
                        transaction.set_rollback(True)
                        break
                else:
                    # FAZ 52 (A-3): cancel_group'ta da stok başarısı zorunlu —
                    # cancel_row ile aynı sertleştirme. Bir satır başarısız
                    # olursa transaction.set_rollback(True) + sentinel ile
                    # tüm grup atomic blok bittiğinde rollback olur. Eski
                    # davranışta kısmi başarı oluşabiliyordu.
                    _stock_res = _revert_process_stock(p, request.user)
                    if not _stock_res.get('success', True):
                        _stock_failure_msg = (
                            f"Toplu iptal: stok geri sarılamadı "
                            f"(process_no={p.process_no or p.id}, "
                            f"reason={_stock_res.get('reason')}"
                            + (
                                f", error={_stock_res.get('error')}"
                                if _stock_res.get('error') else ''
                            )
                            + "). Toplu iptal iptal edildi; lütfen ilgili "
                            "satırı kontrol edin."
                        )
                        transaction.set_rollback(True)
                        break  # iç for loop — sonraki satırları işleme
                    _mark_process_canceled(p)

        # 2. Cari Satırlarını Kapat (Hepsini Pasife Çek)
        # FAZ 52: stok başarısızlığı varsa SL/CL/Payment'ı atla — set_rollback(True)
        # zaten tüm değişiklikleri rollback edecek; aşağıdaki bloklar yan etki
        # yaratmasın.
        if not _stock_failure_msg and ledger_rows:
            # Eğer OB işlemiyse güvenlik kontrolü yap (Yarım kalmış OB iptal edilmemeli)
            if pn.startswith("OB"):
                can, reason = _can_cancel_ob_group(pn)
                if not can:
                    raise Exception(reason)  # Transaction rollback tetikler

            # FAZ 52 (S-04): Eski `update(is_active=False)` mass mutation
            # FAZ 51 (R-02) audit-shadow REVERSAL pattern ile değiştirildi.
            # `reverse_supplier_ledger_for_process` orijinali pasifleştirir,
            # REVERSAL audit satırı yazar (is_active=False → bakiye davranışı
            # değişmez, sadece denetim izi eklenir). cancel_row ile simetri
            # kurulmuş oldu.
            from apps.suppliers.services import (
                reverse_supplier_ledger_for_process,
            )
            _grp_audit = extract_audit_context(request)
            _grp_reason = f'Toplu iptal — {pn}'
            _sl_res = reverse_supplier_ledger_for_process(
                audit=_grp_audit, reason=_grp_reason, process_no=pn,
            )
            logger.info(
                "cancel_group: process_no=%s → SupplierLedger %d satır REVERSAL "
                "yazıldı (FAZ 51 audit-shadow).",
                pn, _sl_res.get('reversed_count', 0),
            )

        # R-FAZ 4 + FAZ 35: CustomerLedger iptali — REVERSAL pattern.
        # Eski mass `is_active=False` mutation FAZ 35'te kaldırıldı; her aktif
        # satır için LedgerService.reverse_entry() çağrılır (Kasa/P&L sync,
        # audit trail). Aktör yetkisiz büyük tutarlar PENDING kalır → cari
        # ekranından onaylanır.
        # FAZ 52: stok başarısızlığı varsa CL/Payment bloklarını atla.
        if not _stock_failure_msg:
            _cancel_audit = extract_audit_context(request)
            _cancel_reason = f'Toplu iptal — {pn}'
            _cl_qs = CustomerLedger.objects.filter(
                process_no=pn, is_active=True,
            ).exclude(transaction_type=CustomerLedger.REVERSAL)
            _rev_count = 0
            _skip_count = 0
            for _cl_entry in _cl_qs:
                try:
                    LedgerService.reverse_entry(
                        original=_cl_entry,
                        audit=_cancel_audit,
                        reason=_cancel_reason,
                    )
                    _rev_count += 1
                except InvalidLedgerStateError as _exc:
                    _skip_count += 1
                    logger.warning(
                        "cancel_group: CustomerLedger #%s reverse_entry atlandı: %s",
                        _cl_entry.pk, _exc,
                    )
            if _rev_count or _skip_count:
                logger.info(
                    "cancel_group: process_no=%s → %d CustomerLedger REVERSAL yazıldı (%d atlandı).",
                    pn, _rev_count, _skip_count,
                )

            # --- FAZ 15: Payment Rollback ---
            # Bu Process No'ya ait tüm Payment kayıtlarını iptal et.
            _cancelled_payment_count = Payment.objects.filter(
                process_no=pn,
                is_cancelled=False,
            ).update(
                is_cancelled=True,
                cancelled_at=timezone.now(),
            )
            if _cancelled_payment_count > 0:
                logger.info(
                    "cancel_group: process_no=%s → %d Payment kaydı iptal edildi.",
                    pn, _cancelled_payment_count,
                )

            # FAZ 52 (S-03): Kasa İptali Karşı Girişi (cancel_row ile simetri).
            try:
                _reverse_cashbox_for_process(
                    process_no=pn,
                    audit=_cancel_audit,
                )
            except Exception as _cb_err:
                logger.error(
                    "cancel_group: CashboxLedger reversal failed "
                    "(process_no=%s): %s",
                    pn, _cb_err,
                )

    # FAZ 52 (A-5): Atomic blok bittikten sonra stok hata sentinel'ini
    # kontrol et. set_rollback(True) ile değişiklikler geri alındı; kullanıcıya
    # 409 Conflict ile açık mesaj dön.
    if _stock_failure_msg:
        logger.warning("cancel_group: stok başarısızlığı → rollback (msg=%s)", _stock_failure_msg)
        return JsonResponse({
            "result": False,
            "error_msg": _stock_failure_msg,
            "stock_reversal_failed": True,
        }, status=409)

    return JsonResponse({"result": True, "count": len(rows) + len(ledger_rows)})


# Yardımcı fonksiyonlar
def _proc_datetime(p): return p.date


def _product_name(p):
    if p.product:
        return p.product.name
    # FAZ 23: Kasa kalemi — ürün yok, kasa adını göster
    ba = getattr(p, 'bank_account', None)
    if ba:
        pay_cur = getattr(p, 'payment_currency', '') or ''
        ba_name = getattr(ba, 'name', '') or 'Kasa'
        if pay_cur:
            return f"💰 {ba_name} ({pay_cur})"
        return f"💰 {ba_name}"
    return "-"


_PROCESS_TYPE_TR = {
    "RETAIL": "Perakende",
    "WHOLESALE": "Toptan",
    "FAST_PROCESS": "Hızlı İşlem",
}


def _proc_kind_tr(p: Process) -> str:
    code = (getattr(p, "process_type", None) or "").upper()
    return _PROCESS_TYPE_TR.get(code, code or "-")


def _proc_no(p: Process) -> str:
    return getattr(p, "process_no", None) or f"PRC-{getattr(p, 'id', '')}"


def _collect_payments_for_store(store: Stores, start: datetime | None, end: datetime | None) -> Tuple[Decimal, Decimal]:
    p_qs = Process.objects.filter(store=store)
    if start:
        p_qs = p_qs.filter(date__gte=start)
    if end:
        p_qs = p_qs.filter(date__lt=end)
    proc_nos = list(
        p_qs.exclude(process_no__isnull=True).exclude(process_no__exact="").values_list("process_no", flat=True)
    )
    if not proc_nos:
        return (Decimal("0.00"), Decimal("0.00"))
    # FAZ 15: İptal edilmiş ödemeleri kapsam dışında tut
    pay_qs = Payment.objects.filter(process_no__in=proc_nos, is_cancelled=False)
    if start:
        pay_qs = pay_qs.filter(date__gte=start)
    if end:
        pay_qs = pay_qs.filter(date__lt=end)
    cash_in = Decimal("0.00")
    cash_out = Decimal("0.00")
    for pm in pay_qs:
        amt = Decimal(str(getattr(pm, "amount", 0) or 0))
        if getattr(pm, "is_output", False):
            cash_out += amt
        else:
            cash_in += amt
    return (_q2(cash_in), _q2(cash_out))
