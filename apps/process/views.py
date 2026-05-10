import os
import random
import logging
from collections import defaultdict
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional
from typing import Any, Dict, List, Tuple
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Max, Q, Sum, OuterRef, Subquery, F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from decimal import Decimal
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from apps.process.models import Process, Payment
from apps.custody.models import CustomerCustodyLedger
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf.default import DEFAULT_FONT

# --- FAZ 3: Eski Inventories import'lari kaldirildi, StockService eklendi ---
from apps.stock_management.services.stock_service import StockService, InsufficientStockError
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService

# Eski import: from apps.inventories.models import Inventories, InventoryMovement  # KALDIRILDI
from apps.process.models import Process, Payment
from apps.products.models import Products
from apps.roles.decorators import role_required
from apps.whatsapp.services import wa_preflight, send_whatsapp_template_guarded

logger = logging.getLogger(__name__)

# --- FONT VE CACHE AYARLARI ---
try:
    cache.delete("wa_tpl_schema:islem_ozeti_kisa_v2")
except Exception:
    pass

try:
    font_path = os.path.join(settings.BASE_DIR, "static", "management", "fonts", "DejaVuSansCustom.ttf")
    font_name = "DejaVuSansCustom"
    if os.path.exists(font_path):
        pdfmetrics.registerFont(TTFont(font_name, font_path))
        DEFAULT_FONT["helvetica"] = font_name
        DEFAULT_FONT["Times-Roman"] = font_name
except Exception:
    pass


# --- YARDIMCI FONKSİYONLAR ---

def generate_process_no():
    return 'P' + ''.join(random.choices('0123456789', k=10))


def _fmt_tl(val):
    if val is None: return "0,00"
    return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_hs(val):
    if val is None: return "0,000"
    return f"{val:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_local(dt, fmt="%d.%m.%Y %H:%M"):
    """Naive/aware fark etmeksizin yerel saat diliminde güvenli formatlar."""
    if not dt:
        return ""
    try:
        tz = timezone.get_default_timezone()
        if getattr(settings, "USE_TZ", False):
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, tz)
            dt = timezone.localtime(dt, tz)
        else:
            if getattr(dt, "tzinfo", None):
                dt = timezone.localtime(dt, tz)
        return dt.strftime(fmt)
    except Exception:
        try:
            return dt.strftime(fmt)
        except Exception:
            return str(dt)


def _q2(x: Any) -> Decimal:
    return Decimal(str(x or "0")).quantize(Decimal("0.01"))


def _q3(x: Any) -> Decimal:
    return Decimal(str(x or "0")).quantize(Decimal("0.001"))


def _product_name(p: Process) -> str:
    prod = getattr(p, "product", None)
    return (getattr(prod, "name", None) or "Ürün") if prod else "Ürün"


def _qty_display(p: Process) -> str:
    gram = Decimal(str(getattr(p, "gram", 0) or 0))
    piece = Decimal(str(getattr(p, "piece", 0) or 0))

    if gram > 0:
        if gram % 1 == 0:
            return f"{int(gram)} gr"
        return f"{_q2(gram)} gr"

    return f"{int(piece)} Ad"


# ==============================================================================
# FAZ 3: update_product_stock() -> StockService tabanlı yeni implementasyon
# ==============================================================================

def update_product_stock(product, transaction_type, quantity_pieces, quantity_weight, waiting_stock, user=None,
                         description=None, process_no=None, unit_cost_hs=Decimal('0.000'),
                         process_id=None):
    """
    FAZ 3 REFACTORED: Artik dogrudan Inventories tablosuna yazmak yerine
    StockService.record_entry() / record_exit() kullanir.

    Eski tabloya (Inventories, InventoryMovement) ARTIK YAZILMAZ.
    Yeni tablolar: StockSnapshot (anlik stok), StockLedger (hareket logu).

    Bu fonksiyon geriye donuk uyumluluk icin vardir. Tum view'lar
    hala bu fonksiyonu cagirmaktadir.

    Parametreler (eski imza korundu):
        product: Products instance
        transaction_type: 'ENTRY' | 'EXIT'
        quantity_pieces: Adet (int veya Decimal)
        quantity_weight: Gram (Decimal)
        waiting_stock: Siparis/Bekleyen stok flag'i (True ise incoming_stock)
        user: Islemi yapan kullanici
        description: Aciklama notu
        process_no: Islem numarasi (grup id; geriye donuk uyumluluk)
        unit_cost_hs: Birim Has maliyeti (WAC hesabi icin)
        process_id: R-FAZ 5 — Process satirinin UUID'si. Verilirse StockLedger
                    ref_id olarak bu kullanilir (per-line iz). Boylece
                    cancel_stock_entry(ref_type='process', ref_id=str(p.id))
                    yalnizca o satirin hareketini reverse edebilir. Verilmezse
                    ref_id=process_no (grup paylasimli) eski davranisa duser.
    """
    store = user.store

    # Bekleyen (siparis) stok girisi icin ayrı handle
    # NOT: Siparis stok mantigi eski Inventories.incoming_stock_* icindi.
    # Yeni sistemde siparis stogunu StockSnapshot uzerindeki incoming alanlarina yaziyoruz.
    if waiting_stock:
        snapshot, created = StockSnapshot.objects.get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': Decimal('0.0000'),
                'stock_pieces': 0,
                'weighted_avg_cost_hs': Decimal('0.0000'),
                'weighted_avg_cost_eur': Decimal('0.00'),
            }
        )
        if hasattr(snapshot, 'incoming_stock_pieces'):
            snapshot.incoming_stock_pieces = (snapshot.incoming_stock_pieces or 0) + int(quantity_pieces or 0)
        if hasattr(snapshot, 'incoming_stock_gram'):
            snapshot.incoming_stock_gram = (snapshot.incoming_stock_gram or Decimal('0')) + Decimal(str(quantity_weight or 0))
        snapshot.save()
        return

    # Has Altin TL kuru (islem anindaki kur)
    hs_rate_eur = Decimal('0.0000')
    try:
        hs_data = PriceService.get_price('GOLD_24K')
        if transaction_type == 'ENTRY':
            hs_rate_eur = hs_data.get('buy_tl', Decimal('0'))
        else:
            hs_rate_eur = hs_data.get('sell_tl', Decimal('0'))

        # Fallback: PriceService bos donerse eski Products tablosundan oku
        if hs_rate_eur <= 0:
            hs_prod = Products.objects.filter(name__icontains='Has Altın').only('sale_price_eur', 'buy_price_eur').first()
            if hs_prod:
                if transaction_type == 'ENTRY':
                    hs_rate_eur = Decimal(str(hs_prod.buy_price_eur or 0))
                else:
                    hs_rate_eur = Decimal(str(hs_prod.sale_price_eur or 0))
    except Exception:
        pass

    # TL maliyet hesapla
    unit_cost_eur = Decimal('0.00')
    if hs_rate_eur > 0 and unit_cost_hs > 0:
        unit_cost_eur = (Decimal(str(unit_cost_hs)) * hs_rate_eur).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # Reason mapping: description'dan en uygun StockLedger.Reason'i sec
    reason_map_entry = {
        'Hızlı işlem': StockLedger.Reason.PURCHASE,
        'Toptan işlem': StockLedger.Reason.PURCHASE,
        'Perakende': StockLedger.Reason.PURCHASE,
        'Perakende Nakit': StockLedger.Reason.PURCHASE,
        'Nakit (Hızlı)': StockLedger.Reason.PURCHASE,
        'Manuel Stok Düzenleme': StockLedger.Reason.ADJUSTMENT_PLUS,
    }
    reason_map_exit = {
        'Hızlı işlem': StockLedger.Reason.SALE,
        'Toptan işlem': StockLedger.Reason.SALE,
        'Perakende': StockLedger.Reason.SALE,
        'Perakende Nakit': StockLedger.Reason.SALE,
        'Nakit (Hızlı)': StockLedger.Reason.SALE,
        'Manuel Stok Düzenleme': StockLedger.Reason.ADJUSTMENT_MINUS,
    }

    desc_key = (description or '').strip()

    # R-FAZ 5: Per-line ref_id desteği — process_id verilmişse onu kullan,
    # aksi halde geriye dönük uyumluluk için process_no ref_id olur.
    _ledger_ref_id = str(process_id) if process_id else str(process_no or '')

    if transaction_type == 'ENTRY':
        reason = reason_map_entry.get(desc_key, StockLedger.Reason.PURCHASE)

        StockService.record_entry(
            product=product,
            store=store,
            quantity_gram=Decimal(str(quantity_weight or 0)),
            quantity_pieces=int(quantity_pieces or 0),
            reason=reason,
            ref_type='process',
            ref_id=_ledger_ref_id,
            unit_cost_hs=Decimal(str(unit_cost_hs or 0)),
            unit_cost_eur=unit_cost_eur,
            hs_rate_eur=hs_rate_eur,
            user=user,
            notes=description or f"ENTRY islemi",
        )

    elif transaction_type == 'EXIT':
        reason = reason_map_exit.get(desc_key, StockLedger.Reason.SALE)

        try:
            StockService.record_exit(
                product=product,
                store=store,
                quantity_gram=Decimal(str(quantity_weight or 0)),
                quantity_pieces=int(quantity_pieces or 0),
                reason=reason,
                ref_type='process',
                ref_id=_ledger_ref_id,
                hs_rate_eur=hs_rate_eur,
                user=user,
                notes=description or f"EXIT islemi",
            )
        except InsufficientStockError as exc:
            # FAZ 48 (2026-05-03): Teşhis bilgisi ile zenginleştirilmiş hata mesajı.
            # Eski mesaj sadece ürün adını gösteriyordu → kullanıcı "stoğa ekledim ama
            # düşmüyor" durumunda hangi senaryonun (snapshot yok / pieces yetersiz /
            # gram yetersiz) tetiklendiğini göremiyordu. InsufficientStockError zaten
            # available/requested/deficit alanlarını taşıyor; bunları görüntüye taşıyoruz.
            _avail = getattr(exc, 'available', None)
            _req = getattr(exc, 'requested', None)
            _unit = getattr(exc, 'unit', '')
            _deficit = getattr(exc, 'deficit', None)
            _detail = ""
            if _avail is not None and _req is not None:
                _detail = (
                    f"<br>Mevcut: {_avail}{_unit}"
                    f"<br>Talep: {_req}{_unit}"
                )
                if _deficit is not None:
                    _detail += f"<br>Eksik: {_deficit}{_unit}"
            raise ValidationError(
                "Yetersiz stok!<br>"
                f"Ürün adı: {product.name}"
                f"{_detail}"
            )
    else:
        raise ValueError(f"Geçersiz işlem türü: {transaction_type}")


