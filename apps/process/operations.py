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
from apps.customers.models import CustomerLedger
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

def _revert_process_stock(p: Process, user) -> None:
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

    Hangi (ref_type, ref_id) çiftiyle aranır?
        - HURDA PURCHASE (perakende, R-Faz 7 sonrası): ref_type='process',
          ref_id=str(p.id) — complete_process update_product_stock ile yazıyor.
        - HURDA PURCHASE (legacy, R-Faz 7 öncesi): ref_type='scrap_add',
          ref_id=str(p.id) — add_scrap_to_process anında yazılmıştı.
        - HURDA SALE & diğer perakende SATIŞ/ALIŞ: ref_type='process',
          ref_id=str(p.id) (R-Faz 5'te update_product_stock per-line yazıyor)
        - Bilezik PURCHASE/SALE: aynı 'process' + str(p.id).
        - waiting_stock=True olan satırlar stoğa girmediği için sadece
          incoming_stock alanına dokunulduysa cancel_stock_entry boş geçer.
    """
    if not p.product or not p.store:
        return

    if p.waiting_stock:
        # Bekleyen sipariş — gerçek stok hareketi yoktu, reversal'a gerek yok.
        return

    tx_type = (p.transaction_type or "").upper()
    is_scrap_product = bool(getattr(p.product, 'is_scrap', False))

    # R-FAZ 7 — ref_type fallback:
    # Yeni (post-R-Faz 7) tüm satırlar 'process' ile yazılır. Geriye dönük
    # uyum için hurda PURCHASE'da önce 'process', bulunamazsa 'scrap_add'
    # denenir. Bu sayede deploy öncesi sepete eklenmiş ama deploy sonrası
    # iptal edilen taslak hurda satırları da temizlenir.
    if is_scrap_product and tx_type == 'PURCHASE':
        _ref_type_candidates = ('process', 'scrap_add')
    else:
        _ref_type_candidates = ('process',)

    _ref_id = str(p.id)
    _cancel_hit = False

    for _ref_type in _ref_type_candidates:
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
            logger.error(
                "_revert_process_stock cancel_stock_entry failed "
                "(process_id=%s, ref_type=%s, ref_id=%s): %s",
                p.id, _ref_type, _ref_id, _err,
            )
            return
        if isinstance(_result, dict) and _result.get('cancelled_stock_count', 0) > 0:
            _cancel_hit = True
            break

    if not _cancel_hit:
        # Hiçbir ref_type'la eşleşme bulunamadı — taslak (cart) iptali olabilir
        # ya da daha önce iptal edilmiş bir satır. Audit için info düzeyinde log.
        logger.info(
            "_revert_process_stock: ledger eşleşmesi yok (process_id=%s, "
            "tx=%s, is_scrap=%s) — taslak iptali veya tekrar-iptal olabilir.",
            p.id, tx_type, is_scrap_product,
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


def _mark_process_canceled(p: Process) -> None:
    """Process tablosundaki kaydı 'İPTAL' olarak işaretler."""
    if hasattr(p, "is_status"):
        p.is_status = "CANCELED"
    if hasattr(p, "is_deleted"):
        p.is_deleted = True
    p.save(update_fields=["is_status", "is_deleted"])


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
            "amount_tl": float(_q2(amount_val)),
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


import logging

logger = logging.getLogger(__name__)


@login_required
@require_POST
def cancel_row(request):
    """
    Güçlendirilmiş İptal Fonksiyonu.
    İşlem bütünlüğünü korumak için, seçilen satırın ait olduğu grubun tamamını siler.
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

                    # R-FAZ 5: Hard-delete yerine soft-delete — denetim izi korunur.
                    if hasattr(c_rec, 'is_deleted'):
                        c_rec.is_deleted = True
                        _save_fields = ['is_deleted']
                        if hasattr(c_rec, 'is_active'):
                            c_rec.is_active = False
                            _save_fields.append('is_active')
                        c_rec.save(update_fields=_save_fields)
                    else:
                        # Modelde soft-delete alanı yoksa (legacy) hard-delete'e düş
                        c_rec.delete()
                # --------------------------------------------------
                # Stok ve Process durumu güncelleme
                _revert_process_stock(p, request.user)
                _mark_process_canceled(p)

                # Eğer bu ürün işlemine bağlı bir cari kaydı varsa onu da sil
                if p.process_no:
                    SupplierLedger.objects.filter(process_no=p.process_no, is_active=True).update(is_active=False)

                    # R-FAZ 4: CustomerLedger pasifleştirme.
                    # CustomerLedger satırı checkout başına TEK kez yazılır (aggregate
                    # balance_diff). Tek satır iptalinde grupta hâlâ aktif Process
                    # kalıyorsa müşteri bakiyesi olduğu gibi tutulur — kalan satıra
                    # ilişkin ödeme farkı geçerli. Tüm grup iptal olduğunda
                    # (son satır da temizlendiyse) CustomerLedger pasife çekilir.
                    _remaining_active = Process.objects.filter(
                        process_no=p.process_no,
                    ).exclude(
                        Q(is_deleted=True) | Q(is_status='CANCELED') | Q(transaction_type='CANCELED'),
                    ).exclude(id=p.id).exists()
                    if not _remaining_active:
                        _cl_count = CustomerLedger.objects.filter(
                            process_no=p.process_no, is_active=True,
                        ).update(is_active=False)
                        if _cl_count:
                            logger.info(
                                "cancel_row PROC: process_no=%s → %d CustomerLedger satırı pasifleştirildi.",
                                p.process_no, _cl_count,
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
                    # R-FAZ 5: Soft-delete (model destekliyorsa); aksi halde hard-delete.
                    if hasattr(c_rec, 'is_deleted'):
                        c_rec.is_deleted = True
                        _save_fields = ['is_deleted']
                        if hasattr(c_rec, 'is_active'):
                            c_rec.is_active = False
                            _save_fields.append('is_active')
                        c_rec.save(update_fields=_save_fields)
                    else:
                        c_rec.delete()
                # --------------------------------------------------
                _revert_process_stock(p, request.user)  # Stok düzeltme
                _mark_process_canceled(p)  # Status güncelleme

        # 2. Cari Satırlarını Kapat (Hepsini Pasife Çek)
        if ledger_rows:
            # Eğer OB işlemiyse güvenlik kontrolü yap (Yarım kalmış OB iptal edilmemeli)
            if pn.startswith("OB"):
                can, reason = _can_cancel_ob_group(pn)
                if not can:
                    raise Exception(reason)  # Transaction rollback tetikler

            # Bu Process No'ya ait tüm aktif cari kayıtlarını pasife çekiyoruz
            SupplierLedger.objects.filter(process_no=pn, is_active=True).update(is_active=False)

        # R-FAZ 4: CustomerLedger pasifleştirme.
        # Toplu iptalde process_no eşleşen tüm aktif müşteri carisi satırları
        # is_active=False yapılır (SupplierLedger ile aynı desen).
        _cl_count = CustomerLedger.objects.filter(
            process_no=pn, is_active=True,
        ).update(is_active=False)
        if _cl_count:
            logger.info(
                "cancel_group: process_no=%s → %d CustomerLedger satırı pasifleştirildi.",
                pn, _cl_count,
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