def calc_process_summary(process_no):
    procs = Process.objects.filter(process_no=process_no, is_deleted=False)
    sales_tl = sum(p.amount for p in procs if p.transaction_type in ['SALE', 'ORDER_IN'])
    purchases_tl = sum(p.amount for p in procs if p.transaction_type in ['PURCHASE', 'RETURN'])
    sales_hs = sum(p.price_hs for p in procs if p.transaction_type in ['SALE', 'ORDER_IN'])
    purchases_hs = sum(p.price_hs for p in procs if p.transaction_type in ['PURCHASE', 'RETURN'])
    net_total_tl = sales_tl - purchases_tl
    net_total_hs = sales_hs - purchases_hs
    payments = Payment.objects.filter(process_no=process_no)
    in_buckets = {'CASH': Decimal(0), 'CREDIT_CARD': Decimal(0), 'TRANSFER': Decimal(0)}
    out_buckets = {'CASH': Decimal(0), 'CREDIT_CARD': Decimal(0), 'TRANSFER': Decimal(0)}
    paid_in = Decimal(0)
    paid_out = Decimal(0)

    for p in payments:
        if p.payment_type == 'COMMISSION':
            continue

        if p.is_output:
            paid_out += p.amount
            if p.payment_type in out_buckets:
                out_buckets[p.payment_type] += p.amount
        else:
            paid_in += p.amount
            if p.payment_type in in_buckets:
                in_buckets[p.payment_type] += p.amount

    net_paid = paid_in - paid_out

    balance_eur = net_total_tl - net_paid

    balance_hs = Decimal('0')
    if net_total_tl != 0:
        balance_hs = (balance_eur / net_total_tl) * net_total_hs

    def fmt_bucket(source):
        return {
            'cash': _fmt_tl(source['CASH']) if source['CASH'] > 0 else None,
            'credit_card': _fmt_tl(source['CREDIT_CARD']) if source['CREDIT_CARD'] > 0 else None,
            'transfer': _fmt_tl(source['TRANSFER']) if source['TRANSFER'] > 0 else None,
        }

    return {
        "total_sales": _fmt_tl(sales_tl),
        "total_purchases": _fmt_tl(purchases_tl),
        "net_total": _fmt_tl(net_total_tl),
        "net_total_raw": net_total_tl,
        "paid_total": _fmt_tl(net_paid),
        "paid_in": _fmt_tl(paid_in),
        "paid_out": _fmt_tl(paid_out),
        "by_type_in": fmt_bucket(in_buckets),
        "by_type_out": fmt_bucket(out_buckets),
        "balance": _fmt_tl(abs(balance_eur)),
        "balance_eur": _fmt_tl(abs(balance_eur)),
        "balance_eur_raw": balance_eur,
        "balance_raw": balance_eur,
        "net_hs": _fmt_hs(net_total_hs),
        "balance_hs": _fmt_hs(abs(balance_hs))
    }


@login_required(login_url='login')
@role_required('PROCESS_PROCESS_INDEX')
def process_index(request):
    return render(request, 'management/process/index.html', {
        'title': 'İşlem Özetleri'
    })


@login_required(login_url="login")
def get_all(request):
    from django.urls import reverse
    from django.db.models import Max, Q
    from datetime import datetime, date
    from decimal import Decimal

    try:
        from apps.invoices.models import Invoice
    except Exception:
        Invoice = None

    def _normalize_http_url(u: str) -> str:
        s = (u or '').strip()
        if not s:
            return ''
        if s.startswith('http://') or s.startswith('https://'):
            return s
        return 'https://' + s.lstrip('/')

    draw = int(request.GET.get("draw", "1"))
    length = int(request.GET.get("length", "25"))
    start = int(request.GET.get("start", "0"))

    search_value = request.GET.get("search[value]", "").strip()
    order_idx = request.GET.get("order[0][column]", None)
    order_dir = request.GET.get("order[0][dir]", "desc")
    order_col = request.GET.get(f"columns[{order_idx}][data]", "") if order_idx is not None else ""

    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    customer_filter = request.GET.get("customer_filter", "all")
    employee_filter = request.GET.get("employee_filter", "").strip()
    transaction_type_filter = request.GET.get("transaction_type", "all")
    process_kind = request.GET.get("process_kind", "all").strip().lower()
    # FAZ: Yeni filtreler — Ürün tipi ve Ayar (milyem)
    product_kind = request.GET.get("product_kind", "all").strip().lower()
    mileage_filter = request.GET.get("mileage", "").strip()

    user_store_id = request.user.store_id

    base_queryset = Process.objects.filter(
        is_deleted=False,
        store_id=user_store_id,
        is_status="COMPLETED"
    )

    if process_kind != "all":
        kind_map = {"fast": "FAST_PROCESS", "retail": "RETAIL", "wholesale": "WHOLESALE"}
        pt = kind_map.get(process_kind)
        if pt:
            base_queryset = base_queryset.filter(process_type=pt)

    # FAZ: Ürün tipi filtresi
    if product_kind == "barcoded":
        base_queryset = base_queryset.filter(product__category__name__icontains='Barkodlu')
    elif product_kind == "gram":
        base_queryset = base_queryset.filter(product__is_gram_bullion=True)
    elif product_kind == "scrap":
        base_queryset = base_queryset.filter(product__is_scrap=True)
    elif product_kind == "bracelet":
        base_queryset = base_queryset.filter(product__category__name__icontains='Bilezik')

    # FAZ: Ayar (milyem) filtresi
    if mileage_filter:
        try:
            base_queryset = base_queryset.filter(product__product_mileage=Decimal(mileage_filter))
        except (InvalidOperation, TypeError, ValueError):
            pass

    if employee_filter:
        base_queryset = base_queryset.filter(employee_id=employee_filter)

    if date_from:
        try:
            df = datetime.strptime(date_from, "%d/%m/%Y").date()
            base_queryset = base_queryset.filter(date__date__gte=df)
        except Exception:
            pass
    if date_to:
        try:
            dt_ = datetime.strptime(date_to, "%d/%m/%Y").date()
            base_queryset = base_queryset.filter(date__date__lte=dt_)
        except Exception:
            pass

    if transaction_type_filter != "all":
        base_queryset = base_queryset.filter(transaction_type=transaction_type_filter)

    if customer_filter == "with_customer":
        base_queryset = base_queryset.exclude(customer__isnull=True)
    elif customer_filter == "without_customer":
        base_queryset = base_queryset.filter(customer__isnull=True)

    if search_value:
        base_queryset = base_queryset.filter(
            Q(process_no__icontains=search_value) |
            Q(customer__first_name__icontains=search_value) |
            Q(customer__last_name__icontains=search_value)
        )

    grouped = base_queryset.values("process_no").annotate(last_date=Max("date"))

    sort_field = "last_date"
    if order_col == "process_no":
        sort_field = "process_no"
    if order_dir == "desc":
        sort_field = f"-{sort_field}"
    grouped = grouped.order_by(sort_field)

    total_count = grouped.count()

    page_rows = list(grouped if length == -1 else grouped[start:start + length])
    process_nos = [r["process_no"] for r in page_rows]

    # --- INVOICE LOOKUP GÜNCELLEMESİ ---
    invoices_by_pno = {}
    if Invoice and process_nos:
        inv_qs = (Invoice.objects
                  .filter(store_id=user_store_id, is_deleted=False, process__process_no__in=process_nos)
                  .order_by("process__process_no", "-id")
                  .values("id", "invoice_no", "process__process_no", "pavo_sale_data", "invoice_type"))
        for r in inv_qs:
            pno = r.get("process__process_no")
            if pno and pno not in invoices_by_pno:
                invoices_by_pno[pno] = {
                    "id": r.get("id"),
                    "invoice_no": r.get("invoice_no") or "",
                    "pavo_sale_data": r.get("pavo_sale_data") or {},
                    "invoice_type": r.get("invoice_type")
                }

    ENTRY_SET = {"PURCHASE", "STOCK_IN", "RETURN", "ORDER_IN"}
    EXIT_SET = {"SALE"}

    all_procs = list(
        base_queryset.filter(process_no__in=process_nos)
        .select_related("employee", "customer")
        .order_by("-date")
    )
    procs_by_pno = defaultdict(list)
    for _proc in all_procs:
        procs_by_pno[_proc.process_no].append(_proc)

    all_pays = list(Payment.objects.filter(process_no__in=process_nos))
    pays_by_pno = defaultdict(list)
    for _pay in all_pays:
        pays_by_pno[_pay.process_no].append(_pay)

    data = []

    for pno in process_nos:
        procs = procs_by_pno[pno]
        first_proc = procs[0] if procs else None

        pays = pays_by_pno[pno]
        tl_purchase = sum((Decimal(p.amount or 0) for p in pays if getattr(p, "is_output", False)), Decimal("0"))
        tl_sale = sum((Decimal(p.amount or 0) for p in pays if not getattr(p, "is_output", False)), Decimal("0"))

        if (tl_purchase == 0 and tl_sale == 0) and procs:
            tl_sale = sum((Decimal(p.amount or 0) for p in procs if p.transaction_type in EXIT_SET), Decimal("0"))
            tl_purchase = sum((Decimal(p.amount or 0) for p in procs if p.transaction_type in ENTRY_SET), Decimal("0"))

        total_tl = (tl_sale - tl_purchase)

        net_hs = Decimal("0")
        for p in procs:
            phs = Decimal(str(getattr(p, "price_hs", 0) or 0))
            if p.transaction_type in ENTRY_SET:
                net_hs += phs
            elif p.transaction_type in EXIT_SET:
                net_hs -= phs

        hs_rate_sale = None
        hs_rate_buy = None
        for p in procs:
            if hs_rate_sale is None and p.transaction_type in EXIT_SET:
                hs_rate_sale = getattr(p, "hs_rate_sale_eur", None)
            if hs_rate_buy is None and p.transaction_type in ENTRY_SET:
                hs_rate_buy = getattr(p, "hs_rate_buy_eur", None)
            if hs_rate_sale is not None and hs_rate_buy is not None:
                break

        if total_tl > 0:
            tx_type = "SALE"
        elif total_tl < 0:
            tx_type = "PURCHASE"
        else:
            tx_type = (first_proc.transaction_type if first_proc else "")

        emp_name = "-"
        if first_proc and first_proc.employee:
            emp_name = f"{first_proc.employee.first_name or ''} {first_proc.employee.last_name or ''}".strip() or "-"

        cust_name = "-"
        if first_proc and first_proc.customer:
            fn = first_proc.customer.first_name or ""
            ln = first_proc.customer.last_name or ""
            cn = (fn + " " + ln).strip()
            cust_name = cn if cn else "-"

        invoice_url = ""
        invoice_no = ""
        invoice_id = None

        inv_row = invoices_by_pno.get(pno)
        if inv_row and inv_row.get("id"):
            invoice_no = inv_row.get("invoice_no") or str(inv_row.get("id"))
            invoice_id = str(inv_row.get("id"))

            try:
                sd = inv_row.get("pavo_sale_data") or {}
                inq = ""
                if isinstance(sd, dict):
                    inq = sd.get("SaleInquieryLink") or sd.get("SaleInquiryLink") or ""
                inq = _normalize_http_url(str(inq))
                if inq:
                    invoice_url = inq
                else:
                    invoice_url = reverse("invoices:detail", args=[str(inv_row.get("id"))])
            except Exception:
                invoice_url = f"/invoices/detail/{inv_row.get('id')}/"

        data.append({
            "process_no": pno,
            "product_name": _product_name(p),
            "quantity_display": _qty_display(p),
            "employee": emp_name,
            "customer": cust_name,
            "date": (first_proc.date if first_proc else None),

            "price_hs": net_hs,
            "hs_rate_sale_eur": hs_rate_sale,
            "hs_rate_buy_eur": hs_rate_buy,

            "purchase_amount": tl_purchase,
            "sale_amount": tl_sale,
            "total_amount": total_tl,

            "transaction_type": tx_type,

            "invoice_url": invoice_url,
            "invoice_no": invoice_no,
            "invoice_id": invoice_id,
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total_count,
        "recordsFiltered": total_count,
        "data": data
    })


@login_required(login_url='login')
@role_required('PROCESS_DELETE')
def delete(request):
    """
    Process kaydı iptali (2026-04-21 güvenli sürüm).

    Audit trail korunur — HARD DELETE YAPILMAZ:
      - StockLedger & StockSnapshot silinmez (tarih izleri korunur).
      - Process.is_status='CANCELED', is_deleted=True yapılır.
      - SupplierLedger aynı process_no ile ilişkiliyse pasife çekilir.
      - Hurda: StockService.record_exit(reason=RETURN_OUT) ile havuzdan gram düşer.
      - Bilezik: Aynı şekilde havuzdan gram düşer.
      - Her iki durumda, havuz snapshot gramı 0'a inerse ilgili
        Bracelets/Scraps satırı otomatik soft-delete edilir
        (listede stok=0 ama satır kalma bug'ı buradan kaynaklanıyordu).
    """
    if request.method == 'POST':
        # Geç importlar (dairesel import'tan kaçın)
        from apps.suppliers.models import SupplierLedger
        from apps.bracelets.models import Bracelets
        from apps.scraps.models import Scraps
        from django.db import transaction as _txn

        ids = request.POST.getlist('ids[]')
        try:
            with _txn.atomic():
                records = Process.objects.filter(id__in=ids).select_related('product__category', 'store')

                affected_stores = set()

                for record in records:
                    product = record.product
                    store = record.store
                    if store:
                        affected_stores.add(store.id)

                    cat_name = ''
                    if product and product.category:
                        cat_name = (product.category.name or '').lower()

                    is_hurda = 'hurda' in cat_name
                    is_bilezik = 'bilezik' in cat_name

                    # --- Ortak: Stok ters hareketi (PURCHASE iptali) ---
                    if (is_hurda or is_bilezik) and product and record.gram and record.gram > 0:
                        if not record.waiting_stock:
                            snap = StockSnapshot.objects.filter(
                                product=product, store=store
                            ).first()
                            u_hs = snap.weighted_avg_cost_hs if snap else Decimal('0')
                            u_tl = snap.weighted_avg_cost_eur if snap else Decimal('0')
                            try:
                                StockService.record_exit(
                                    product=product,
                                    store=store,
                                    quantity_gram=record.gram,
                                    quantity_pieces=int(record.piece or 0),
                                    reason=StockLedger.Reason.RETURN_OUT,
                                    ref_type='process_cancel',
                                    ref_id=str(record.id),
                                    unit_cost_hs=u_hs, unit_cost_eur=u_tl,
                                    user=request.user,
                                    notes=f"İşlem iptali (Process.delete): {record.process_no}",
                                )
                            except InsufficientStockError:
                                pass

                            # Legacy Products.gram azalt (metadata)
                            try:
                                Products.objects.filter(id=product.id).update(
                                    gram=F('gram') - record.gram
                                )
                            except Exception:
                                pass

                    # --- SupplierLedger pasifleştirme (tedarikçi cari borç sıfırla) ---
                    if record.process_no:
                        SupplierLedger.objects.filter(
                            process_no=record.process_no, is_active=True
                        ).update(is_active=False)

                    # --- Process soft-delete (hard delete YOK) ---
                    record.is_status = 'CANCELED'
                    record.is_deleted = True
                    record.save(update_fields=['is_status', 'is_deleted'])

                    # --- Havuz boşsa ilgili Bracelets/Scraps satırını gizle ---
                    # FAZ 65: Barkodlu (tekil) GoldPurchases urunleri "havuz" degildir
                    # — gram-tabanli aggregate stok degil, piece-tabanli tekil urundur.
                    # Pool cleanup mantigi yalnizca hurda/bilezik POOL urunleri icin
                    # tasarlanmistir. Restore edilmis barkodlu urunlerde StockSnapshot
                    # eksik oldugunda pool_empty=True yanlis tetiklenip
                    # Products.is_active=False ile urun veri kaybi olusturuyordu.
                    _is_barcoded = bool(getattr(product, 'barcode', None)) if product else False
                    if product and store and not _is_barcoded:
                        snap_after = StockSnapshot.objects.filter(
                            product=product, store=store
                        ).first()
                        pool_empty = (not snap_after) or (snap_after.stock_gram is None) or \
                                     (snap_after.stock_gram <= Decimal('0'))

                        # Kalan aktif PURCHASE Process var mı?
                        has_other_active = Process.objects.filter(
                            product=product, store=store,
                            transaction_type='PURCHASE', is_deleted=False,
                        ).exclude(is_status='CANCELED').exists()

                        if pool_empty and not has_other_active:
                            # Products'u pasife çek (soft)
                            Products.objects.filter(id=product.id).update(is_active=False)

                            if is_bilezik:
                                Bracelets.objects.filter(
                                    product=product, store=store, is_deleted=False
                                ).update(is_deleted=True, is_active=False)
                            elif is_hurda:
                                Scraps.objects.filter(
                                    product=product, store=store, is_deleted=False
                                ).update(is_deleted=True, is_active=False)

                # Dashboard cache invalidation
                try:
                    for sid in affected_stores:
                        cache.delete(f"dashboard_assets_summary:{sid}")
                except Exception:
                    pass

            return JsonResponse({'result': True})

        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})

    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
def process_detail_page(request, process_no: str):
    _ENTRY_SET = {"PURCHASE", "STOCK_IN", "RETURN", "ORDER_IN"}
    _PROC_TYPE_LABELS = {
        'RETAIL': 'Perakende', 'WHOLESALE': 'Toptan', 'FAST_PROCESS': 'Hızlı İşlem'
    }
    _STATUS_MAP = {
        'COMPLETED':    ('bg-light-success text-success', 'TAMAMLANDI'),
        'PENDING':      ('bg-light-warning text-warning', 'BEKLİYOR'),
        'IN_PROGRESS':  ('bg-light-info text-info',       'AKTİF'),
        'OPEN_BINDING': ('bg-light-primary text-primary', 'AÇIK BAĞLAMA'),
        'WAITING_STOCK':('bg-light-warning text-warning', 'STOK BEKLİYOR'),
        'CANCELED':     ('bg-light-danger text-danger',   'İPTAL'),
    }

    qs = (Process.objects
          .filter(process_no=process_no, is_deleted=False)
          .select_related("product", "product__category", "employee", "customer", "store")
          .order_by("date"))

    if not qs.exists():
        return HttpResponse("İşlem bulunamadı.", status=404)

    first = qs.first()
    customer = first.customer
    store = first.store

    # --- 1. ZENGİN İŞLEM KALEMLERİ ---
    items = []
    total_profit_hs = Decimal('0')
    any_legacy = False
    for p in qs:
        payload = _build_xray_item_payload(p, _ENTRY_SET)
        items.append(payload)
        gp = payload['financials'].get('gross_profit_hs')
        if gp is not None:
            total_profit_hs += Decimal(str(gp))
        if payload['data_quality'].get('is_legacy'):
            any_legacy = True

    # --- 2. ÖDEME DETAYLARI ---
    pays = Payment.objects.filter(process_no=process_no).order_by("date")
    _PAY_META = {
        'CASH':        {'label': 'Nakit',       'icon': 'fa-solid fa-money-bill-wave', 'color': 'success'},
        'CREDIT_CARD': {'label': 'Kredi Kartı', 'icon': 'fa-regular fa-credit-card',  'color': 'info'},
        'TRANSFER':    {'label': 'Havale / EFT','icon': 'fa-solid fa-building-columns','color': 'warning'},
    }
    payment_list = []
    for pay in pays:
        pm = _PAY_META.get(pay.payment_type, {'label': 'Diğer', 'icon': 'fa-solid fa-coins', 'color': 'secondary'})
        payment_list.append({
            "type_label": pm['label'],
            "icon": pm['icon'],
            "color": pm['color'],
            "date": pay.date.strftime("%d.%m.%Y %H:%M"),
            "amount_raw": float(pay.amount),
            "amount": _fmt_tl(pay.amount),
            "is_refund": pay.is_output,
            "installment": pay.installment if pay.installment > 1 else None,
        })

    # --- 3. EMANET DURUMU ---
    custody_info = None
    custody_ledger = CustomerCustodyLedger.objects.filter(process_no=process_no).first()
    if custody_ledger:
        custody_info = {
            "type": custody_ledger.get_custody_type_display(),
            "amount_hs": _fmt_hs(custody_ledger.amount_hs),
            "desc": custody_ledger.description,
        }

    # --- 4. ÖZET VE BAKİYE ---
    summary = calc_process_summary(process_no)
    raw_bal = summary['balance_raw']
    if raw_bal > Decimal('0.01'):
        balance_state = {'cls': 'bg-light-danger text-danger', 'text': 'Borçlu', 'icon': 'fa-solid fa-circle-exclamation'}
    elif raw_bal < Decimal('-0.01'):
        balance_state = {'cls': 'bg-light-warning text-warning', 'text': 'Alacaklı', 'icon': 'fa-solid fa-circle-info'}
    else:
        balance_state = {'cls': 'bg-light-success text-success', 'text': 'Kapandı', 'icon': 'fa-solid fa-circle-check'}

    # --- 5. DURUM VE META ---
    status_code = first.is_status or 'IN_PROGRESS'
    is_canceled = bool(getattr(first, 'is_cancelled', False))
    if is_canceled:
        status_cls, status_text = 'bg-light-danger text-danger', 'İPTAL EDİLDİ'
    else:
        status_cls, status_text = _STATUS_MAP.get(status_code, ('bg-light text-secondary', status_code))

    profit_cls = 'text-success' if total_profit_hs >= Decimal('0') else 'text-danger'

    # --- 6. WhatsApp paylaşım için imzalı token ---
    public_token = make_public_process_token(process_no)
    customer_phone_clean = ''
    if customer and getattr(customer, 'phone', None):
        digits = ''.join(ch for ch in str(customer.phone) if ch.isdigit())
        if digits:
            if digits.startswith('90'):
                customer_phone_clean = digits
            elif digits.startswith('0'):
                customer_phone_clean = '90' + digits[1:]
            else:
                customer_phone_clean = '90' + digits

    context = {
        "process_no": process_no,
        "date": first.date.strftime("%d.%m.%Y %H:%M"),
        "employee_name": f"{first.employee.first_name} {first.employee.last_name}".strip() if first.employee else "—",
        "employee_username": getattr(first.employee, 'username', '') or '',
        "customer": customer,
        "store_name": getattr(store, 'name', '—') or '—',
        "process_type": first.process_type or 'RETAIL',
        "process_type_label": _PROC_TYPE_LABELS.get(first.process_type, first.process_type or '—'),
        "status_cls": status_cls,
        "status_text": status_text,
        "is_canceled": is_canceled,
        "items": items,
        "payments": payment_list,
        "has_partial_payment": len(payment_list) > 1,
        "summary": summary,
        "balance_state": balance_state,
        "custody_info": custody_info,
        "any_legacy": any_legacy,
        "total_profit_hs": _fmt_hs(total_profit_hs) if items else None,
        "total_profit_hs_raw": float(total_profit_hs),
        "profit_cls": profit_cls,
        "public_token": public_token,
        "customer_phone_clean": customer_phone_clean,
        "title": f"İşlem Detayı — {process_no}",
    }

    return render(request, 'management/process/detail.html', context)


# ═══ FAZ 40.3 — Public (Login'siz) İşlem Özeti ════════════════════════════════
# Müşteri WhatsApp ile gelen linkten herhangi bir kimlik doğrulama olmadan
# işlemi görüntüleyebilir. Token kriptografik imzalıdır (signing.dumps), tahmin
# edilemez. Maliyet/kâr/WAC/personel iç verileri MÜŞTERİYE GÖSTERİLMEZ.
# ───────────────────────────────────────────────────────────────────────────────

PUBLIC_PROCESS_TOKEN_SALT = 'public-process-detail-v1'
PUBLIC_PROCESS_TOKEN_MAX_AGE = 60 * 60 * 24 * 365  # 1 yıl

# ─────────────────────────────────────────────────────────────────────────────
# FAZ 68.1 — URL-Safe Token Separator
#   Django'nun varsayılan `signing.dumps()` çıktısı 'data:timestamp:signature'
#   formatında olup ':' karakteri içerir. Bu karakter URL path segment'inde
#   teknik olarak izinli olsa da:
#     - WhatsApp URL butonu native render'ında ':' scheme separator olarak
#       yorumlanıp token'ı truncate edebilir,
#     - Bazı Nginx/CDN/proxy konfigürasyonları ':' içeren path'leri reddeder,
#     - URL-encode (%3A) edilirse Django routing eşleşse de signature
#       doğrulaması bozulur.
#   Çözüm: Separator olarak '.' kullanılır (URL-safe, base64url alfabesinde
#   bulunmaz, signing.Signer tarafından kabul edilir).
#
#   Geriye Uyumluluk: ':' içeren eski token'lar 1 yıl geçerli kalmaya devam
#   eder. verify_public_process_token() önce yeni format ('.'), başarısızsa
#   eski format (':') ile dener — kademeli geçiş sağlanır.
# ─────────────────────────────────────────────────────────────────────────────
PUBLIC_PROCESS_TOKEN_SEP = '.'  # URL-safe separator


def _build_public_process_signer():
    """
    URL-safe separator ('.') ile TimestampSigner örneği döndürür.
    Lazy import — modül yüklenirken settings hazır olmasa da güvenli.
    """
    return signing.TimestampSigner(salt=PUBLIC_PROCESS_TOKEN_SALT, sep=PUBLIC_PROCESS_TOKEN_SEP)


def make_public_process_token(process_no: str) -> str:
    """
    Process numarasından kriptografik imzalı (geri-açılabilir) token üretir.
    URL-safe separator ('.') kullanır → WhatsApp URL butonunda ve tüm web
    sunucularında sorunsuz çalışır.
    """
    signer = _build_public_process_signer()
    return signer.sign_object({'pn': str(process_no)}, compress=True)


def verify_public_process_token(token: str):
    """
    Token'ı doğrular, geçerliyse process_no döndürür, aksi halde None.

    Iki separator destekler (kademeli geçiş için):
      1) Yeni format: '.' separator (FAZ 68.1+)
      2) Eski format: ':' separator (FAZ 40.3 default)
    """
    if not token:
        return None

    # 1) Yeni format ('.') ile dene
    try:
        signer = _build_public_process_signer()
        data = signer.unsign_object(token, max_age=PUBLIC_PROCESS_TOKEN_MAX_AGE)
        if isinstance(data, dict):
            pn = data.get('pn')
            if pn:
                return pn
    except (signing.BadSignature, signing.SignatureExpired):
        pass
    except Exception:
        pass

    # 2) Geriye uyum — eski format (':') ile dene
    try:
        data = signing.loads(token, salt=PUBLIC_PROCESS_TOKEN_SALT,
                             max_age=PUBLIC_PROCESS_TOKEN_MAX_AGE)
        if isinstance(data, dict):
            return data.get('pn')
    except signing.BadSignature:
        return None
    except Exception:
        return None

    return None


def _build_public_item_payload(p, ENTRY_SET):
    """
    Müşteri-yönlü payload — iç finansal veriler (WAC, maliyet, kâr) HARİÇ.
    """
    is_entry = (getattr(p, 'transaction_type', '') or '') in ENTRY_SET
    prod = getattr(p, 'product', None)

    image_url = ''
    karat_label = ''
    milyem = 0
    jewelry_type = ''
    name = 'Nakit / Diğer'
    barcode = ''

    if prod is not None:
        try:
            img = getattr(prod, 'image', None)
            if img:
                image_url = img.url
        except Exception:
            image_url = ''
        try:
            milyem = int(float(getattr(prod, 'product_mileage', 0) or 0))
        except Exception:
            milyem = 0
        karat_label = _karat_label_from_mileage(milyem)
        jewelry_type = _safe_str(getattr(prod, 'jewelry_type', ''), '')
        name = _safe_str(getattr(prod, 'name', None), 'Ürün')
        barcode = _safe_str(getattr(prod, 'barcode', ''), '')

    # Miktar
    try:
        gram_val = Decimal(str(getattr(p, 'gram', 0) or 0))
    except Exception:
        gram_val = Decimal('0')
    try:
        piece_val = int(getattr(p, 'piece', 0) or 0)
    except Exception:
        piece_val = 0

    if piece_val > 0 and gram_val <= Decimal('0'):
        qty_display = f"{piece_val} adet"
    elif gram_val > Decimal('0') and piece_val == 0:
        qty_display = f"{gram_val:.3f}".replace('.', ',') + " gr"
    elif piece_val > 0 and gram_val > Decimal('0'):
        qty_display = f"{piece_val} adet × {gram_val:.3f}".replace('.', ',') + " gr"
    else:
        qty_display = '—'

    try:
        tx_label = p.get_transaction_type_display()
    except Exception:
        tx_label = getattr(p, 'transaction_type', '') or '—'

    return {
        'name': name,
        'image_url': image_url,
        'karat_label': karat_label,
        'milyem': milyem,
        'jewelry_type': jewelry_type,
        'barcode': barcode,
        'qty_display': qty_display,
        'gram': float(gram_val),
        'piece': piece_val,
        'transaction_label': tx_label,
        'is_entry': is_entry,
        'unit_price': float(getattr(p, 'unit_price', 0) or 0),
        'amount_eur': float(getattr(p, 'amount', 0) or 0),
        'amount_hs': float(getattr(p, 'price_hs', 0) or 0),
    }


def public_process_detail(request, token: str):
    """
    Login GEREKMEZ. WhatsApp linki üzerinden gelen müşteri görüntüler.
    """
    process_no = verify_public_process_token(token)
    if not process_no:
        return render(request, 'management/process/public_detail.html', {
            'invalid_token': True,
            'title': 'Geçersiz Bağlantı',
        }, status=404)

    _ENTRY_SET = {"PURCHASE", "STOCK_IN", "RETURN", "ORDER_IN"}

    qs = (Process.objects
          .filter(process_no=process_no, is_deleted=False)
          .select_related('product', 'customer', 'store')
          .order_by('date'))

    if not qs.exists():
        return render(request, 'management/process/public_detail.html', {
            'invalid_token': True,
            'title': 'İşlem Bulunamadı',
        }, status=404)

    first = qs.first()
    customer = first.customer
    store = first.store

    items = [_build_public_item_payload(p, _ENTRY_SET) for p in qs]

    # Ödemeler — sadece müşteri-yönlü görünüm
    pays = Payment.objects.filter(process_no=process_no).order_by('date')
    _PAY_LABEL = {'CASH': 'Nakit', 'CREDIT_CARD': 'Kredi Kartı', 'TRANSFER': 'Havale / EFT'}
    payments_pub = []
    for pay in pays:
        payments_pub.append({
            'label': _PAY_LABEL.get(pay.payment_type, 'Diğer'),
            'date': pay.date.strftime('%d.%m.%Y %H:%M'),
            'amount': _fmt_tl(pay.amount),
            'amount_raw': float(pay.amount),
            'is_refund': pay.is_output,
            'installment': pay.installment if pay.installment > 1 else None,
        })

    summary = calc_process_summary(process_no)
    raw_bal = summary['balance_raw']
    if raw_bal > Decimal('0.01'):
        bal_state = {'cls': 'pub-bal-debt', 'text': 'Kalan Borç', 'icon': 'fa-exclamation-circle'}
    elif raw_bal < Decimal('-0.01'):
        bal_state = {'cls': 'pub-bal-credit', 'text': 'Alacaklı (Fazla Ödeme)', 'icon': 'fa-info-circle'}
    else:
        bal_state = {'cls': 'pub-bal-paid', 'text': 'Tamamen Ödendi', 'icon': 'fa-circle-check'}

    # Mağaza branding
    store_logo = ''
    try:
        if store and getattr(store, 'avatar', None):
            store_logo = store.avatar.url
    except Exception:
        store_logo = ''

    store_info = {
        'name': (getattr(store, 'title', None) or getattr(store, 'name', None) or '—') if store else '—',
        'phone': getattr(store, 'phone', '') or '' if store else '',
        'address': getattr(store, 'address', '') or '' if store else '',
        'city': getattr(store, 'city', '') or '' if store else '',
        'logo_url': store_logo,
    }

    is_canceled = bool(getattr(first, 'is_cancelled', False))

    context = {
        'invalid_token': False,
        'process_no': process_no,
        'date': first.date.strftime('%d.%m.%Y %H:%M'),
        'customer_name': (f"{customer.first_name} {customer.last_name}".strip()
                          if customer else 'Müşteri'),
        'store': store_info,
        'items': items,
        'payments': payments_pub,
        'summary': summary,
        'balance_state': bal_state,
        'is_canceled': is_canceled,
        'title': f'İşlem Özeti — {process_no}',
    }
    return render(request, 'management/process/public_detail.html', context)


@login_required(login_url="login")
@role_required("PROCESS_PROCESS_INDEX")
def process_receipt_view(request, process_no: str):
    rows = (Process.objects
            .filter(process_no=process_no, is_deleted=False)
            .select_related("product", "employee", "customer", "store")
            .order_by("date"))
    if not rows.exists():
        return HttpResponse(status=404)

    first = rows.first()
    last = rows.last()

    total_sale = rows.filter(transaction_type="SALE").aggregate(t=Sum("amount"))["t"] or Decimal("0")
    total_buy = rows.filter(transaction_type__in=["PURCHASE", "RETURN"]).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    total_net = total_sale - total_buy

    pays = Payment.objects.filter(process_no=process_no).exclude(payment_type="COMMISSION")
    paid_in = pays.filter(is_output=False).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    paid_out = pays.filter(is_output=True).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    paid_net = paid_in - paid_out
    balance = total_net - paid_net

    def parts(d):
        out = []
        if d.get('cash', 0) > 0:        out.append(f"Nakit {_fmt_tl(d['cash'])}")
        if d.get('credit_card', 0) > 0: out.append(f"Kredi Kartı {_fmt_tl(d['credit_card'])}")
        if d.get('transfer', 0) > 0:    out.append(f"Havale {_fmt_tl(d['transfer'])}")
        return " · ".join(out)

    summ = calc_process_summary(process_no)
    pay_breakdown_in = parts({
        'cash': Decimal(summ['by_type_in']['cash'].replace('.', '').replace(',', '.')) if summ['by_type_in'][
            'cash'] else Decimal('0'),
        'credit_card': Decimal(summ['by_type_in']['credit_card'].replace('.', '').replace(',', '.')) if
        summ['by_type_in']['credit_card'] else Decimal('0'),
        'transfer': Decimal(summ['by_type_in']['transfer'].replace('.', '').replace(',', '.')) if summ['by_type_in'][
            'transfer'] else Decimal('0'),
    })
    pay_breakdown_out = parts({
        'cash': Decimal(summ['by_type_out']['cash'].replace('.', '').replace(',', '.')) if summ['by_type_out'][
            'cash'] else Decimal('0'),
        'credit_card': Decimal(summ['by_type_out']['credit_card'].replace('.', '').replace(',', '.')) if
        summ['by_type_out']['credit_card'] else Decimal('0'),
        'transfer': Decimal(summ['by_type_out']['transfer'].replace('.', '').replace(',', '.')) if summ['by_type_out'][
            'transfer'] else Decimal('0'),
    })

    items = []
    for r in rows:
        qty_str = f"{r.gram} gr" if r.gram else (f"{r.piece} adet" if r.piece else "")
        items.append({
            "name": (r.product.name if r.product else "-"),
            "type": r.get_transaction_type_display(),
            "qty": qty_str,
            "amount": _fmt_tl(r.amount),
            "date": fmt_local(r.date),
        })

    store = first.store
    store_meta = {
        "name": getattr(store, "name", None),
        "id": getattr(store, "id", None),
        "code": getattr(store, "code", None),
        "phone": getattr(store, "phone", None),
        "address": getattr(store, "address", None)
    }

    paper = "58" if request.GET.get("paper") == "58" else "80"
    auto_print = (request.GET.get("print") in ["1", "true", "yes"])

    ctx = {
        "paper": paper,
        "auto_print": auto_print,
        "process_no": process_no,
        "store_meta": store_meta,
        "employee_name": (f"{first.employee.first_name} {first.employee.last_name}" if first.employee else "-"),
        "customer_name": (
            f"{first.customer.first_name} {first.customer.last_name}" if first.customer else "-") if first.customer else "-",
        "customer_phone": getattr(first.customer, "phone", "-") if first.customer else "-",
        "header": {"created": fmt_local(first.date), "updated": fmt_local(last.date)},
        "items": items,
        "totals": {
            "sale": _fmt_tl(total_sale), "buy": _fmt_tl(total_buy),
            "net": _fmt_tl(total_net), "paid_net": _fmt_tl(paid_net),
            "balance": _fmt_tl(abs(balance)),
            "balance_sign": (1 if balance > 0 else (-1 if balance < 0 else 0)),
        },
        "pay_breakdown_in": pay_breakdown_in,
        "pay_breakdown_out": pay_breakdown_out,
        "badge": summ and {
            "kind": ("Kalan borcunuz" if summ["balance_eur_raw"] > 0 else "Alacağınız" if summ[
                                                                                             "balance_eur_raw"] < 0 else "İşlem"),
            "value": (summ["balance_eur"] + " TL" if summ["balance_eur_raw"] != 0 else "Tamamlandı")
        }
    }
    return render(request, "management/process/receipt_thermal.html", ctx)


@login_required(login_url='login')
def process_detail_view(request, process_no):
    """
    FAZ 3 REFACTORED: Maliyet bilgisi artik StockSnapshot.weighted_avg_cost_hs
    uzerinden dinamik cekilir. Eski Inventories Subquery kaldirildi.
    """
    processes = Process.objects.filter(
        process_no=process_no,
        is_deleted=False
    ).select_related(
        'customer', 'product', 'product__category', 'employee'
    ).order_by('date')

    if not processes.exists():
        return render(request, '404.html', {'message': 'İşlem bulunamadı.'})

    first_proc = processes.first()
    customer = first_proc.customer
    customer_name = f"{customer.first_name} {customer.last_name}" if customer else "PEŞİN MÜŞTERİ"
    process_date = first_proc.date

    total_process_gram = Decimal('0')
    total_process_amount = Decimal('0')
    total_process_piece = Decimal('0')
    items = []

    ENTRY_SET = ['PURCHASE', 'STOCK_IN', 'RETURN', 'ORDER_IN']

    # FAZ 3: Tum snapshot'lari tek sorguda cekip map'e al
    product_ids = [p.product_id for p in processes if p.product_id]
    store = first_proc.store
    snapshot_map = {}
    if product_ids:
        for snap in StockSnapshot.objects.filter(product_id__in=product_ids, store=store):
            snapshot_map[snap.product_id] = snap

    for p in processes:
        is_entry = p.transaction_type in ENTRY_SET

        if p.product and p.product.category:
            category_name = p.product.category.name
        else:
            category_name = "Kategorisiz"

        if p.employee:
            employee_name = f"{p.employee.first_name} {p.employee.last_name}".strip() or p.employee.username
        else:
            employee_name = "Sistem/Bilinmiyor"

        # FAZ 3: Maliyet artik StockSnapshot'tan (WAC) alinir
        snap = snapshot_map.get(p.product_id) if p.product_id else None
        birim_maliyet_hasi = (snap.weighted_avg_cost_hs if snap else Decimal('0.000'))

        satis_hasi_toplam = p.price_hs or Decimal('0.000')

        # FAZ 41/42 — WAC HS/gram birimindedir (StockService SSOT).
        #
        # FAZ 42: Barkodlu parça ürün (Process.gram=0, piece=1) fallback'i —
        # retail_views.py:1102'de parça satışlarda gram sıfırlanıyor; WAC
        # gram başına saklı olduğundan ürünün fiziksel ağırlığıyla çarp.
        # Mantık:
        #   1) Process.gram > 0           → gram_val
        #   2) Process.gram=0, piece>0,
        #      Products.gram > 0          → product.gram (barkodlu parça)
        #   3) gram=0, product.gram=0     → piece_val (WATCH/DIAMOND)
        _gram_dec = Decimal(str(p.gram or 0))
        _piece_dec = Decimal(str(p.piece or 0))
        _product_gram_dec = Decimal('0')
        if p.product is not None:
            try:
                _product_gram_dec = Decimal(str(p.product.gram or 0))
            except (InvalidOperation, TypeError, ValueError):
                _product_gram_dec = Decimal('0')

        if _gram_dec > Decimal('0'):
            qty_val = _gram_dec
        elif _piece_dec > Decimal('0') and _product_gram_dec > Decimal('0'):
            qty_val = _product_gram_dec
        else:
            qty_val = _piece_dec

        toplam_maliyet_hasi = (birim_maliyet_hasi * qty_val).quantize(Decimal('0.000'), rounding=ROUND_HALF_UP)

        if p.transaction_type not in ENTRY_SET:
            kar_hasi = satis_hasi_toplam - toplam_maliyet_hasi
        else:
            kar_hasi = Decimal('0.000')

        if p.product:
            product_name = p.product.name
            is_gram = getattr(p.product, 'is_gram_bullion', False)
            # FAZ 41 — qty_val (WAC × miktar hesabı için) yukarıda gram>0 →
            # gram, aksi halde piece olarak ayarlandı; burada override
            # etmiyoruz. WAC HS/gram olduğundan gram tabanlı çarpım gerekli.
            # Aşağıdaki `quantity_str` zaten 1015-1019 bloğuyla yeniden
            # kuruluyor — bu satır artık placeholder.
            quantity_str = f"{p.gram} gr" if is_gram else f"{p.piece} adet"

            if is_gram or (p.gram and p.gram > 0 and not p.piece):
                gram_formatli = f"{Decimal(str(p.gram or 0)):.2f}".replace('.', ',')
                quantity_str = f"{gram_formatli} gr"
            else:
                quantity_str = f"{int(p.piece or 0)} adet"

            currency = p.product.currency or "TL"
            category_name = p.product.category.name if p.product.category else "Kategorisiz"

            # --- FAZ 3: ANLIK (CANLI) FİYAT PriceService'DEN ---
            try:
                hs_price_data = PriceService.get_price('GOLD_24K')
                hs_buy_tl = hs_price_data.get('buy_tl', Decimal('0'))
                hs_sell_tl = hs_price_data.get('sell_tl', Decimal('0'))
            except Exception:
                hs_buy_tl = Decimal(str(getattr(p.product, 'buy_price_eur', 0) or 0))
                hs_sell_tl = Decimal(str(getattr(p.product, 'sale_price_eur', 0) or 0))

            # Anlik maliyet = WAC_HS * guncel_kur * miktar
            anlik_maliyet_tl = birim_maliyet_hasi * hs_buy_tl * Decimal(str(qty_val or 0))
            anlik_satis_tl = (p.product.sale_price_hs or Decimal('0')) * hs_sell_tl * Decimal(str(qty_val or 0))

            if not is_entry:
                anlik_kar_tl = (p.amount or Decimal('0')) - anlik_maliyet_tl
            else:
                anlik_kar_tl = anlik_satis_tl - (p.amount or Decimal('0'))

        else:
            product_name = "Nakit İşlem"
            quantity_str = f"{p.amount} TL/Döviz"
            currency = "TL"
            category_name = "-"
            anlik_maliyet_tl = Decimal('0.00')
            anlik_satis_tl = Decimal('0.00')
            anlik_kar_tl = Decimal('0.00')

        satis_hasi = p.price_hs or Decimal('0.000')
        kar_tl = p.gross_profit or Decimal('0.00')

        kur = p.hs_rate_sale_eur if (p.hs_rate_sale_eur and p.hs_rate_sale_eur > 0) else Decimal('1')

        # ── Ürün detay zenginleştirmesi (görsel, takı tipi, barkod, milyem) ──
        product_image_url = ''
        product_jewelry_type = ''
        product_barcode = ''
        product_milyem_int = 0
        product_gold_rate_text = ''
        product_ring_size = ''
        if p.product:
            try:
                if p.product.image:
                    product_image_url = p.product.image.url
            except Exception:
                product_image_url = ''
            product_jewelry_type = (p.product.jewelry_type or '').strip()
            product_barcode = (p.product.barcode or '').strip()
            try:
                product_milyem_int = int(float(p.product.product_mileage or 0))
            except (ValueError, TypeError):
                product_milyem_int = 0
            # Ayar metni (585 → 14 Ayar vb.)
            _m = product_milyem_int
            if _m >= 990:   product_gold_rate_text = '24 Ayar'
            elif _m >= 900: product_gold_rate_text = '22 Ayar'
            elif _m >= 720: product_gold_rate_text = '18 Ayar'
            elif _m >= 580: product_gold_rate_text = '14 Ayar'
            elif _m >= 410: product_gold_rate_text = '10 Ayar'
            elif _m >= 320: product_gold_rate_text = '8 Ayar'
            else:           product_gold_rate_text = f'{_m} M' if _m else ''
            product_ring_size = (getattr(p.product, 'ring_size', '') or '').strip()

        items.append({
            'transaction_name': p.get_transaction_type_display(),
            'is_entry': is_entry,
            'product_name': product_name,
            'quantity_str': quantity_str,
            'mileage': p.product.product_mileage if p.product and p.product.product_mileage else "0.00",
            'unit_price': p.unit_price or Decimal('0.00'),
            'currency': currency,
            'amount': p.amount or Decimal('0.00'),
            'satis_hasi': satis_hasi,
            'maliyet_hasi': toplam_maliyet_hasi,
            'kar_hasi': kar_hasi,
            'category_name': category_name,
            'employee_name': employee_name,
            'anlik_maliyet_tl': anlik_maliyet_tl,
            'anlik_satis_tl': anlik_satis_tl,
            'anlik_kar_tl': anlik_kar_tl,
            # Zenginleştirilmiş ürün meta verisi
            'product_image_url': product_image_url,
            'product_jewelry_type': product_jewelry_type,
            'product_barcode': product_barcode,
            'product_gold_rate_text': product_gold_rate_text,
            'product_milyem': product_milyem_int,
            'product_ring_size': product_ring_size,
        })

        total_process_gram += p.gram
        total_process_piece += p.piece
        total_process_amount += p.amount

    context = {
        'process_no': process_no,
        'customer_name': customer_name,
        'process_date': process_date,
        'total_gram': total_process_gram,
        'total_piece': total_process_piece,
        'total_amount': total_process_amount,
        'items': items,
    }

    return render(request, 'management/process/process-detail.html', context)


# ══════════════════════════════════════════════════════════════════════════════
# FAZ 40 — İŞLEM DETAY MODAL JSON ENDPOINT (Lazy-Load)
# ──────────────────────────────────────────────────────────────────────────────
# İşlemler tablosundaki process_no badge'ine tıklandığında çağrılır.
# Tarihsel WAC waterfall: StockLedger.unit_cost_hs → Process.cost_amount_hs → null.
# ══════════════════════════════════════════════════════════════════════════════

def _karat_label_from_mileage(milyem) -> str:
    try:
        m = int(float(milyem or 0))
    except (ValueError, TypeError):
        m = 0
    if m >= 990:
        return '24 Ayar'
    if m >= 900:
        return '22 Ayar'
    if m >= 720:
        return '18 Ayar'
    if m >= 580:
        return '14 Ayar'
    if m >= 410:
        return '10 Ayar'
    if m >= 320:
        return '8 Ayar'
    return f'{m} M' if m else ''


def _resolve_historical_wac_hs(proc) -> Tuple[Optional[Decimal], str]:
    """
    Tarihsel WAC waterfall.

    Returns: (unit_cost_hs, source)
      source ∈ {LEDGER, LEDGER_LEGACY_NORMALIZED, PROCESS_COST, UNAVAILABLE}

    FAZ 44 — 1.05 EŞİK KURALI (SSOT):
      Has altın için unit_cost_hs (HAS/gram) saf altın fraksiyonudur (≤ 1.000).
      1.05 üzerinde okunan değer kesinlikle FAZ 34 öncesi "toplam maliyetin
      birim alana yanlış yazılmış" legacy verisidir → render zamanı normalize.
    """
    # 1) StockLedger SSOT — işlem anında mühürlenmiş birim maliyet
    try:
        if proc.product_id:
            led = (StockLedger.objects
                   .filter(ref_type='process',
                           ref_id=str(proc.id),
                           product_id=proc.product_id)
                   .order_by('-created_on')
                   .first())
            if led and led.unit_cost_hs and Decimal(str(led.unit_cost_hs)) > Decimal('0'):
                wac = Decimal(str(led.unit_cost_hs))
                # FAZ 44 — Legacy total tespiti: WAC > 1.05 imkansız (saf altın fraksiyonu).
                # Bu durumda legacy "toplam maliyetin birim alana yazılmış hali" olduğunu
                # varsayıp gram ile normalize et.
                # FAZ 65.1 — Barkodlu parça satışında retail_views.py:1102 Process.gram=0
                # yazıyor (sepet "1 adet" semantiği). proc.gram=0 ise Products.gram
                # fiziksel ağırlığı fallback olarak kullanılır. Aksi halde restore edilmiş
                # legacy ürünlerde normalizasyon devreye girmeyip 15× şişen maliyet üretir.
                gram_proc = Decimal(str(proc.gram or 0))
                if gram_proc <= Decimal('0'):
                    piece_proc = int(getattr(proc, 'piece', 0) or 0)
                    prod_gram = Decimal('0')
                    try:
                        if proc.product_id and getattr(proc, 'product', None) is not None:
                            prod_gram = Decimal(str(getattr(proc.product, 'gram', 0) or 0))
                    except Exception:
                        prod_gram = Decimal('0')
                    if piece_proc > 0 and prod_gram > Decimal('0'):
                        gram_proc = prod_gram

                if wac > Decimal('1.05') and gram_proc > Decimal('0'):
                    return (
                        (wac / gram_proc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP),
                        'LEDGER_LEGACY_NORMALIZED',
                    )
                return wac, 'LEDGER'
    except Exception:
        pass

    # 2) Process.cost_amount_hs / gram → birim maliyet
    try:
        cost_total = Decimal(str(proc.cost_amount_hs or 0))
        gram = Decimal(str(proc.gram or 0))
        if cost_total > Decimal('0') and gram > Decimal('0'):
            return (cost_total / gram).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP), 'PROCESS_COST'
    except Exception:
        pass

    return None, 'UNAVAILABLE'


def _safe_str(v, default='—'):
    try:
        s = str(v) if v is not None else ''
        return s.strip() or default
    except Exception:
        return default


def _build_xray_item_payload(p, ENTRY_SET):
    """Tek bir Process satırı için payload üretir — her erişim defensive."""
    is_entry = (getattr(p, 'transaction_type', '') or '') in ENTRY_SET

    # ─── Ürün ───
    product_payload = None
    prod = getattr(p, 'product', None)
    if prod is not None:
        image_url = ''
        try:
            img = getattr(prod, 'image', None)
            if img:
                image_url = img.url
        except Exception:
            image_url = ''

        milyem_int = 0
        try:
            milyem_int = int(float(getattr(prod, 'product_mileage', 0) or 0))
        except (ValueError, TypeError):
            milyem_int = 0

        category_name = 'Kategorisiz'
        try:
            cat = getattr(prod, 'category', None)
            if cat is not None and getattr(cat, 'name', None):
                category_name = cat.name
        except Exception:
            pass

        product_payload = {
            'id': _safe_str(getattr(prod, 'id', None), '—'),
            'name': _safe_str(getattr(prod, 'name', None), '—'),
            'category': category_name,
            'karat_label': _karat_label_from_mileage(milyem_int),
            'milyem': milyem_int,
            'image_url': image_url,
            'jewelry_type': _safe_str(getattr(prod, 'jewelry_type', ''), ''),
            'barcode': _safe_str(getattr(prod, 'barcode', ''), ''),
            'is_currency': bool(getattr(prod, 'is_currency', False)),
        }

    # ─── Personel ───
    employee_payload = None
    emp = getattr(p, 'employee', None)
    if emp is not None:
        try:
            full_name = f"{getattr(emp, 'first_name', '') or ''} {getattr(emp, 'last_name', '') or ''}".strip()
            if not full_name:
                full_name = getattr(emp, 'username', None) or '—'
            employee_payload = {
                'id': getattr(emp, 'id', None),
                'full_name': full_name,
                'username': getattr(emp, 'username', '') or '',
            }
        except Exception:
            employee_payload = None

    # ─── Miktar ───
    try:
        gram_val = Decimal(str(getattr(p, 'gram', 0) or 0))
    except Exception:
        gram_val = Decimal('0')
    try:
        piece_val = int(getattr(p, 'piece', 0) or 0)
    except Exception:
        piece_val = 0

    if piece_val > 0 and gram_val <= Decimal('0'):
        qty_display = f"{piece_val} adet"
    elif gram_val > Decimal('0') and piece_val == 0:
        qty_display = f"{gram_val:.3f} gr".replace('.', ',')
    elif piece_val > 0 and gram_val > Decimal('0'):
        qty_display = f"{piece_val} adet × {gram_val:.3f} gr".replace('.', ',')
    else:
        cur_safe = (getattr(prod, 'currency', None) if prod is not None else None) or 'TL'
        qty_display = f"{getattr(p, 'amount', 0) or 0} {cur_safe}"

    # ─── Tarihsel WAC ───
    try:
        wac_hs, wac_source = _resolve_historical_wac_hs(p)
    except Exception:
        wac_hs, wac_source = None, 'UNAVAILABLE'
    is_legacy = (wac_source == 'UNAVAILABLE')
    has_wac = (wac_hs is not None and wac_hs > Decimal('0'))
    wac_anomaly = bool(has_wac and wac_hs > Decimal('1.05'))

    # ─── Finansallar ───
    try:
        sale_price_eur = Decimal(str(getattr(p, 'amount', 0) or 0))
    except Exception:
        sale_price_eur = Decimal('0')
    try:
        sale_total_hs = Decimal(str(getattr(p, 'price_hs', 0) or 0))
    except Exception:
        sale_total_hs = Decimal('0')

    cost_total_hs = None
    gross_profit_hs = None
    profit_margin_pct = None

    # FAZ 41/42 — WAC her zaman HS/gram birimindedir (StockService.record_entry
    # WAC formülü gram tabanlı:
    #   new_wac_hs = ((old_gram * old_wac) + (qty_gram * unit_cost)) / new_total_gram
    # ).
    #
    # FAZ 42 EKLEMESİ — Barkodlu Parça Ürün (gerdanlık/yüzük/bilezik) Fallback'i:
    # retail_views.py:1102'de parça satışlarda Process.gram=0 olarak yazılıyor
    # (sepet "1 adet" semantiği). Bu durumda WAC HS/gram cinsinden saklı
    # olduğu için doğru maliyet ürünün fiziksel ağırlığıyla (Products.gram)
    # çarpılmalı. Aksi halde piece_val=1 ile çarpıp 17.125 HS yerine 0.685
    # HS gibi hatalı maliyet üretiliyor.
    #
    # Karar mantığı (üç dal):
    #   1) Process.gram > 0           → qty_ref = gram_val           (toptan/gram satış)
    #   2) Process.gram = 0, piece>0,
    #      Products.gram > 0          → qty_ref = product.gram       (barkodlu parça)
    #   3) gram=0, product.gram=0     → qty_ref = piece_val          (WATCH/DIAMOND)
    _product_gram_xray = Decimal('0')
    if prod is not None:
        try:
            _product_gram_xray = Decimal(str(getattr(prod, 'gram', 0) or 0))
        except (InvalidOperation, TypeError, ValueError):
            _product_gram_xray = Decimal('0')

    if gram_val > Decimal('0'):
        qty_ref = gram_val
    elif piece_val > 0 and _product_gram_xray > Decimal('0'):
        qty_ref = _product_gram_xray
    else:
        qty_ref = Decimal(piece_val or 0)

    if has_wac and qty_ref > Decimal('0'):
        try:
            cost_total_hs = (wac_hs * qty_ref).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            if not is_entry:
                gross_profit_hs = (sale_total_hs - cost_total_hs).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
                if cost_total_hs > Decimal('0'):
                    profit_margin_pct = float(
                        (gross_profit_hs / cost_total_hs * Decimal('100')).quantize(
                            Decimal('0.01'), rounding=ROUND_HALF_UP
                        )
                    )
        except Exception:
            cost_total_hs = None
            gross_profit_hs = None
            profit_margin_pct = None

    try:
        recorded_profit_tl = float(Decimal(str(getattr(p, 'gross_profit', 0) or 0)))
    except Exception:
        recorded_profit_tl = 0.0

    # transaction type label
    try:
        tx_label = p.get_transaction_type_display()
    except Exception:
        tx_label = getattr(p, 'transaction_type', '') or '—'

    currency_safe = 'TL'
    if prod is not None:
        try:
            currency_safe = getattr(prod, 'currency', None) or 'TL'
        except Exception:
            currency_safe = 'TL'

    return {
        'transaction_type': getattr(p, 'transaction_type', '') or '',
        'transaction_type_label': tx_label,
        'is_entry': is_entry,
        'product': product_payload,
        'employee': employee_payload,
        'qty': {
            'piece': piece_val,
            'gram': float(gram_val),
            'display': qty_display,
        },
        'financials': {
            'unit_price': float(getattr(p, 'unit_price', 0) or 0),
            'currency': currency_safe,
            'sale_price_eur': float(sale_price_eur),
            'sale_total_hs': float(sale_total_hs),
            'historical_wac_hs': (float(wac_hs) if wac_hs is not None else None),
            'historical_wac_source': wac_source,
            'cost_total_hs': (float(cost_total_hs) if cost_total_hs is not None else None),
            'gross_profit_hs': (float(gross_profit_hs) if gross_profit_hs is not None else None),
            'profit_margin_pct': profit_margin_pct,
            'recorded_profit_tl': recorded_profit_tl,
            'hs_rate_sale_eur': float(getattr(p, 'hs_rate_sale_eur', 0) or 0),
        },
        'data_quality': {
            'has_wac': has_wac,
            'is_legacy': is_legacy,
            'wac_anomaly': wac_anomaly,
        },
    }


@login_required(login_url='login')
def process_detail_modal_json(request, process_no):
    """
    İşlemler tablosu satırından açılan röntgen modali için lazy-load JSON.
    Her satır defensive — alan yoksa fallback değer döner, view 500 vermez.

    GET /process/detail-modal-json/<process_no>/
    """
    try:
        qs = (Process.objects
              .filter(process_no=process_no, is_deleted=False)
              .select_related('customer', 'product', 'product__category', 'employee', 'store')
              .order_by('date'))

        if not qs.exists():
            return JsonResponse(
                {'result': False, 'error_msg': f'"{process_no}" numaralı işlem bulunamadı.'},
                status=404
            )

        first = qs.first()
        ENTRY_SET = {'PURCHASE', 'STOCK_IN', 'RETURN', 'ORDER_IN'}

        items = []
        for p in qs:
            try:
                items.append(_build_xray_item_payload(p, ENTRY_SET))
            except Exception:
                logger.exception(f"process_detail_modal_json item build failed: process_no={process_no}, pid={getattr(p, 'id', '?')}")
                # Tek satır başarısız olsa bile diğerleri görünmeli
                items.append({
                    'transaction_type': getattr(p, 'transaction_type', '') or '',
                    'transaction_type_label': getattr(p, 'transaction_type', '') or '—',
                    'is_entry': False,
                    'product': None,
                    'employee': None,
                    'qty': {'piece': 0, 'gram': 0.0, 'display': '— (veri okuma hatası)'},
                    'financials': {
                        'unit_price': 0, 'currency': 'TL', 'sale_price_eur': 0, 'sale_total_hs': 0,
                        'historical_wac_hs': None, 'historical_wac_source': 'UNAVAILABLE',
                        'cost_total_hs': None, 'gross_profit_hs': None, 'profit_margin_pct': None,
                        'recorded_profit_tl': 0, 'hs_rate_sale_eur': 0,
                    },
                    'data_quality': {'has_wac': False, 'is_legacy': True, 'wac_anomaly': False},
                })

        # Header verisi
        customer_name = '—'
        try:
            if getattr(first, 'customer', None):
                cname = f"{first.customer.first_name or ''} {first.customer.last_name or ''}".strip()
                customer_name = cname or '—'
            elif getattr(first, 'supplier', None):
                customer_name = (
                    getattr(first.supplier, 'company_name', None)
                    or getattr(first.supplier, 'person_name', None)
                    or '—'
                )
        except Exception:
            customer_name = '—'

        try:
            tx_label = first.get_transaction_type_display()
        except Exception:
            tx_label = getattr(first, 'transaction_type', '') or '—'

        store_name = '—'
        try:
            store_name = getattr(getattr(first, 'store', None), 'name', None) or '—'
        except Exception:
            pass

        payload = {
            'result': True,
            'process_no': process_no,
            'process_type': getattr(first, 'process_type', '') or '',
            'transaction_type': getattr(first, 'transaction_type', '') or '',
            'transaction_type_label': tx_label,
            'status': getattr(first, 'is_status', '') or '',
            'is_canceled': (getattr(first, 'is_status', '') == 'CANCELED'),
            'date': first.date.isoformat() if getattr(first, 'date', None) else None,
            'store_name': store_name,
            'customer_name': customer_name,
            'items': items,
        }
        return JsonResponse(payload)

    except Exception as e:
        logger.exception(f"process_detail_modal_json failed: process_no={process_no}")
        return JsonResponse({
            'result': False,
            'error_msg': 'İşlem detayı yüklenirken bir hata oluştu.',
            'debug': str(e)[:200] if settings.DEBUG else None,
        }, status=500)


# ======================================================================
# BEKLEYEN STOK TAMAMLAMA (Waiting Stock Fulfillment)
# ======================================================================

@login_required(login_url='login')
@role_required('PROCESS_MANAGE')
def fulfill_waiting_stock(request):
    """
    waiting_stock=True olan Process kalemlerini gerçek stok hareketine çevirir.

    Akış:
        1. Process kaydını bul (waiting_stock=True olmalı)
        2. incoming_stock_* alanlarından düş
        3. Gerçek stok hareketini StockService üzerinden kaydet
        4. Process.waiting_stock = False yap

    POST JSON: { "process_ids": ["uuid1", "uuid2", ...] }
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek.'}, status=405)

    import json
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = {}

    process_ids = body.get('process_ids', [])
    if not process_ids:
        return JsonResponse({'result': False, 'error_msg': 'İşlem seçilmedi.'})

    store = getattr(request.user, 'store', None)
    if not store:
        return JsonResponse({'result': False, 'error_msg': 'Mağaza bulunamadı.'})

    fulfilled = 0
    errors = []

    for pid in process_ids:
        try:
            proc = Process.objects.select_related('product').get(
                id=pid,
                store=store,
                waiting_stock=True,
            )

            product = proc.product
            if not product:
                errors.append(f'{proc.process_no}: Ürün bağlantısı yok.')
                continue

            gram = Decimal(str(proc.gram or 0))
            piece = int(proc.piece or 0)

            # 1. incoming_stock alanlarından düş
            try:
                snapshot = StockSnapshot.objects.get(product=product, store=store)
                if hasattr(snapshot, 'incoming_stock_pieces') and piece > 0:
                    snapshot.incoming_stock_pieces = max(0, (snapshot.incoming_stock_pieces or 0) - piece)
                if hasattr(snapshot, 'incoming_stock_gram') and gram > 0:
                    snapshot.incoming_stock_gram = max(
                        Decimal('0'),
                        (snapshot.incoming_stock_gram or Decimal('0')) - gram
                    )
                snapshot.save()
            except StockSnapshot.DoesNotExist:
                pass  # Snapshot yoksa skip, update_product_stock oluşturacak

            # 2. Gerçek stok hareketini kaydet (waiting_stock=False ile)
            # Process transaction_type'a göre ENTRY veya EXIT
            tx_type = proc.transaction_type or 'PURCHASE'
            if tx_type in ['PURCHASE', 'STOCK_IN', 'RETURN']:
                mv = 'ENTRY'
            else:
                mv = 'EXIT'

            # Has maliyeti hesapla
            # FAZ 44 — Yön-duyarlı maliyet çözümlemesi:
            #   1) cost_amount_hs varsa daima öncelikli (Process.cost_amount_hs TOPLAM HS)
            #   2) ENTRY için: cost_amount_hs yoksa price_hs (alış toplamı) fallback olur
            #   3) EXIT için: price_hs SATIŞ tutarıdır → WAC'a yazılması yasaktır.
            #      Bunun yerine StockSnapshot.weighted_avg_cost_hs (zaten birim) kullan.
            #   4) Hiçbiri yoksa unit_cost_hs=0 — StockService EXIT için WAC değişmez,
            #      ENTRY için snapshot mevcut değerini korur.
            unit_cost_hs = Decimal('0.000')
            cost_total_hs = Decimal(str(proc.cost_amount_hs or 0))

            if cost_total_hs > Decimal('0') and gram > Decimal('0'):
                unit_cost_hs = (cost_total_hs / gram).quantize(
                    Decimal('0.0001'), rounding=ROUND_HALF_UP
                )
            elif mv == 'ENTRY':
                entry_total_hs = Decimal(str(proc.price_hs or 0))
                if entry_total_hs > Decimal('0') and gram > Decimal('0'):
                    unit_cost_hs = (entry_total_hs / gram).quantize(
                        Decimal('0.0001'), rounding=ROUND_HALF_UP
                    )
            else:
                # EXIT — snapshot'tan birim WAC oku (zaten birim cinsinden)
                try:
                    _exit_snap = StockSnapshot.objects.filter(
                        product=product, store=store
                    ).only('weighted_avg_cost_hs').first()
                    if _exit_snap and _exit_snap.weighted_avg_cost_hs:
                        unit_cost_hs = Decimal(str(_exit_snap.weighted_avg_cost_hs)).quantize(
                            Decimal('0.0001'), rounding=ROUND_HALF_UP
                        )
                except Exception:
                    unit_cost_hs = Decimal('0.000')

            update_product_stock(
                product=product,
                transaction_type=mv,
                quantity_pieces=piece,
                quantity_weight=gram,
                waiting_stock=False,  # Bu sefer gerçek stok hareketi!
                user=request.user,
                description=f"Bekleyen stok tamamlama — {proc.process_no}",
                process_no=proc.process_no,
                unit_cost_hs=unit_cost_hs,
            )

            # 3. Process flag'ini güncelle
            proc.waiting_stock = False
            proc.save(update_fields=['waiting_stock'])

            fulfilled += 1

        except Process.DoesNotExist:
            errors.append(f'{pid}: İşlem bulunamadı veya zaten tamamlanmış.')
        except InsufficientStockError as e:
            errors.append(f'{pid}: Yetersiz stok — {str(e)[:100]}')
        except Exception as e:
            logger.exception(f"fulfill_waiting_stock hatası: pid={pid}")
            errors.append(f'{pid}: {str(e)[:100]}')

    msg = f'{fulfilled} bekleyen stok kalemi tamamlandı.'
    if errors:
        msg += f' ({len(errors)} hata: {"; ".join(errors[:3])})'

    return JsonResponse({'result': fulfilled > 0 or not errors, 'msg': msg})


# ══════════════════════════════════════════════════════════════════════════════
# FAZ: İŞLEM ÖZETLERİ — KATEGORİ BAZLI KÂRLILIK RAPORU
# ══════════════════════════════════════════════════════════════════════════════

def _proc_fmt_tr(val, decimals=2):
    """TR locale sayı formatı: 10 → '10,00' | 1000 → '1.000,00'"""
    try:
        v = float(val or 0)
    except (ValueError, TypeError):
        v = 0.0
    s = f"{v:,.{decimals}f}"
    return s.replace(',', 'X').replace('.', ',').replace('X', '.')


def _build_profit_report_rows(store, filters):
    """
    Verilen filtrelere göre kategori bazında kârlılık raporu.
    Process.is_status='COMPLETED' + is_deleted=False + store filtresi uygulanır.
    Tek ORM sorgusunda koşullu aggregation ile satış/alış ayrı hesaplanır.
    """
    from django.db.models.functions import Coalesce
    from django.db.models import Count, Sum, Value, DecimalField, Q, F

    DEC15_2 = DecimalField(max_digits=15, decimal_places=2)
    DEC15_3 = DecimalField(max_digits=15, decimal_places=3)
    zero2 = Value(Decimal('0'), output_field=DEC15_2)
    zero3 = Value(Decimal('0'), output_field=DEC15_3)

    qs = Process.objects.filter(is_deleted=False, store=store, is_status='COMPLETED')

    # Tarih filtresi (dd/mm/YYYY formatı)
    from datetime import datetime as _dt
    date_from = (filters.get('date_from') or '').strip()
    date_to = (filters.get('date_to') or '').strip()
    if date_from:
        try:
            df = _dt.strptime(date_from, "%d/%m/%Y").date()
            qs = qs.filter(date__date__gte=df)
        except Exception:
            pass
    if date_to:
        try:
            dt_ = _dt.strptime(date_to, "%d/%m/%Y").date()
            qs = qs.filter(date__date__lte=dt_)
        except Exception:
            pass

    # İşlem türü (hızlı/perakende/toptan)
    process_kind = (filters.get('process_kind') or 'all').strip().lower()
    if process_kind != 'all':
        kind_map = {"fast": "FAST_PROCESS", "retail": "RETAIL", "wholesale": "WHOLESALE"}
        if kind_map.get(process_kind):
            qs = qs.filter(process_type=kind_map[process_kind])

    # Ürün tipi
    product_kind = (filters.get('product_kind') or 'all').strip().lower()
    if product_kind == "barcoded":
        qs = qs.filter(product__category__name__icontains='Barkodlu')
    elif product_kind == "gram":
        qs = qs.filter(product__is_gram_bullion=True)
    elif product_kind == "scrap":
        qs = qs.filter(product__is_scrap=True)
    elif product_kind == "bracelet":
        qs = qs.filter(product__category__name__icontains='Bilezik')

    # Ayar (milyem)
    mileage_filter = (filters.get('mileage') or '').strip()
    if mileage_filter:
        try:
            qs = qs.filter(product__product_mileage=Decimal(mileage_filter))
        except (InvalidOperation, TypeError, ValueError):
            pass

    sale_q = Q(transaction_type='SALE')
    buy_q = Q(transaction_type__in=['PURCHASE', 'STOCK_IN', 'ORDER_IN', 'RETURN'])

    rows = (
        qs
        .values(category=Coalesce(F('product__category__name'), Value('Kategorisiz')))
        .annotate(
            sale_count=Count('id', filter=sale_q),
            sale_piece=Coalesce(Sum('piece', filter=sale_q), 0),
            sale_gram=Coalesce(Sum('gram', filter=sale_q, output_field=DEC15_3), zero3),
            sale_amount_eur=Coalesce(Sum('amount', filter=sale_q, output_field=DEC15_2), zero2),
            sale_cost_tl=Coalesce(Sum('cost_amount_eur', filter=sale_q, output_field=DEC15_2), zero2),
            sale_profit_tl=Coalesce(Sum('gross_profit', filter=sale_q, output_field=DEC15_2), zero2),
            sale_hs=Coalesce(Sum('price_hs', filter=sale_q, output_field=DEC15_3), zero3),
            sale_cost_hs=Coalesce(Sum('cost_amount_hs', filter=sale_q, output_field=DEC15_3), zero3),
            buy_count=Count('id', filter=buy_q),
            buy_amount_eur=Coalesce(Sum('amount', filter=buy_q, output_field=DEC15_2), zero2),
            buy_hs=Coalesce(Sum('price_hs', filter=buy_q, output_field=DEC15_3), zero3),
        )
        .order_by('category')
    )

    # ════════════════════════════════════════════════════════════════════
    # FAZ 44 — Perakende cost_amount_hs=0 → StockLedger fallback
    # ════════════════════════════════════════════════════════════════════
    # retail_views.py Process.cost_amount_hs alanını hiç set etmiyor; sadece
    # fast_views (Hızlı İşlem) yazıyor. Sonuç: kategori toplamlarında perakende
    # SALE'ler maliyet=0 ile geliyor → kar_hs şişiyor. Çözüm: cost_amount_hs=0
    # olan SALE Process'leri için StockLedger.unit_cost_hs (1.05 normalize) ×
    # gram türetip kategori bazında ekle.
    missing_cost_processes = qs.filter(sale_q, cost_amount_hs=0).values(
        'id', 'product_id', 'gram', 'piece', 'product__gram',
        'product__category__name'
    )
    extra_cost_by_category = {}
    if missing_cost_processes:
        proc_ids = [str(mp['id']) for mp in missing_cost_processes]
        led_map = {}
        for led in StockLedger.objects.filter(
            ref_type='process',
            ref_id__in=proc_ids,
        ).only('ref_id', 'product_id', 'unit_cost_hs'):
            led_map[(str(led.ref_id), led.product_id)] = led.unit_cost_hs

        for mp in missing_cost_processes:
            cat = mp['product__category__name'] or 'Kategorisiz'
            unit_cost = led_map.get((str(mp['id']), mp['product_id']))
            if unit_cost is None or unit_cost <= 0:
                continue
            unit_cost = Decimal(str(unit_cost))
            gram_val = Decimal(str(mp['gram'] or 0))
            prod_gram = Decimal(str(mp['product__gram'] or 0))
            piece_val = int(mp['piece'] or 0)

            # FAZ 44 — 1.05 EŞİK KURALI: legacy total tespiti
            qty_for_mult = gram_val
            if gram_val <= Decimal('0') and piece_val > 0 and prod_gram > Decimal('0'):
                # Barkodlu parça ürün: Process.gram=0, Products.gram fiziksel ağırlık
                qty_for_mult = prod_gram

            if unit_cost > Decimal('1.05'):
                # Legacy: birim alana toplam yazılmış. Doğrudan toplam HS olarak kullan.
                cost_total = unit_cost
            elif qty_for_mult > Decimal('0'):
                cost_total = (unit_cost * qty_for_mult).quantize(
                    Decimal('0.001'), rounding=ROUND_HALF_UP
                )
            else:
                cost_total = Decimal('0')

            extra_cost_by_category[cat] = (
                extra_cost_by_category.get(cat, Decimal('0')) + cost_total
            )

    data = []
    for r in rows:
        cat_name = r['category'] or 'Kategorisiz'
        # FAZ 44 — Eksik maliyet ekle
        extra = extra_cost_by_category.get(cat_name, Decimal('0'))
        sale_cost_hs_eff = (Decimal(str(r['sale_cost_hs'] or 0)) + extra)

        # Kar Hası = Satış Hası - Maliyet Hası
        kar_hs = float(r['sale_hs'] or 0) - float(sale_cost_hs_eff)
        sale_tl = float(r['sale_amount_eur'] or 0)
        profit_tl = float(r['sale_profit_tl'] or 0)
        kar_pct = (profit_tl / sale_tl * 100.0) if sale_tl > 0 else 0.0

        data.append({
            'category': r['category'] or 'Kategorisiz',
            'sale_count': r['sale_count'] or 0,
            'sale_piece': int(r['sale_piece'] or 0),
            'sale_gram': _proc_fmt_tr(r['sale_gram'], 2),
            'sale_amount_eur': _proc_fmt_tr(r['sale_amount_eur'], 2),
            'sale_cost_tl': _proc_fmt_tr(r['sale_cost_tl'], 2),
            'sale_profit_tl': _proc_fmt_tr(r['sale_profit_tl'], 2),
            'sale_hs': _proc_fmt_tr(r['sale_hs'], 3),
            'sale_cost_hs': _proc_fmt_tr(sale_cost_hs_eff, 3),
            'kar_hs': _proc_fmt_tr(kar_hs, 3),
            'kar_pct': _proc_fmt_tr(kar_pct, 2),
            'buy_count': r['buy_count'] or 0,
            'buy_amount_eur': _proc_fmt_tr(r['buy_amount_eur'], 2),
            'buy_hs': _proc_fmt_tr(r['buy_hs'], 3),
            # Ham değerler (JS ve toplam için)
            '_raw_sale_amount_eur': float(r['sale_amount_eur'] or 0),
            '_raw_sale_cost_tl': float(r['sale_cost_tl'] or 0),
            '_raw_sale_profit_tl': profit_tl,
            '_raw_sale_hs': float(r['sale_hs'] or 0),
            '_raw_sale_cost_hs': float(sale_cost_hs_eff),
            '_raw_kar_hs': kar_hs,
            '_raw_sale_gram': float(r['sale_gram'] or 0),
            '_raw_buy_amount_eur': float(r['buy_amount_eur'] or 0),
            '_raw_buy_hs': float(r['buy_hs'] or 0),
        })
    return data


@login_required(login_url='login')
@role_required('PROCESS_PROCESS_INDEX')
def profit_report_data(request):
    """Kategori bazlı kârlılık raporu AJAX endpoint'i."""
    store = request.user.store
    filters = {
        'date_from': request.GET.get('date_from', ''),
        'date_to': request.GET.get('date_to', ''),
        'process_kind': request.GET.get('process_kind', 'all'),
        'product_kind': request.GET.get('product_kind', 'all'),
        'mileage': request.GET.get('mileage', ''),
    }
    data = _build_profit_report_rows(store, filters)
    return JsonResponse({'result': True, 'data': data, 'filters': filters})


@login_required(login_url='login')
@role_required('PROCESS_PROCESS_INDEX')
def profit_report_pdf(request):
    """Kategori bazlı kârlılık raporunu xhtml2pdf ile PDF olarak indirir (landscape A4)."""
    from io import BytesIO
    try:
        from xhtml2pdf import pisa
    except ImportError:
        return HttpResponse('xhtml2pdf kutuphanesi yuklu degil.', status=500)
    from django.template.loader import render_to_string

    store = request.user.store
    filters = {
        'date_from': request.GET.get('date_from', ''),
        'date_to': request.GET.get('date_to', ''),
        'process_kind': request.GET.get('process_kind', 'all'),
        'product_kind': request.GET.get('product_kind', 'all'),
        'mileage': request.GET.get('mileage', ''),
    }
    data = _build_profit_report_rows(store, filters)

    # Genel toplamlar
    t_sale_count = sum(d['sale_count'] for d in data)
    t_sale_tl = sum(d['_raw_sale_amount_eur'] for d in data)
    t_cost_tl = sum(d['_raw_sale_cost_tl'] for d in data)
    t_profit_tl = sum(d['_raw_sale_profit_tl'] for d in data)
    t_sale_hs = sum(d['_raw_sale_hs'] for d in data)
    t_cost_hs = sum(d['_raw_sale_cost_hs'] for d in data)
    t_kar_hs = sum(d['_raw_kar_hs'] for d in data)
    t_buy_count = sum(d['buy_count'] for d in data)
    t_buy_tl = sum(d['_raw_buy_amount_eur'] for d in data)
    t_kar_pct = (t_profit_tl / t_sale_tl * 100.0) if t_sale_tl > 0 else 0.0

    store_name = (
        getattr(store, 'barcode_title', None)
        or getattr(store, 'title', None)
        or getattr(store, 'name', None)
        or 'Kuyum Plus'
    )
    report_date = timezone.now().strftime('%d/%m/%Y %H:%M')

    # Filtre etiketleri
    filter_labels = []
    if filters['date_from'] or filters['date_to']:
        filter_labels.append(
            f"Tarih: {filters['date_from'] or '—'} / {filters['date_to'] or '—'}"
        )
    pk_map = {'fast': 'Hizli', 'retail': 'Perakende', 'wholesale': 'Toptan'}
    if filters['process_kind'] != 'all' and pk_map.get(filters['process_kind']):
        filter_labels.append(f"Islem Turu: {pk_map[filters['process_kind']]}")
    prod_map = {'barcoded': 'Barkodlu Urunler', 'gram': 'Gram Altin',
                'scrap': 'Hurda', 'bracelet': 'Bilezik'}
    if filters['product_kind'] != 'all' and prod_map.get(filters['product_kind']):
        filter_labels.append(f"Urun Tipi: {prod_map[filters['product_kind']]}")
    if filters['mileage']:
        filter_labels.append(f"Ayar (Milyem): {filters['mileage']}")

    html_string = render_to_string(
        'management/process/profit_report_pdf.html',
        {
            'store_name': store_name,
            'report_date': report_date,
            'data': data,
            'filter_labels': filter_labels,
            't_sale_count': t_sale_count,
            't_sale_tl': _proc_fmt_tr(t_sale_tl, 2),
            't_cost_tl': _proc_fmt_tr(t_cost_tl, 2),
            't_profit_tl': _proc_fmt_tr(t_profit_tl, 2),
            't_sale_hs': _proc_fmt_tr(t_sale_hs, 3),
            't_cost_hs': _proc_fmt_tr(t_cost_hs, 3),
            't_kar_hs': _proc_fmt_tr(t_kar_hs, 3),
            't_buy_count': t_buy_count,
            't_buy_tl': _proc_fmt_tr(t_buy_tl, 2),
            't_kar_pct': _proc_fmt_tr(t_kar_pct, 2),
        },
    )

    pdf_buffer = BytesIO()
    pdf_status = pisa.CreatePDF(BytesIO(html_string.encode('utf-8')), dest=pdf_buffer)
    if pdf_status.err:
        return HttpResponse('PDF olusturulurken hata olustu.', status=500)

    now_str = timezone.now().strftime('%Y%m%d-%H%M')
    response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="islem-karlilik-raporu-{now_str}.pdf"'
    )
    return response
