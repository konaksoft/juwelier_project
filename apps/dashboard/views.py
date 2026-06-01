import io
from django.shortcuts import render, redirect
from django.db.models import Sum, Q, Count, Case, When, Value, DecimalField, IntegerField
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from datetime import timedelta, datetime

from apps.customers import models
from apps.customers.models import Customers
from apps.products.models import Products
from apps.suppliers.models import Suppliers
from apps.process.models import Process, Payment
from apps.definitions.categories.models import Categories
# --- FAZ 4: StockSnapshot ve StockLedger entegrasyonu ---
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.accounts.models import Users
from django.db.models.functions import TruncDate
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.http import HttpResponse
from django.template import engines
from django.utils import timezone
from apps.roles.decorators import role_required
from django.template.loader import render_to_string
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf import pisa
from xhtml2pdf.default import DEFAULT_FONT
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from apps.products.models import CurrencyChoices
import os
# Eski import: from apps.inventories.models import InventoryMovement  # KALDIRILDI (FAZ 4)
from django.views.decorators.clickjacking import xframe_options_exempt
from apps.dashboard.tasks import _build_customer_detail_report_context, _parse_date_params

FX_CODES = {
    'USD', 'EUR', 'CAD', 'QAR', 'TRY', 'GBP', 'CHF', 'AUD', 'SAR',
}
font_path = os.path.join(settings.BASE_DIR, "static", "management", "fonts", "DejaVuSansCustom.ttf")
font_name = "DejaVuSansCustom"
pdfmetrics.registerFont(TTFont(font_name, font_path))
DEFAULT_FONT['helvetica'] = font_name
DEFAULT_FONT['Times-Roman'] = font_name


def _local_today():
    return timezone.localtime(timezone.now()).date()


@role_required('DASHBOARD_INDEX_VIEW')
@login_required(login_url='login')
def index_view(request):
    ziynet_category = Categories.objects.filter(name='Ziynet', is_deleted=False).first()
    context = {
        'title': 'Çalışma Alanı',
        'categories': Categories.objects.filter(is_deleted=False),
        'default_category': ziynet_category.id if ziynet_category else None
    }

    return render(request, 'management/dashboard/index.html', context)


@login_required(login_url='login')
def dashboard_data(request):
    """
    FAZ R-1: Optimize edilmiş dashboard grafik verileri.
    DailyStoreReport rollup tablosundan çeker — 4 sorgu yerine 1 sorgu.
    Rollup yoksa fallback olarak canlı hesaplar.
    """
    from apps.dashboard.models import DailyStoreReport

    store = request.user.store
    period_str = request.GET.get('period')
    today = _local_today()
    end_date = today

    if period_str:
        try:
            period_days = int(period_str)
            start_date = today - timedelta(days=period_days)
        except ValueError:
            start_date = today
    else:
        start_date = today

    # Rollup tablosundan günlük veriler (1 sorgu)
    rollup_data = (
        DailyStoreReport.objects
        .filter(store=store, report_date__gte=start_date, report_date__lte=end_date)
        .values('report_date')
        .order_by('report_date')
    )
    rollup_map = {r['report_date']: r for r in rollup_data}

    date_list = []
    current_day = start_date
    while current_day <= end_date:
        date_list.append(current_day)
        current_day += timedelta(days=1)

    daily_tl_diff = []
    daily_has_diff = []
    for d in date_list:
        r = rollup_map.get(d)
        if r:
            tl_in = float(r.get('cash_in', 0) or 0) + float(r.get('card_in', 0) or 0) + float(r.get('transfer_in', 0) or 0)
            tl_out = float(r.get('cash_out', 0) or 0) + float(r.get('card_out', 0) or 0) + float(r.get('transfer_out', 0) or 0)
            daily_tl_diff.append(tl_in - tl_out)
            daily_has_diff.append(float(r.get('total_purchases_hs', 0) or 0) - float(r.get('total_sales_hs', 0) or 0))
        else:
            # Rollup henüz yok — fallback: doğrudan Process'ten (eski sorgu basitleştirilmiş)
            day_procs = Process.objects.filter(
                store=store, is_deleted=False, is_status='COMPLETED', date__date=d,
            )
            hs_in = day_procs.filter(transaction_type='PURCHASE').aggregate(t=Sum('price_hs'))['t'] or Decimal('0')
            hs_out = day_procs.filter(transaction_type='SALE').aggregate(t=Sum('price_hs'))['t'] or Decimal('0')
            # Payment: process_group FK üzerinden (subquery yerine JOIN)
            day_pay = Payment.objects.filter(
                process_group__store=store, date__date=d,
                is_cancelled=False, is_approved=True,
            )
            tl_in = float(day_pay.filter(is_output=False).aggregate(t=Sum('amount'))['t'] or 0)
            tl_out = float(day_pay.filter(is_output=True).aggregate(t=Sum('amount'))['t'] or 0)
            daily_tl_diff.append(tl_in - tl_out)
            daily_has_diff.append(float(hs_in - hs_out))

    # Top selling: hâlâ Process'ten (küçük sorgu, cache'lenebilir)
    top_selling_qs = (
        Process.objects
        .filter(transaction_type='SALE', store=store, is_deleted=False, is_status='COMPLETED')
        .values('product__name')
        .annotate(total_sold=Sum('piece'))
        .order_by('-total_sold')[:5]
    )

    return JsonResponse({
        'date_labels': [d.strftime('%Y-%m-%d') for d in date_list],
        'daily_tl_diff': daily_tl_diff,
        'daily_has_diff': daily_has_diff,
        'top_selling_product_names': [p['product__name'] for p in top_selling_qs],
        'top_selling_product_quantities': [p['total_sold'] for p in top_selling_qs],
    })


@login_required(login_url='login')
def get_summary_data(request):
    """
    FAZ R-1: Optimize edilmiş dashboard özet verileri.
    8 ayrı sorgu → 2 sorgu + Redis cache (5 dakika).
    DailyStoreReport rollup varsa onu kullanır, yoksa canlı hesaplar.
    """
    from django.core.cache import cache as redis_cache
    from apps.dashboard.services import get_dashboard_summary

    store = request.user.store
    store_id = store.id
    today = _local_today()

    # Statik sayılar: Redis cache (5 dk)
    counts_key = f"dashboard_counts:{store_id}"
    counts = redis_cache.get(counts_key)
    if not counts:
        counts = {
            'total_customers': Customers.objects.filter(store=store_id).count(),
            'total_employees': Users.objects.filter(role__name="Personel", store=store_id).count(),
            'total_products': Products.objects.filter(Q(store=store_id) | Q(store__isnull=True)).count(),
            'total_suppliers': Suppliers.objects.filter(store=store_id).count(),
        }
        redis_cache.set(counts_key, counts, timeout=300)

    # Günlük KPI: DailyStoreReport rollup'tan (tek sorgu veya cache)
    kpi = get_dashboard_summary(store, today)

    return JsonResponse({
        'total_customers': counts['total_customers'],
        'total_employees': counts['total_employees'],
        'daily_purchases': kpi['purchase_count'],
        'daily_sales': kpi['sale_count'],
        'total_products': counts['total_products'],
        'total_suppliers': counts['total_suppliers'],
        'daily_input_tl': str(kpi['cash_in'] + kpi['card_in'] + kpi['transfer_in']),
        'daily_output_tl': str(kpi['cash_out'] + kpi['card_out'] + kpi['transfer_out']),
        'daily_input_hs': str(kpi['total_purchases_hs']),
        'daily_output_hs': str(kpi['total_sales_hs']),
        # FAZ R-1: Yeni KPI alanları (frontend güncellendiğinde kullanılacak)
        'daily_gross_profit': str(kpi['total_gross_profit']),
        'daily_net_profit': str(kpi['total_net_profit']),
        'stock_value_eur': str(kpi['stock_value_eur']),
        'net_cash_flow': str(kpi['net_cash_flow']),
        'transaction_count': kpi['transaction_count'],
        'unique_customers': kpi['unique_customers'],
        'commission_total': str(kpi['commission_total']),
    })


@login_required(login_url='login')
@role_required('DASHBOARD_INDEX_VIEW')
def get_assets_summary(request):
    """
    Mağaza varlıkları (Kasa/Banka/POS + Stok HAS + Kategori/Ayar dağılımı)
    anlık özeti. Dashboard sayfası bu endpoint'i AJAX ile çağırır.

    Cache: Redis, 10 dk TTL. Invalidasyon gerektiğinde cache.delete kullanılabilir.
    """
    from django.core.cache import cache as redis_cache
    from apps.dashboard.services import get_store_assets_summary

    store = request.user.store
    if not store:
        return JsonResponse({
            'cash_accounts': [],
            'pos_bank_accounts': [],
            'cash_total_by_currency': {},
            'stock_summary': {
                'total_has_value': 0,
                'total_gram': 0,
                'total_pieces': 0,
                'categories': [],
            },
            'karat_breakdown': {
                'k14_gram': 0, 'k18_gram': 0, 'k22_gram': 0,
                'scrap_gram': 0, 'bracelet_gram': 0,
            },
            'generated_at': timezone.now().isoformat(),
        })

    cache_key = f"dashboard_assets_summary:{store.id}"
    payload = redis_cache.get(cache_key)
    if not payload:
        payload = get_store_assets_summary(store)
        redis_cache.set(cache_key, payload, timeout=600)  # 10 dk
    return JsonResponse(payload)


# ============================================================================
# FAZ 26 (2026-05-01): Patron Odaklı Dashboard — TAB 1 Endpoint
# ============================================================================
# Yeni 3 sekmeli dashboard yapısının ilk sekmesini ("Mağaza Varlıkları ve
# Stok") besleyen tek-shot AJAX endpoint'i. Mevcut /assets-summary/ endpoint'i
# (multi_material_cards.js, mağaza KPI dashboard vb.) tarafından
# kullanıldığından korunur; bu yeni endpoint AYRI bir route olarak eklenir.
#
# Cache stratejisi:
#   Redis 5 dk TTL. Stok ve tedarikçi cari değiştiğinde davetkar invalidasyon
#   gerekirse: cache.delete(f"dashboard_assets_v2:{store.id}").
#
# Yetki:
#   index_view ile aynı role gate'ine bağlanır (DASHBOARD_INDEX_VIEW).
# ============================================================================

@role_required('DASHBOARD_INDEX_VIEW')
@login_required(login_url='login')
def assets_v2_view(request):
    """
    TAB 1 — Mağaza Varlıkları ve Stok için tek-shot JSON.

    Dönen şema (bkz. services.get_tab1_assets_data docstring):
        net_summary, kasa, banka_pos_toplam_try, stok_kirilimlari,
        tedarikci_borclari, generated_at.
    """
    from django.core.cache import cache as redis_cache
    from apps.dashboard.services import get_tab1_assets_data

    store = request.user.store
    if not store:
        return JsonResponse(get_tab1_assets_data(None))

    cache_key = f"dashboard_assets_v2:{store.id}"
    # FAZ 26.3: ?refresh=1 query string'i ile cache bypass — Yenile butonu
    # kullanıcının taze veri görmesini garanti etsin.
    force_refresh = request.GET.get('refresh') in ('1', 'true', 'True')
    payload = None if force_refresh else redis_cache.get(cache_key)
    if not payload:
        payload = get_tab1_assets_data(store)
        redis_cache.set(cache_key, payload, timeout=300)  # 5 dk
    return JsonResponse(payload)


@login_required(login_url='login')
def get_top_customers_by_sales(request):
    store = request.user.store
    top_customers = Process.objects.filter(transaction_type='SALE', store=store, customer__is_deleted=False,
                                           customer__isnull=False).values('customer__first_name',
                                                                          'customer__last_name').annotate(
        total_sales_eur=Sum('amount')).order_by('-total_sales_eur')[:5]
    customer_names = [f"{c['customer__first_name']} {c['customer__last_name']}" for c in top_customers]
    total_sales_values = [float(c['total_sales_eur']) for c in top_customers]
    return JsonResponse({
        'customer_names': customer_names,
        'total_sales_values': total_sales_values
    })


@login_required(login_url='login')
@role_required('DASHBOARD_GENERATE_REPORT')
@xframe_options_exempt
def generate_report(request):
    # Yeni tarih aralığı parametrelerini alıyoruz
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    period = request.GET.get('period', 'daily')

    user_store_id = request.user.store_id
    today_date = _local_today()

    # --- 1. TARİH HESAPLAMA MANTIĞI ---
    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
            period_days = (end_date - start_date).days + 1
        except (ValueError, TypeError):
            start_date = today_date
            end_date = today_date
            period_days = 1
    elif period == 'daily':
        start_date = today_date
        end_date = today_date
        period_days = 1
    elif period == 'weekly':
        start_date = today_date - timedelta(days=7)
        end_date = today_date
        period_days = 7
    elif period == 'two_weeks':
        start_date = today_date - timedelta(days=14)
        end_date = today_date
        period_days = 14
    elif period == 'monthly':
        start_date = today_date - timedelta(days=30)
        end_date = today_date
        period_days = 30
    else:
        return HttpResponse('Geçersiz periyot veya tarih seçildi.', status=400)

    # --- 2. YARDIMCI FONKSİYONLAR ---
    def fmt_tr(val, d=2):
        try:
            q = Decimal(str(val if val is not None else "0"))
        except (InvalidOperation, ValueError, TypeError):
            q = Decimal("0")
        s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    def now_local():
        n = timezone.now()
        return timezone.localtime(n) if timezone.is_aware(n) else n

    def fmt_dt(dt):
        if timezone.is_aware(dt):
            return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M")
        return dt.strftime("%d/%m/%Y %H:%M")

    # --- 3. VERİ SORGULAMA ---
    processes = (
        Process.objects
        .filter(
            is_deleted=False,
            store_id=user_store_id,
            is_status='COMPLETED',
            date__date__gte=start_date,
            date__date__lte=end_date  # end_date kullanıldı
        )
        .select_related('employee', 'customer', 'product')
        .order_by('date')
    )

    # Aggregates (Toplamlar)
    total_sales = processes.filter(transaction_type='SALE').aggregate(Sum('amount'))['amount__sum'] or 0
    total_purchases = processes.filter(transaction_type__in=['PURCHASE', 'RETURN']).aggregate(Sum('amount'))[
                          'amount__sum'] or 0
    total_sale_has = processes.filter(transaction_type='SALE').aggregate(Sum('price_hs'))['price_hs__sum'] or 0
    net_total = Decimal(str(total_sales)) - Decimal(str(total_purchases))

    # Tablo satırlarını hazırla
    rows = []
    for p in processes:
        rows.append({
            "process_no": p.process_no,
            "datetime": fmt_dt(p.date),
            "customer": (
                f"{getattr(p.customer, 'first_name', '-') or '-'} "
                f"{getattr(p.customer, 'last_name', '-') or '-'}"
            ) if p.customer else "-",
            "product": (p.product.name if p.product else "-"),
            "tx_type": p.get_transaction_type_display() if hasattr(p,
                                                                   "get_transaction_type_display") else p.transaction_type,
            "piece": fmt_tr(p.piece or 0, 0),
            "unit_price": fmt_tr(p.unit_price or 0, 2),
            "total": fmt_tr(p.amount or 0, 2),
            "has": fmt_tr(p.price_hs or 0, 3),
        })

    # --- 4. CONTEXT VE RESPONSE ---
    store = request.user.store
    store_id_text = str(
        getattr(store, "store_id", "")
        or getattr(store, "display_id", "")
        or getattr(store, "identity_no", "")
        or getattr(store, "id", "-")
    )

    now_loc = now_local()
    context = {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Profesyonel Kuyum Yönetim Sistemi",
        "report_title": "İşlem Raporu",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "branch_name": getattr(store, "branch_name", getattr(store, "name", "-")),
        "authorized_person": (f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username),
        "period_days": period_days,

        "sum_total_sales_eur": fmt_tr(total_sales, 2),
        "sum_total_purchases_eur": fmt_tr(total_purchases, 2),
        "sum_total_sales_has": fmt_tr(total_sale_has, 3),
        "sum_net_total_tl": fmt_tr(net_total, 2),
        "is_net_negative": net_total < 0,

        "rows": rows,
        "company_address": getattr(store, "address", ""),
        "company_email": getattr(store, "email", ""),
        "company_contact": getattr(store, "phone", ""),
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),

        "store_id": store_id_text,
        "store_display_id": store_id_text,
        "is_preview": request.GET.get('preview') == '1'
    }

    download_pdf = request.GET.get('download_pdf', '0') == '1'

    if not download_pdf:
        return render(request, "management/dashboard/process_report.html", context)

    # PDF Oluşturma
    html_content = render_to_string("management/dashboard/process_report.html", context, request=request)
    pdf_file = io.BytesIO()
    pisa_status = pisa.CreatePDF(io.BytesIO(html_content.encode("utf-8")), dest=pdf_file)

    if pisa_status.err:
        return HttpResponse("PDF oluşturulurken hata oluştu", status=500)

    pdf_file.seek(0)
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename=Islem_Raporu.pdf'
    return response


def _local_now():
    n = timezone.now()
    return timezone.localtime(n) if timezone.is_aware(n) else n


def _fmt_dt(dt):
    if timezone.is_aware(dt):
        return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M")
    return dt.strftime("%d/%m/%Y %H:%M")


@login_required(login_url='login')
@xframe_options_exempt
def generate_currency_report(request):
    # Yeni tarih aralığı parametrelerini alıyoruz
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    period = request.GET.get("period", "daily")
    store = request.user.store

    store_id_text = str(
        getattr(store, "store_id", "")
        or getattr(store, "display_id", "")
        or getattr(store, "identity_no", "")
        or getattr(store, "id", "-")
    )

    # --- YARDIMCI FONKSİYONLAR ---
    def fmt_tr(val, d=2):
        try:
            q = Decimal(str(val if val is not None else "0"))
        except (InvalidOperation, ValueError, TypeError):
            q = Decimal("0")
        s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    def D(val, q='0'):
        try:
            return Decimal(str(val if val is not None else q))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(q)

    def full_name(obj):
        if not obj: return '-'
        fn = getattr(obj, 'first_name', '') or ''
        ln = getattr(obj, 'last_name', '') or ''
        val = f"{fn} {ln}".strip()
        return val if val else getattr(obj, 'username', '-') if hasattr(obj, 'username') else '-'

    today_date = _local_today()

    # --- 1. TARİH HESAPLAMA MANTIĞI ---
    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
            period_days = (end_date - start_date).days + 1
        except (ValueError, TypeError):
            start_date = today_date
            end_date = today_date
            period_days = 1
    elif period == 'daily':
        start_date = today_date
        end_date = today_date
        period_days = 1
    elif period == 'weekly':
        start_date = today_date - timedelta(days=7)
        end_date = today_date
        period_days = 7
    elif period == 'two_weeks':
        start_date = today_date - timedelta(days=14)
        end_date = today_date
        period_days = 14
    elif period == 'monthly':
        start_date = today_date - timedelta(days=30)
        end_date = today_date
        period_days = 30
    else:
        return HttpResponse('Geçersiz periyot veya tarih seçildi.', status=400)

    # --- 2. SORGULAMA ---
    qs_base = (
        Process.objects
        .filter(
            is_deleted=False,
            is_status='COMPLETED',
            date__date__gte=start_date,
            date__date__lte=end_date,  # end_date kullanıldı
            store=store,
        )
        .select_related('employee', 'customer', 'product')
        .order_by('date')
    )

    # Döviz filtreleme lojiği
    fx_codes = FX_CODES  # Global tanımlı FX_CODES listesi
    regex = r'^(' + '|'.join(sorted(fx_codes)) + r')\b'
    fx_q = (
            Q(product__price_currency__in=fx_codes)
            | Q(product__currency__in=fx_codes)
            | Q(product__name__iregex=regex)
    )
    qs = qs_base.filter(fx_q)

    # --- 3. VERİ İŞLEME ---
    total_buy_tl = Decimal('0')
    total_sell_tl = Decimal('0')
    by_currency = defaultdict(lambda: {
        'BUY': {'fx': Decimal('0'), 'tl': Decimal('0')},
        'SELL': {'fx': Decimal('0'), 'tl': Decimal('0')}
    })
    rows = []

    for p in qs:
        tx = (p.transaction_type or '').upper()
        if tx in ('PURCHASE', 'RETURN'):
            tx_key, tx_label = 'BUY', 'Alış'
        elif tx == 'SALE':
            tx_key, tx_label = 'SELL', 'Satış'
        else:
            continue

        prod = getattr(p, 'product', None)
        cur = None
        if prod:
            cur = getattr(prod, 'price_currency', None) or getattr(prod, 'currency', None)
            if not cur and getattr(prod, 'name', None):
                name_up = prod.name.upper()
                for c in fx_codes:
                    if name_up.startswith(c):
                        cur = c
                        break
        currency_code = cur or 'FX'

        fx_amount = D(p.piece, '0')
        rate_tl = D(p.unit_price, '0')
        tl_total = D(p.amount, '0')

        if tx_key == 'BUY':
            total_buy_tl += tl_total
        else:
            total_sell_tl += tl_total

        by_currency[currency_code][tx_key]['fx'] += fx_amount
        by_currency[currency_code][tx_key]['tl'] += tl_total

        rows.append({
            "process_no": p.process_no or str(p.id)[:8],
            "datetime": timezone.localtime(p.date).strftime("%d/%m/%Y %H:%M"),  # _fmt_dt yerine doğrudan strftime
            "customer": full_name(getattr(p, 'customer', None)),
            "currency": currency_code,
            "tx_type": tx_label,
            "fx_amount": fmt_tr(fx_amount, 2),
            "rate": fmt_tr(rate_tl, 4),
            "tl_total": fmt_tr(tl_total, 2),
            "employee": full_name(getattr(p, 'employee', None)),
        })

    net_tl = total_sell_tl - total_buy_tl

    currency_summary = []
    for cur, buckets in by_currency.items():
        buy_fx = buckets['BUY']['fx']
        sell_fx = buckets['SELL']['fx']
        buy_tl = buckets['BUY']['tl']
        sell_tl = buckets['SELL']['tl']
        currency_summary.append({
            "currency": cur,
            "buy_fx": fmt_tr(buy_fx, 2),
            "sell_fx": fmt_tr(sell_fx, 2),
            "net_fx": fmt_tr(sell_fx - buy_fx, 2),
            "buy_tl": fmt_tr(buy_tl, 2),
            "sell_tl": fmt_tr(sell_tl, 2),
            "net_tl": fmt_tr(sell_tl - buy_tl, 2),
        })

    # --- 4. CONTEXT VE RESPONSE ---
    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n

    context = {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Profesyonel Kuyum Yönetim Sistemi",
        "report_title": "Döviz İşlem Raporu",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "branch_name": getattr(store, "branch_name", getattr(store, "name", "-")),
        "authorized_person": full_name(request.user),
        "period_days": period_days,
        "sum_total_sales_eur": fmt_tr(total_sell_tl, 2),
        "sum_total_purchases_eur": fmt_tr(total_buy_tl, 2),
        "sum_net_total_tl": fmt_tr(net_tl, 2),
        "is_net_negative": (net_tl < 0),
        "rows": rows,
        "currency_summary": currency_summary,
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
        "store_id": store_id_text,
        "is_preview": request.GET.get('preview') == '1'
    }

    if request.GET.get('download_pdf', '0') != '1':
        return render(request, "management/dashboard/currency_report.html", context)

    # PDF Oluşturma
    from django.template.loader import render_to_string
    import xhtml2pdf.pisa as pisa
    html = render_to_string("management/dashboard/currency_report.html", context, request=request)
    pdf_file = io.BytesIO()
    pisa.CreatePDF(io.BytesIO(html.encode("utf-8")), dest=pdf_file)
    pdf_file.seek(0)

    resp = HttpResponse(pdf_file, content_type="application/pdf")
    resp["Content-Disposition"] = 'attachment; filename=Doviz_Islem_Raporu.pdf'
    return resp


@login_required(login_url='login')
@xframe_options_exempt
def generate_current_stock_report(request):
    """
    FAZ 10: Profesyonel Stok Durum Raporu — Immutable Ledger Entegrasyonu.

    Hesaplama mantığı tamamen StockLedger'a (tek kaynak) dayanır:
      Açılış Stoğu   = Ledger Tüm-Zaman Bakiye  –  Dönem Girişleri  +  Dönem Çıkışları
      Giren          = Dönemdeki tüm direction=IN hareketleri
      Çıkan          = Dönemdeki tüm direction=OUT hareketleri
      Olması Gereken = Ledger Tüm-Zaman Bakiye  (= Açılış + Giren – Çıkan)
      Anlık Stok     = StockSnapshot (cache tablosu)
      Fark           = Anlık Stok – Olması Gereken  →  0 ise Integrity ✓

    Ürün sınıflandırması 6 ana gruba ayrılır:
      ALTIN_HAS · ZİYNET · BİLEZİK · HURDA · DÖVİZ · BARKODLU
    """

    # ═══════════════════════════════════════════════════════════════
    # 0. YARDIMCI: Güvenli Decimal Dönüştürücü
    # ═══════════════════════════════════════════════════════════════
    def D(val, default='0'):
        try:
            if val is None:
                return Decimal(default)
            return Decimal(str(val))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    # ═══════════════════════════════════════════════════════════════
    # 1. TARİH PARAMETRELERİ
    # ═══════════════════════════════════════════════════════════════
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    period = request.GET.get('period', 'daily')

    store = request.user.store
    store_id_text = str(
        getattr(store, 'store_id', '')
        or getattr(store, 'display_id', '')
        or getattr(store, 'identity_no', '')
        or getattr(store, 'id', '-')
    )

    today_date = _local_today()

    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
            period_days = (end_date - start_date).days + 1
        except (ValueError, TypeError):
            start_date = today_date
            end_date = today_date
            period_days = 1
    elif period == 'daily':
        start_date = today_date
        end_date = today_date
        period_days = 1
    elif period == 'weekly':
        start_date = today_date - timedelta(days=7)
        end_date = today_date
        period_days = 7
    elif period == 'two_weeks':
        start_date = today_date - timedelta(days=14)
        end_date = today_date
        period_days = 14
    elif period == 'monthly':
        start_date = today_date - timedelta(days=30)
        end_date = today_date
        period_days = 30
    else:
        return HttpResponse('Geçersiz periyot seçildi.', status=400)

    # ═══════════════════════════════════════════════════════════════
    # 2. YARDIMCI FONKSİYONLAR
    # ═══════════════════════════════════════════════════════════════
    FX_CODES_SET = {'USD', 'EUR', 'CAD', 'QAR', 'TRY', 'GBP', 'CHF', 'AUD', 'SAR'}

    def _fx_token_from_name(name):
        """Ürün adından döviz kodu çıkarır (ör: 'USDTRY Döviz' → 'USDTRY')."""
        if not name:
            return None
        nm = ''.join(ch for ch in name.upper() if ch.isalpha())
        if len(nm) >= 6:
            left, right = nm[:3], nm[3:6]
            if left in FX_CODES_SET and right in FX_CODES_SET:
                return left + right
        if len(nm) >= 3 and nm[:3] in FX_CODES_SET:
            return nm[:3]
        return None

    def classify_product(prod):
        """
        Ürünü 6 ana sınıfa ayırır. Öncelik sırası:
        1) HURDA  — is_scrap=True (milyem bazlı hurda havuzları)
        2) ZİYNET — kategori adı 'Ziynet' (Çeyrek, Yarım, Tam vb.)
        3) BİLEZİK — kategori adı 'Bilezik' içerir
        4) DÖVİZ  — FX ürünleri (döviz çifti veya yabancı para)
        5) ALTIN_HAS — price_currency/currency = 'HS' veya kategori 'Altın'
        6) BARKODLU — barkodu olan tekil ürünler
        7) DİĞER  — hiçbirine uymayan
        """
        if not prod:
            return 'DIGER', 'adet'

        cat_name = ''
        cat = getattr(prod, 'category', None)
        if cat:
            cat_name = (getattr(cat, 'name', '') or '').strip().lower()

        # 1. HURDA
        if getattr(prod, 'is_scrap', False):
            return 'HURDA', 'gr'

        # 2. ZİYNET
        if cat_name == 'ziynet':
            return 'ZIYNET', 'adet'

        # 3. BİLEZİK
        if 'bilezik' in cat_name:
            return 'BILEZIK', 'gr'

        # 4. DÖVİZ
        pc = str(getattr(prod, 'price_currency', '') or '')
        cc = str(getattr(prod, 'currency', '') or '')
        name_upper = (getattr(prod, 'name', '') or '').upper()
        fx_foreign = FX_CODES_SET - {'HS'}
        if (pc in fx_foreign) or (cc in fx_foreign) or _fx_token_from_name(name_upper):
            return 'DOVIZ', 'fx'

        # 5. ALTIN (HAS)
        if cat_name in ('altın', 'altin') or pc == 'HS' or cc == 'HS':
            return 'ALTIN_HAS', 'gr'

        # 6. BARKODLU
        barcode = (getattr(prod, 'barcode', '') or '').strip()
        if barcode:
            return 'BARKODLU', 'adet'

        # 7. DİĞER
        return 'DIGER', 'adet'

    def is_weight_unit(kind, prod=None):
        """Gram/miktar bazlı mı yoksa adet bazlı mı?"""
        if kind in ('ALTIN_HAS', 'BILEZIK', 'HURDA', 'DOVIZ'):
            return True
        if kind == 'ZIYNET' and prod and getattr(prod, 'is_gram_bullion', False):
            return True
        return False

    def currency_code_for_fx(prod):
        """FX ürünü için görüntüleme kodu (ör: USDTRY, EUR)."""
        if not prod:
            return 'FX'
        pc = str(getattr(prod, 'price_currency', '') or '')
        cc = str(getattr(prod, 'currency', '') or '')
        name = getattr(prod, 'name', '') or ''
        token = _fx_token_from_name(name)
        if token:
            return token
        fx_foreign = FX_CODES_SET - {'HS'}
        if pc in fx_foreign:
            return pc
        if cc in fx_foreign:
            return cc
        return 'FX'

    def decimals_for(kind):
        """Kategori bazlı ondalık hassasiyet: Gr→3, Adet→0, FX→2."""
        if kind in ('ZIYNET', 'BARKODLU', 'DIGER'):
            return 0
        if kind == 'DOVIZ':
            return 2
        return 3

    def product_label(kind, prod):
        """Raporda görünecek ürün adı / kodu."""
        if not prod:
            return '-'
        barcode = (getattr(prod, 'barcode', '') or '').strip()
        name = (getattr(prod, 'name', '') or '').strip() or f"Ürün-{str(getattr(prod, 'id', ''))[:8]}"
        if kind == 'DOVIZ':
            return currency_code_for_fx(prod)
        if kind == 'BARKODLU':
            return barcode or name
        return name

    def fmt_tr(x, d=2):
        """Türkçe sayı formatı: 1.234,567"""
        q = D(x)
        s = format(q, f',.{d}f') if d > 0 else format(q, ',.0f')
        return s.replace(',', '§').replace('.', ',').replace('§', '.')

    # ═══════════════════════════════════════════════════════════════
    # 3. MALİ ÖZET (Process tablosundan — TL bazlı)
    # ═══════════════════════════════════════════════════════════════
    process_qs = (
        Process.objects.filter(
            is_deleted=False,
            is_status='COMPLETED',
            date__date__gte=start_date,
            date__date__lte=end_date,
            store=store,
        )
    )
    total_sales_eur = D(process_qs.filter(transaction_type='SALE').aggregate(s=Sum('amount'))['s'])
    total_purchases_eur = D(process_qs.filter(
        transaction_type__in=['PURCHASE', 'RETURN']
    ).aggregate(s=Sum('amount'))['s'])
    net_total_tl = total_sales_eur - total_purchases_eur

    # ═══════════════════════════════════════════════════════════════
    # 4. STOK HAREKETLERİ — StockLedger (Immutable Ledger)
    # ═══════════════════════════════════════════════════════════════
    #
    # Adım A: Dönem içindeki hareketleri ürün bazlı topla
    # ─────────────────────────────────────────────────────
    ledger_period_qs = StockLedger.objects.filter(
        store=store,
        created_on__date__gte=start_date,
        created_on__date__lte=end_date,
    ).select_related('product', 'product__category')

    per_prod = {}
    label_order = {}
    idx = 0

    for entry in ledger_period_qs.iterator():
        prod = entry.product
        if not prod:
            continue
        pid = prod.id

        if pid not in per_prod:
            kind, unit = classify_product(prod)
            weight = is_weight_unit(kind, prod)
            lbl = product_label(kind, prod)
            per_prod[pid] = {
                'product': prod,
                'kind': kind,
                'unit': unit,
                'weight_based': weight,
                'label': lbl,
                'in_total': D('0'),
                'out_total': D('0'),
                'decimals': decimals_for(kind),
            }
            if lbl not in label_order:
                label_order[lbl] = idx
                idx += 1

        info = per_prod[pid]
        amt = D(entry.quantity_gram) if info['weight_based'] else D(entry.quantity_pieces)

        if entry.direction == StockLedger.Direction.IN:
            info['in_total'] += abs(amt)
        else:
            info['out_total'] += abs(amt)

    # ─────────────────────────────────────────────────────
    # Adım B: Tüm-zaman Ledger bakiyesi (Integrity kaynağı)
    # ─────────────────────────────────────────────────────
    product_ids = list(per_prod.keys())
    DEC_FIELD = DecimalField(max_digits=18, decimal_places=4)
    INT_FIELD = IntegerField()
    ZERO_DEC = Value(Decimal('0'), output_field=DEC_FIELD)
    ZERO_INT = Value(0, output_field=INT_FIELD)

    all_time_map = {}
    if product_ids:
        all_time_raw = (
            StockLedger.objects
            .filter(store=store, product_id__in=product_ids)
            .values('product_id')
            .annotate(
                all_in_gram=Coalesce(Sum(Case(
                    When(direction=StockLedger.Direction.IN, then='quantity_gram'),
                    default=ZERO_DEC,
                    output_field=DEC_FIELD,
                )), Decimal('0'), output_field=DEC_FIELD),
                all_out_gram=Coalesce(Sum(Case(
                    When(direction=StockLedger.Direction.OUT, then='quantity_gram'),
                    default=ZERO_DEC,
                    output_field=DEC_FIELD,
                )), Decimal('0'), output_field=DEC_FIELD),
                all_in_pcs=Coalesce(Sum(Case(
                    When(direction=StockLedger.Direction.IN, then='quantity_pieces'),
                    default=ZERO_INT,
                    output_field=INT_FIELD,
                )), Value(0), output_field=INT_FIELD),
                all_out_pcs=Coalesce(Sum(Case(
                    When(direction=StockLedger.Direction.OUT, then='quantity_pieces'),
                    default=ZERO_INT,
                    output_field=INT_FIELD,
                )), Value(0), output_field=INT_FIELD),
            )
        )
        for row in all_time_raw:
            all_time_map[row['product_id']] = {
                'in_gram': D(row['all_in_gram']),
                'out_gram': D(row['all_out_gram']),
                'in_pcs': D(row['all_in_pcs']),
                'out_pcs': D(row['all_out_pcs']),
            }

    # ─────────────────────────────────────────────────────
    # Adım C: Anlık Stok — StockSnapshot (cache tablosu)
    # ─────────────────────────────────────────────────────
    snap_map = {}
    if product_ids:
        snap_rows = (
            StockSnapshot.objects
            .filter(store=store, product_id__in=product_ids)
            .values('product_id')
            .annotate(pcs=Sum('stock_pieces'), wt=Sum('stock_gram'))
        )
        snap_map = {
            r['product_id']: {'pieces': D(r['pcs']), 'weight': D(r['wt'])}
            for r in snap_rows
        }

    # ═══════════════════════════════════════════════════════════════
    # 5. HESAPLAMA VE GRUPLAMA
    # ═══════════════════════════════════════════════════════════════
    #
    # Her ürün için:
    #   ledger_balance = Tüm-zaman SUM(IN) − SUM(OUT)   ← Gerçek kaynak
    #   opening        = ledger_balance − dönem_giren + dönem_çıkan
    #   expected       = ledger_balance  (= opening + giren − çıkan)
    #   actual         = StockSnapshot   ← Cache değeri
    #   fark           = actual − expected  → 0 ise Integrity ✓
    # ─────────────────────────────────────────────────────

    by_label = defaultdict(lambda: {
        'kind': None, 'unit': None, 'decimals': 0,
        'in_total': D('0'), 'out_total': D('0'),
        'opening': D('0'), 'expected': D('0'), 'actual': D('0'),
    })

    empty_at = {'in_gram': D('0'), 'out_gram': D('0'), 'in_pcs': D('0'), 'out_pcs': D('0')}
    empty_snap = {'pieces': D('0'), 'weight': D('0')}

    for pid, info in per_prod.items():
        at = all_time_map.get(pid, empty_at)
        snap = snap_map.get(pid, empty_snap)

        if info['weight_based']:
            ledger_balance = at['in_gram'] - at['out_gram']
            actual = snap['weight']
        else:
            ledger_balance = at['in_pcs'] - at['out_pcs']
            actual = snap['pieces']

        opening = ledger_balance - info['in_total'] + info['out_total']
        expected = ledger_balance

        lbl = info['label']
        blk = by_label[lbl]
        blk['kind'] = info['kind']
        blk['unit'] = info['unit']
        blk['decimals'] = max(blk['decimals'], info['decimals'])
        blk['in_total'] += info['in_total']
        blk['out_total'] += info['out_total']
        blk['opening'] += opening
        blk['expected'] += expected
        blk['actual'] += actual

    items = []
    for label in sorted(by_label.keys(), key=lambda k: label_order.get(k, 10 ** 9)):
        blk = by_label[label]
        decs = blk['decimals']
        diff = blk['actual'] - blk['expected']

        items.append({
            'kind': blk['kind'],
            'stok_kodu': label,
            'ilk_stok': fmt_tr(blk['opening'], decs),
            'giren': fmt_tr(blk['in_total'], decs),
            'cikan': fmt_tr(blk['out_total'], decs),
            'olmali': fmt_tr(blk['expected'], decs),
            'stok_durum': fmt_tr(blk['actual'], decs),
            'is_integrity_ok': (diff == D('0')),
        })

    # ─────────────────────────────────────────────────────
    # Kategori sırasına göre grupla
    # ─────────────────────────────────────────────────────
    GROUP_ORDER = ['ALTIN_HAS', 'ZIYNET', 'BILEZIK', 'HURDA', 'DOVIZ', 'BARKODLU', 'DIGER']
    GROUP_TITLES = {
        'ALTIN_HAS': 'ALTIN (HAS)',
        'ZIYNET': 'ZİYNET',
        'BILEZIK': 'BİLEZİK',
        'HURDA': 'HURDA',
        'DOVIZ': 'DÖVİZ',
        'BARKODLU': 'BARKODLU ÜRÜN',
        'DIGER': 'DİĞER',
    }

    grouped_rows = []
    for k in GROUP_ORDER:
        group_items = [it for it in items if it['kind'] == k]
        if group_items:
            grouped_rows.append({'is_group': True, 'group': GROUP_TITLES.get(k, k)})
            grouped_rows.extend([{**it, 'is_group': False} for it in group_items])

    # ═══════════════════════════════════════════════════════════════
    # 6. CONTEXT VE RESPONSE
    # ═══════════════════════════════════════════════════════════════
    now_loc = timezone.localtime(timezone.now()) if timezone.is_aware(timezone.now()) else timezone.now()
    report_no = f"STK-{now_loc.strftime('%Y%m%d%H%M')}"

    context = {
        'company_name': getattr(store, 'name', 'Kuyum Plus'),
        'company_subtitle': getattr(store, 'address', '') or '',
        'report_title': 'Güncel Rapor – Stok Durumu',
        'report_no': report_no,
        'period_text': f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        'generated_date': now_loc.date().strftime('%d/%m/%Y'),
        'generated_time': now_loc.strftime('%H:%M'),
        'print_datetime': now_loc.strftime('%d/%m/%Y %H:%M'),
        'authorized_person': (
            f"{request.user.first_name} {request.user.last_name}".strip()
            or request.user.username
        ),
        'period_days': period_days,
        'store_id': store_id_text,
        'rows': grouped_rows,
        'is_preview': request.GET.get('preview') == '1',
        'sum_total_sales_eur': fmt_tr(total_sales_eur),
        'sum_total_purchases_eur': fmt_tr(total_purchases_eur),
        'sum_net_total_tl': fmt_tr(net_total_tl),
        'is_net_negative': net_total_tl < 0,
    }

    if request.GET.get('download_pdf', '0') != '1':
        return render(request, 'management/dashboard/current_stock_report.html', context)

    html = render_to_string('management/dashboard/current_stock_report.html', context, request=request)
    pdf_buffer = io.BytesIO()
    pisa.CreatePDF(io.BytesIO(html.encode('utf-8')), dest=pdf_buffer)
    pdf_buffer.seek(0)
    resp = HttpResponse(pdf_buffer, content_type='application/pdf')
    resp['Content-Disposition'] = 'attachment; filename=Guncel_Stok_Raporu.pdf'
    return resp


@login_required(login_url='login')
@xframe_options_exempt
def generate_bank_balance_report(request):
    def D(val, default='0'):
        try:
            if val is None:
                return Decimal(default)
            return Decimal(str(val))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    def fmt_tr(x, d=2):
        q = D(x)
        s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    store = request.user.store

    store_id_text = str(
        getattr(store, "store_id", "")
        or getattr(store, "display_id", "")
        or getattr(store, "identity_no", "")
        or getattr(store, "id", "-")
    )

    today = _local_today()
    period = request.GET.get("period", "daily")

    if period == "daily":
        start_date, period_days = today, 1
    elif period == "weekly":
        start_date, period_days = today - timedelta(days=7), 7
    elif period == "monthly":
        start_date, period_days = today - timedelta(days=30), 30
    else:
        return HttpResponse("Geçersiz periyot seçildi.", status=400)

    try:
        installment = int(request.GET.get("installment", "1"))
        if installment not in (1, 3, 6, 9, 12):
            installment = 1
    except Exception:
        installment = 1

    active_bank_ids = (
        Payment.objects
        .filter(
            bank__store=store,
            date__date__gte=start_date,
            date__date__lte=today
        )
        .values_list('bank_id', flat=True)
        .distinct()
    )

    items = []
    total_in = D(0)
    total_out = D(0)

    total_cc_gross_in = D(0)
    total_cc_commission = D(0)
    total_cc_net_in = D(0)

    grouped_rows = []
    if items:
        grouped_rows.append({"is_group": True, "group": "BANKA HESAPLARI"})
        grouped_rows.extend(items)

    net_total_tl = total_in - total_out

    def _local_now():
        n = timezone.now()
        return timezone.localtime(n) if timezone.is_aware(n) else n

    now_loc = _local_now()

    context = {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Profesyonel Kuyum Yönetim Sistemi",
        "report_title": "Banka Bakiye Raporu",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {today.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "branch_name": getattr(store, "branch_name", getattr(store, "name", "-")),
        "authorized_person": (f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username),
        "period_days": period_days,

        "sum_total_sales_eur": fmt_tr(total_in, 2),
        "sum_total_purchases_eur": fmt_tr(total_out, 2),
        "sum_net_total_tl": fmt_tr(net_total_tl, 2),
        "is_net_negative": (net_total_tl < 0),

        "rows": grouped_rows,

        "company_address": getattr(store, "address", "") or "",
        "company_email": getattr(store, "email", "") or "",
        "company_contact": getattr(store, "phone", "") or "",
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),

        "store_id": store_id_text,
        "store_display_id": store_id_text,

        "cc_installment_used": installment,
        "cc_period_gross_in": fmt_tr(total_cc_gross_in, 2),
        "cc_period_est_commission": fmt_tr(total_cc_commission, 2),
        "cc_period_est_net_in": fmt_tr(total_cc_net_in, 2),
    }

    html = render_to_string("management/dashboard/bank_balance_report.html", context, request=request)
    pdf_buffer = io.BytesIO()
    pisa_status = pisa.CreatePDF(io.BytesIO(html.encode("utf-8")), dest=pdf_buffer)
    if pisa_status.err:
        print("PISA LOG >>>", pisa_status.log)
        return HttpResponse("PDF oluşturulurken hata oluştu", status=500)

    pdf_buffer.seek(0)
    resp = HttpResponse(pdf_buffer, content_type="application/pdf")
    resp["Content-Disposition"] = 'inline; filename=Banka_Bakiye_Raporu.pdf'
    return resp


@login_required(login_url='login')
@role_required('DASHBOARD_GENERATE_PROFIT_REPORT')
@xframe_options_exempt
def generate_profit_report(request):
    period = request.GET.get('period', 'daily')
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    report_type = request.GET.get('type', 'ziynet')
    user_store = request.user.store
    today_date = _local_today()

    # ─── 1. TARİH HESAPLAMA (Değişmedi) ─────────────────────
    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
            period_days = (end_date - start_date).days + 1
        except (ValueError, TypeError):
            start_date = today_date
            end_date = today_date
            period_days = 1
    elif period == 'daily':
        start_date, end_date, period_days = today_date, today_date, 1
    elif period == 'weekly':
        start_date, end_date, period_days = today_date - timedelta(days=7), today_date, 7
    elif period == 'two_weeks':
        start_date, end_date, period_days = today_date - timedelta(days=14), today_date, 14
    elif period == 'monthly':
        start_date, end_date, period_days = today_date - timedelta(days=30), today_date, 30
    else:
        return HttpResponse('Geçersiz periyot veya tarih seçildi.', status=400)

    # ─── 2. YARDIMCI FONKSİYONLAR ───────────────────────────
    def fmt_tr(val, d=2):
        try:
            q = Decimal(str(val if val is not None else "0"))
        except (InvalidOperation, ValueError, TypeError):
            q = Decimal("0")
        s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    # ─── 3. BASE QUERYSET ────────────────────────────────────
    base_qs = Process.objects.filter(
        is_deleted=False,
        store=user_store,
        is_status='COMPLETED',
        date__date__gte=start_date,
        date__date__lte=end_date,
        transaction_type__in=['SALE', 'ORDER_IN']
    ).select_related('product', 'product__category', 'employee', 'customer')

    # ─── 4. RAPOR TÜRÜNE GÖRE DALLANMA ──────────────────────
    report_title = "Satış Kâr Raporu"
    template_name = "management/dashboard/profit_report_ziynet.html"

    # ══════════════════════════════════════════════════════════
    #  HAS MERKEZLİ RAPORLAR  (Ziynet + Altın)
    # ══════════════════════════════════════════════════════════
    if report_type in ('ziynet', 'altin'):

        if report_type == 'ziynet':
            processes = base_qs.filter(
                product__category__name__icontains='Ziynet'
            )
            report_title = "Ziynet Satış Kâr Raporu"
            template_name = "management/dashboard/profit_report_ziynet.html"
        else:
            processes = base_qs.filter(
                Q(product__category__name__icontains='Altın') |
                Q(product__name__icontains='Has Altın')
            ).exclude(
                product__category__name__icontains='Ziynet'
            ).exclude(
                product__category__name__icontains='Bilezik'
            )
            report_title = "Altın / Sarrafiye Kâr Raporu"
            template_name = "management/dashboard/profit_report_gold.html"

        processes = processes.order_by('date')

        # ── Güncel Has Altın kuru (rapor sonundaki TL karşılığı için) ──
        hs_product = Products.objects.filter(
            name__icontains='Has Altın'
        ).only('sale_price_eur', 'buy_price_eur').first()

        guncel_has_kur = Decimal('0.00')
        if hs_product and hs_product.sale_price_eur:
            guncel_has_kur = Decimal(str(hs_product.sale_price_eur))

        # ── StockSnapshot cache: weighted_avg_cost_hs ─────────
        # Tek sorgu ile sayfadaki tüm ürünlerin ortalama maliyet Has'ını çek
        product_ids = list(
            processes.values_list('product_id', flat=True).distinct()
        )
        inv_map = {}
        if product_ids:
            for snap in StockSnapshot.objects.filter(
                    product_id__in=product_ids, store=user_store
            ).only('product_id', 'weighted_avg_cost_hs'):
                inv_map[snap.product_id] = snap.weighted_avg_cost_hs or Decimal('0.000')

        rows = []
        total_sale_hs = Decimal('0.000')
        total_cost_hs = Decimal('0.000')
        total_profit_hs = Decimal('0.000')

        for p in processes:
            # ── Miktar belirleme ──
            qty_val = Decimal(p.piece) if (p.piece and p.piece > 0) else Decimal(p.gram or 0)
            unit_label = "Ad" if (p.piece and p.piece > 0) else "Gr"

            if qty_val <= 0:
                continue

            # Miktar görüntüleme
            if unit_label == "Ad":
                qty_display = f"{int(qty_val)} {unit_label}"
            else:
                qty_display = f"{fmt_tr(qty_val, 2)} {unit_label}"


            satis_has_toplam = Decimal(str(p.price_hs or 0))

            birim_maliyet_has = inv_map.get(p.product_id, Decimal('0.000'))

            # Fallback: weighted yoksa ürün kartından al
            # FAZ 44 — 1.05 EŞİK KURALI:
            # Products.buy_price_hs iki çağdan veriyi karışık tutuyor (gold_purchases
            # form girişi TOPLAM, retail_views.py:1355 üzerine yazımı BİRİM). Burada
            # birim maliyet bekliyoruz; 1.05 üzerindeki değer legacy toplam demektir.
            if birim_maliyet_has <= Decimal('0') and p.product:
                raw_buy = Decimal(str(p.product.buy_price_hs or 0))
                prod_gram = Decimal(str(getattr(p.product, 'gram', 0) or 0))
                if raw_buy > Decimal('1.05') and prod_gram > Decimal('0'):
                    birim_maliyet_has = (raw_buy / prod_gram).quantize(
                        Decimal('0.0001'), rounding=ROUND_HALF_UP
                    )
                else:
                    birim_maliyet_has = raw_buy

            maliyet_has_toplam = (birim_maliyet_has * qty_val).quantize(
                Decimal('0.001'), rounding=ROUND_HALF_UP
            )

            kar_has = satis_has_toplam - maliyet_has_toplam

            # Birim satış Has
            birim_satis_has = (satis_has_toplam / qty_val).quantize(
                Decimal('0.001'), rounding=ROUND_HALF_UP
            ) if qty_val > 0 else Decimal('0.000')

            total_sale_hs += satis_has_toplam
            total_cost_hs += maliyet_has_toplam
            total_profit_hs += kar_has

            rows.append({
                "process_no": p.process_no,
                "datetime": timezone.localtime(p.date).strftime("%d/%m/%Y %H:%M"),
                "product": p.product.name if p.product else "-",
                "qty": qty_display,
                "customer": (
                    f"{p.customer.first_name} {p.customer.last_name}"
                    if p.customer else "-"
                ),
                "birim_maliyet_has": fmt_tr(birim_maliyet_has, 3),
                "birim_satis_has": fmt_tr(birim_satis_has, 3),
                "kar_has": fmt_tr(kar_has, 3),
                "is_profit_negative": kar_has < 0,
            })

        anlik_tl_karsiligi = Decimal('0.00')
        if guncel_has_kur > Decimal('0'):
            anlik_tl_karsiligi = (total_profit_hs * guncel_has_kur).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )

        store_id_text = str(
            getattr(user_store, "store_id", "")
            or getattr(user_store, "display_id", "")
            or getattr(user_store, "id", "-")
        )
        n = timezone.now()
        now_loc = timezone.localtime(n) if timezone.is_aware(n) else n

        context = {
            "company_name": getattr(user_store, "name", "Kuyum Plus"),
            "company_subtitle": "Kâr/Zarar Analiz Raporu",
            "report_title": report_title,
            "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
            "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
            "generated_date": now_loc.date().strftime("%d/%m/%Y"),
            "generated_time": now_loc.strftime("%H:%M"),
            "authorized_person": (
                    f"{request.user.first_name} {request.user.last_name}".strip()
                    or request.user.username
            ),
            "period_days": period_days,
            "store_id": store_id_text,
            "rows": rows,
            "sum_total_sale_hs": fmt_tr(total_sale_hs, 3),
            "sum_total_cost_hs": fmt_tr(total_cost_hs, 3),
            "sum_total_profit_hs": fmt_tr(total_profit_hs, 3),
            "is_total_profit_negative": total_profit_hs < 0,
            "guncel_has_kur": fmt_tr(guncel_has_kur, 2),
            "anlik_tl_karsiligi": fmt_tr(anlik_tl_karsiligi, 2),
            "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
            "is_preview": request.GET.get('preview') == '1',
        }

        download_pdf = request.GET.get('download_pdf', '0') == '1'
        if not download_pdf:
            return render(request, template_name, context)

        html_content = render_to_string(template_name, context, request=request)
        pdf_file = io.BytesIO()
        pisa_status = pisa.CreatePDF(
            io.BytesIO(html_content.encode("utf-8")), dest=pdf_file
        )
        if pisa_status.err:
            return HttpResponse("PDF oluşturulurken hata oluştu", status=500)
        pdf_file.seek(0)
        filename = f"{report_type}_Kar_Raporu.pdf"
        response = HttpResponse(pdf_file, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename={filename}'
        return response

    elif report_type == 'bilezik':
        processes = base_qs.filter(product__category__name__icontains='Bilezik')
        report_title = "Bilezik Satış Kâr Raporu"
        template_name = "management/dashboard/profit_report_bilezik.html"

    elif report_type == 'hurda':
        processes = base_qs.filter(
            Q(product__is_scrap=True) | Q(product__category__name__icontains='Hurda')
        )
        report_title = "Hurda Satış Kâr Raporu"
        template_name = "management/dashboard/profit_report_scrap.html"

    elif report_type == 'barkod':
        processes = base_qs.filter(
            product__barcode__isnull=False
        ).exclude(product__barcode='')
        report_title = "Barkodlu Ürün Satış Kâr Raporu"
        template_name = "management/dashboard/profit_report_barcode.html"

    # ════════════════════════════════════════════════════════════════════
    # FAZ 31 / BUG-5 — TÜM KATEGORİLER KÂR RAPORU (2026-05-01)
    # ════════════════════════════════════════════════════════════════════
    # Önceki davranış: Desteklenmeyen her type için base_qs.none() →
    #                  kullanıcı "Tümü" istediğinde boş rapor görüyordu.
    # Yeni davranış:   type='all' tüm SALE/ORDER_IN satırlarını birleşik
    #                  rapor olarak listeler (kategori filtresi yok).
    #                  Mevcut tipler (ziynet/altin/bilezik/hurda/barkod)
    #                  hiç değişmedi; eski URL'ler etkilenmez.
    # ════════════════════════════════════════════════════════════════════
    elif report_type == 'all':
        processes = base_qs
        report_title = "Tüm Kategoriler Kâr Raporu"
        template_name = "management/dashboard/profit_report.html"

    else:
        processes = base_qs.none()

    rows = []
    total_revenue = Decimal('0')
    total_cost = Decimal('0')
    total_profit = Decimal('0')
    processes = processes.order_by('date')

    for p in processes:
        sale_amount = Decimal(p.amount or 0)
        gross_profit = Decimal(p.gross_profit or 0)

        if gross_profit == Decimal('0') and sale_amount > Decimal('0'):
            maliyet_tl = Decimal('0')
            if getattr(p, 'cost_amount_eur', 0) and p.cost_amount_eur > 0:
                maliyet_tl = Decimal(str(p.cost_amount_eur))
            elif p.product and getattr(p.product, 'buy_price_hs', 0) > 0:
                kur = getattr(p, 'hs_rate_buy_eur', 0)
                if not kur or kur == 0:
                    kur = getattr(p, 'hs_rate_sale_eur', 0)
                if kur and Decimal(str(kur)) > Decimal('0'):
                    # FAZ 44 — 1.05 EŞİK KURALI:
                    # Products.buy_price_hs gold_purchases/perakende karışık tutuyor.
                    #   - 1.05 üzeri  → legacy TOPLAM HS (× kur = toplam TL)
                    #   - 1.05 ve altı → BİRİM HS (× kur × qty = toplam TL)
                    raw_buy = Decimal(str(p.product.buy_price_hs))
                    qty_for_cost = Decimal(p.piece) if (p.piece and p.piece > 0) else Decimal(p.gram or 0)
                    if raw_buy > Decimal('1.05'):
                        maliyet_tl = raw_buy * Decimal(str(kur))
                    elif qty_for_cost > Decimal('0'):
                        maliyet_tl = raw_buy * Decimal(str(kur)) * qty_for_cost
            if maliyet_tl > Decimal('0'):
                gross_profit = sale_amount - maliyet_tl

        try:
            cost_amount_eur = (
                Decimal(str(p.cost_amount_eur))
                if getattr(p, 'cost_amount_eur', None)
                else Decimal('0')
            )
        except (InvalidOperation, ValueError, TypeError):
            cost_amount_eur = Decimal('0')
        if cost_amount_eur == 0:
            cost_amount_eur = sale_amount - gross_profit

        try:
            cost_amount_hs = (
                Decimal(str(p.cost_amount_hs)).quantize(Decimal('0.001'))
                if getattr(p, 'cost_amount_hs', None)
                else Decimal('0')
            )
        except (InvalidOperation, ValueError, TypeError):
            cost_amount_hs = Decimal('0')

        if cost_amount_hs == Decimal('0') and cost_amount_eur > Decimal('0'):
            kur = getattr(p, 'hs_rate_buy_eur', 0)
            if not kur or kur == 0:
                kur = getattr(p, 'hs_rate_sale_eur', 0)
            if kur and Decimal(str(kur)) > Decimal('0'):
                cost_amount_hs = (
                        cost_amount_eur / Decimal(str(kur))
                ).quantize(Decimal('0.001'))

        try:
            sell_hs = (
                Decimal(str(p.price_hs)).quantize(Decimal('0.001'))
                if p.price_hs else Decimal('0')
            )
        except (InvalidOperation, ValueError, TypeError):
            sell_hs = Decimal('0')

        qty_val = Decimal(p.piece) if p.piece > 0 else Decimal(p.gram or 0)
        unit_label = "Ad" if p.piece > 0 else "Gr"
        qty_display = f"{fmt_tr(qty_val, 0 if p.piece > 0 else 2)} {unit_label}"

        unit_cost = (cost_amount_eur / qty_val) if qty_val > 0 else Decimal('0')
        unit_cost_hs = (cost_amount_hs / qty_val) if qty_val > 0 else Decimal('0')
        unit_sell_hs = (sell_hs / qty_val) if qty_val > 0 else Decimal('0')

        total_revenue += sale_amount
        total_cost += cost_amount_eur
        total_profit += gross_profit

        rows.append({
            "process_no": p.process_no,
            "datetime": timezone.localtime(p.date).strftime("%d/%m/%Y %H:%M"),
            "product": p.product.name if p.product else "-",
            "barcode": (
                p.product.barcode
                if p.product and p.product.barcode else "-"
            ),
            "qty": qty_display,
            "unit_cost": fmt_tr(unit_cost, 2),
            "unit_sale": fmt_tr(p.unit_price, 2),
            "total_sale": fmt_tr(sale_amount, 2),
            "profit": fmt_tr(gross_profit, 2),
            "is_profit_negative": gross_profit < 0,
            "customer": (
                f"{p.customer.first_name} {p.customer.last_name}"
                if p.customer else "-"
            ),
            "unit_cost_hs": fmt_tr(unit_cost_hs, 3),
            "unit_sell_hs": fmt_tr(unit_sell_hs, 3),
        })

    store_id_text = str(
        getattr(user_store, "store_id", "")
        or getattr(user_store, "display_id", "")
        or getattr(user_store, "id", "-")
    )
    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n

    context = {
        "company_name": getattr(user_store, "name", "Kuyum Plus"),
        "company_subtitle": "Kâr/Zarar Analiz Raporu",
        "report_title": report_title,
        "period_text": (
            f"{start_date.strftime('%d/%m/%Y')} - "
            f"{end_date.strftime('%d/%m/%Y')}"
        ),
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "authorized_person": (
                f"{request.user.first_name} {request.user.last_name}".strip()
                or request.user.username
        ),
        "period_days": period_days,
        "store_id": store_id_text,
        "rows": rows,
        "sum_total_revenue": fmt_tr(total_revenue, 2),
        "sum_total_cost": fmt_tr(total_cost, 2),
        "sum_total_profit": fmt_tr(total_profit, 2),
        "is_total_profit_negative": total_profit < 0,
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
        "is_preview": request.GET.get('preview') == '1',
    }

    download_pdf = request.GET.get('download_pdf', '0') == '1'
    if not download_pdf:
        return render(request, template_name, context)

    html_content = render_to_string(template_name, context, request=request)
    pdf_file = io.BytesIO()
    pisa_status = pisa.CreatePDF(
        io.BytesIO(html_content.encode("utf-8")), dest=pdf_file
    )
    if pisa_status.err:
        return HttpResponse("PDF oluşturulurken hata oluştu", status=500)
    pdf_file.seek(0)
    filename = f"{report_type}_Kar_Raporu.pdf"
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename={filename}'
    return response


@login_required(login_url='login')
@role_required('DASHBOARD_GENERATE_CUSTOMER_REPORT')
@xframe_options_exempt
def generate_customer_report(request):
    from apps.dashboard.tasks import _build_customer_report_context, _parse_date_params

    period = request.GET.get('period', 'daily')
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    today_date = _local_today()
    start_date, end_date, period_days = _parse_date_params(start_date_param, end_date_param, period, today_date)
    if start_date is None:
        return HttpResponse('Geçersiz periyot veya tarih seçildi.', status=400)

    store = request.user.store
    context = _build_customer_report_context(store, request.user, start_date, end_date, period_days)
    context['is_preview'] = request.GET.get('preview') == '1'

    if request.GET.get('download_pdf', '0') != '1':
        return render(request, "management/dashboard/customer_report.html", context)

    html_content = render_to_string("management/dashboard/customer_report.html", context, request=request)
    pdf_file = io.BytesIO()
    pisa_status = pisa.CreatePDF(io.BytesIO(html_content.encode("utf-8")), dest=pdf_file)
    if pisa_status.err:
        return HttpResponse("PDF oluşturulurken hata oluştu", status=500)
    pdf_file.seek(0)
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename=Musteri_Cari_Raporu.pdf'
    return response


@login_required(login_url='login')
@role_required('DASHBOARD_GENERATE_CUSTOMER_DETAIL_REPORT')
@xframe_options_exempt
def generate_customer_detail_report(request):
    """
    Tek bir müşteriye özel detaylı rapor.
    Tüm işlemler, ödemeler, borç/alacak durumu, bakiye analizi.
    """

    customer_id = request.GET.get('customer_id')
    if not customer_id:
        return HttpResponse('Müşteri ID parametresi eksik.', status=400)

    period = request.GET.get('period', 'monthly')
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    today_date = _local_today()

    start_date, end_date, period_days = _parse_date_params(
        start_date_param, end_date_param, period, today_date
    )
    if start_date is None:
        return HttpResponse('Geçersiz periyot veya tarih seçildi.', status=400)

    store = request.user.store

    try:
        customer = Customers.objects.get(
            pk=customer_id, store=store, is_deleted=False
        )
    except Customers.DoesNotExist:
        return HttpResponse('Müşteri bulunamadı.', status=404)

    context = _build_customer_detail_report_context(
        store, request.user, customer, start_date, end_date, period_days
    )
    context['is_preview'] = request.GET.get('preview') == '1'

    template_name = "management/dashboard/customer_detail_report.html"

    if request.GET.get('download_pdf', '0') != '1':
        return render(request, template_name, context)

    html_content = render_to_string(template_name, context, request=request)
    pdf_file = io.BytesIO()
    pisa_status = pisa.CreatePDF(
        io.BytesIO(html_content.encode("utf-8")), dest=pdf_file
    )
    if pisa_status.err:
        return HttpResponse("PDF oluşturulurken hata oluştu", status=500)

    pdf_file.seek(0)
    safe_name = f"{customer.first_name}_{customer.last_name}".replace(' ', '_')
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename=Musteri_Detay_{safe_name}.pdf'
    return response


@login_required(login_url='login')
def get_customers_for_report(request):
    store = request.user.store
    search = request.GET.get('q', '').strip()

    qs = Customers.objects.filter(
        store=store, is_deleted=False, is_active=True
    ).values('id', 'first_name', 'last_name', 'phone', 'customer_number')

    if search:
        qs = qs.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(phone__icontains=search) |
            Q(customer_number__icontains=search)
        )

    qs = qs.order_by('first_name', 'last_name')[:50]

    data = []
    for c in qs:
        full_name = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip()
        label = full_name
        if c['phone']:
            label += f" ({c['phone']})"
        data.append({
            'id': str(c['id']),
            'text': label,
            'name': full_name,
            'phone': c['phone'] or '',
            'customer_number': c['customer_number'] or '',
        })

    return JsonResponse({'results': data})



@login_required(login_url='login')
def queue_report(request):
    """
    Rapor üretimini Celery kuyruğuna ekler, task_id döner.
    Parametreler: report_type, start_date, end_date, period, type (profit için)
    """
    from apps.dashboard.models import GeneratedReports
    from apps.dashboard.tasks import generate_report_task

    report_type = request.GET.get('report_type') or request.POST.get('report_type')
    if not report_type:
        return JsonResponse({'error': True, 'message': 'report_type gerekli'}, status=400)

    start_date = request.GET.get('start_date') or request.POST.get('start_date', '')
    end_date = request.GET.get('end_date') or request.POST.get('end_date', '')
    period = request.GET.get('period') or request.POST.get('period', 'daily')
    profit_type = request.GET.get('type') or request.POST.get('type', 'ziynet')

    customer_id_param = request.GET.get('customer_id') or request.POST.get('customer_id', '')

    result = generate_report_task.delay(
        report_type=report_type,
        user_id=request.user.id,
        start_date_str=start_date,
        end_date_str=end_date,
        period=period,
        profit_type=profit_type,
        customer_id=customer_id_param,
    )

    task_id = result.id
    GeneratedReports.objects.update_or_create(
        task_id=task_id,
        defaults={'report_type': report_type, 'status': 'PENDING'}
    )

    return JsonResponse({'task_id': task_id, 'message': 'Rapor sıraya alındı.'})


@login_required(login_url='login')
def check_report_status(request, task_id):
    from apps.dashboard.models import GeneratedReports

    try:
        rec = GeneratedReports.objects.get(task_id=task_id)
    except GeneratedReports.DoesNotExist:
        return JsonResponse({'status': 'PENDING', 'message': 'Kayıt bulunamadı.'})

    if rec.status == 'SUCCESS' and rec.file:
        file_url = rec.file.url
        return JsonResponse({
            'status': 'SUCCESS',
            'file_url': request.build_absolute_uri(file_url),
            'message': 'Rapor hazır.',
        })
    elif rec.status == 'FAILED':
        return JsonResponse({
            'status': 'FAILED',
            'error': rec.error_message or 'Rapor oluşturulamadı.',
        })
    else:
        return JsonResponse({
            'status': 'PENDING',
            'message': 'Rapor hazırlanıyor...',
        })


# ============================================================================
# FAZ R-2: ENVANETER DEĞER RAPORU API (StockSnapshot → JSON)
# ============================================================================

@login_required(login_url='login')
def api_inventory_value(request):
    """
    Anlık envanter değer raporu: StockSnapshot'tan kategori bazlı stok değeri.
    Response time: < 100ms (tek aggregate sorgusu).
    """
    from apps.stock_management.services.price_service import PriceService

    store = request.user.store

    # Kategori bazlı stok değeri (tek sorgu)
    stock_data = (
        StockSnapshot.objects
        .filter(store=store, stock_gram__gt=0)
        .select_related('product', 'product__category')
        .values(
            'product__category__name',
        )
        .annotate(
            total_gram=Sum('stock_gram'),
            total_pieces=Sum('stock_pieces'),
            total_wac_tl=Sum(
                F('stock_gram') * F('weighted_avg_cost_eur'),
                output_field=DecimalField(),
            ),
            total_wac_hs=Sum(
                F('stock_gram') * F('weighted_avg_cost_hs'),
                output_field=DecimalField(),
            ),
        )
        .order_by('-total_wac_tl')
    )

    # Anlık Has Altın kuru
    try:
        has_price = PriceService.get_price('GOLD_24K')
        has_kur = has_price.get('sell_tl', Decimal('0'))
    except Exception:
        has_kur = Decimal('0')

    categories = []
    grand_total_wac_tl = Decimal('0')
    grand_total_market_tl = Decimal('0')

    for row in stock_data:
        wac_tl = row['total_wac_tl'] or Decimal('0')
        wac_hs = row['total_wac_hs'] or Decimal('0')
        market_tl = (wac_hs * has_kur) if has_kur > 0 else Decimal('0')
        paper_profit = market_tl - wac_tl

        grand_total_wac_tl += wac_tl
        grand_total_market_tl += market_tl

        categories.append({
            'category': row['product__category__name'] or 'Kategorisiz',
            'total_gram': str(row['total_gram'] or 0),
            'total_pieces': row['total_pieces'] or 0,
            'wac_cost_tl': str(wac_tl.quantize(Decimal('0.01'))),
            'wac_cost_hs': str((wac_hs or Decimal('0')).quantize(Decimal('0.0001'))),
            'market_value_tl': str(market_tl.quantize(Decimal('0.01'))),
            'paper_profit_tl': str(paper_profit.quantize(Decimal('0.01'))),
            'paper_profit_pct': str(
                ((paper_profit / wac_tl * 100).quantize(Decimal('0.01'))
                 if wac_tl > 0 else Decimal('0'))
            ),
        })

    return JsonResponse({
        'categories': categories,
        'grand_total_wac_tl': str(grand_total_wac_tl.quantize(Decimal('0.01'))),
        'grand_total_market_tl': str(grand_total_market_tl.quantize(Decimal('0.01'))),
        'grand_total_paper_profit': str(
            (grand_total_market_tl - grand_total_wac_tl).quantize(Decimal('0.01'))
        ),
        'has_altin_kur': str(has_kur),
    })


# ============================================================================
# FAZ R-4: PERSONEL PERFORMANS RAPORU API
# ============================================================================

@login_required(login_url='login')
def api_employee_performance(request):
    """
    Personel performans raporu: DailyEmployeeReport rollup'tan.
    Tarih aralığı filtreli, < 50ms.
    """
    from apps.dashboard.services import get_employee_performance
    from apps.dashboard.tasks import _parse_date_params

    store = request.user.store
    period = request.GET.get('period', 'daily')
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    today_date = _local_today()

    start_date, end_date, period_days = _parse_date_params(
        start_date_param, end_date_param, period, today_date
    )
    if start_date is None:
        return JsonResponse({'error': 'Geçersiz tarih'}, status=400)

    perf_data = get_employee_performance(store, start_date, end_date)

    employees = []
    for row in perf_data:
        first = row.get('employee__first_name', '') or ''
        last = row.get('employee__last_name', '') or ''
        name = f"{first} {last}".strip() or 'Bilinmeyen'
        avg_sale = (
            (row['total_sales_eur'] / row['total_sale_count']).quantize(Decimal('0.01'))
            if row['total_sale_count'] > 0 else Decimal('0')
        )

        employees.append({
            'employee_id': str(row['employee_id']),
            'name': name,
            'sale_count': row['total_sale_count'],
            'total_sales_eur': str(row['total_sales_eur']),
            'total_sales_hs': str(row['total_sales_hs']),
            'total_gross_profit': str(row['total_gross_profit']),
            'purchase_count': row['total_purchase_count'],
            'total_purchases_eur': str(row['total_purchases_eur']),
            'transaction_count': row['total_transaction_count'],
            'avg_sale_tl': str(avg_sale),
        })

    return JsonResponse({
        'period': f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        'employees': employees,
    })


# ============================================================================
# FAZ R-4: TEDARİKÇİ CARİ HESAP ÖZETİ API
# ============================================================================

@login_required(login_url='login')
def api_supplier_ledger(request):
    """
    Tedarikçi cari hesap özeti: Tedarikçi bazında alış/ödeme/bakiye.
    """
    store = request.user.store
    supplier_id = request.GET.get('supplier_id')

    if not supplier_id:
        # Tüm tedarikçilerin özeti
        supplier_data = (
            Process.objects
            .filter(
                store=store,
                is_deleted=False,
                is_status='COMPLETED',
                transaction_type='PURCHASE',
                supplier__isnull=False,
            )
            .values('supplier_id', 'supplier__company_name')
            .annotate(
                total_purchases_eur=Coalesce(Sum('amount'), Decimal('0'), output_field=DecimalField()),
                total_purchases_hs=Coalesce(Sum('price_hs'), Decimal('0'), output_field=DecimalField()),
                purchase_count=Count('id'),
            )
            .order_by('-total_purchases_eur')
        )

        suppliers = []
        for row in supplier_data:
            suppliers.append({
                'supplier_id': str(row['supplier_id']),
                'company_name': row['supplier__company_name'] or '-',
                'total_purchases_eur': str(row['total_purchases_eur']),
                'total_purchases_hs': str(row['total_purchases_hs']),
                'purchase_count': row['purchase_count'],
            })

        return JsonResponse({'suppliers': suppliers})

    # Tek tedarikçi detay (tarih sıralı hareket listesi)
    period = request.GET.get('period', 'monthly')
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    today_date = _local_today()
    from apps.dashboard.tasks import _parse_date_params
    start_date, end_date, _ = _parse_date_params(
        start_date_param, end_date_param, period, today_date
    )
    if start_date is None:
        start_date = today_date - timedelta(days=90)
        end_date = today_date

    processes = (
        Process.objects
        .filter(
            store=store,
            is_deleted=False,
            is_status='COMPLETED',
            supplier_id=supplier_id,
            date__date__gte=start_date,
            date__date__lte=end_date,
        )
        .values(
            'process_no', 'date', 'transaction_type',
            'amount', 'price_hs', 'product__name',
        )
        .order_by('date')
    )

    movements = []
    cumulative_tl = Decimal('0')
    for p in processes:
        amt = p['amount'] or Decimal('0')
        if p['transaction_type'] == 'PURCHASE':
            cumulative_tl += amt
        elif p['transaction_type'] in ('RETURN', 'PAYMENT'):
            cumulative_tl -= amt

        movements.append({
            'process_no': p['process_no'] or '-',
            'date': timezone.localtime(p['date']).strftime('%d/%m/%Y %H:%M') if p['date'] else '-',
            'transaction_type': p['transaction_type'],
            'amount_eur': str(amt),
            'amount_hs': str(p['price_hs'] or Decimal('0')),
            'product': p['product__name'] or '-',
            'cumulative_balance_eur': str(cumulative_tl),
        })

    return JsonResponse({
        'supplier_id': supplier_id,
        'period': f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        'movements': movements,
        'final_balance_eur': str(cumulative_tl),
    })


# ============================================================================
# FAZ R-5: TEK EKRAN DASHBOARD KPI API
# ============================================================================

@login_required(login_url='login')
def api_dashboard_kpi(request):
    """
    FAZ R-5: Tek ekran dashboard için birleştirilmiş KPI endpoint'i.
    Tüm verileri tek HTTP çağrısında döner.

    İçerik:
        - Bugünkü KPI'lar (DailyStoreReport rollup'tan)
        - Haftalık trend (son 7 gün grafik verisi)
        - Anlık stok değeri (StockSnapshot'tan)
        - Bekleyen mutabakat sayısı (Payment'tan)
        - Personel mini-listesi (DailyEmployeeReport'tan)
    """
    from apps.dashboard.services import get_dashboard_summary, get_date_range_summary, get_employee_performance
    from apps.dashboard.models import DailyStoreReport
    from apps.stock_management.services.price_service import PriceService

    store = request.user.store
    today = _local_today()

    # ── 1. Bugünkü KPI'lar (rollup'tan, < 5ms) ──
    kpi = get_dashboard_summary(store, today)

    # ── 2. Haftalık trend (son 7 gün) ──
    week_start = today - timedelta(days=6)
    weekly_data = (
        DailyStoreReport.objects
        .filter(store=store, report_date__gte=week_start, report_date__lte=today)
        .values('report_date', 'total_sales_eur', 'total_purchases_eur', 'total_gross_profit')
        .order_by('report_date')
    )
    weekly_trend = []
    for row in weekly_data:
        weekly_trend.append({
            'date': row['report_date'].strftime('%d/%m'),
            'sales': str(row['total_sales_eur']),
            'purchases': str(row['total_purchases_eur']),
            'profit': str(row['total_gross_profit']),
        })

    # ── 3. Anlık stok değeri + Has kuru ──
    try:
        has_price = PriceService.get_price('GOLD_24K')
        has_kur_sell = str(has_price.get('sell_tl', Decimal('0')))
        has_kur_buy = str(has_price.get('buy_tl', Decimal('0')))
    except Exception:
        has_kur_sell = '0'
        has_kur_buy = '0'

    # ── 4. Bekleyen mutabakat sayısı (hızlı COUNT sorgusu) ──
    pending_reconciliation = (
        Payment.objects
        .filter(
            process_group__store=store,
            reconciliation_status='PENDING',
            is_cancelled=False,
        )
        .count()
    )

    # ── 5. Bugünkü personel mini-listesi ──
    today_employees = get_employee_performance(store, today, today)
    employee_list = []
    for emp in today_employees[:10]:  # İlk 10 personel
        first = emp.get('employee__first_name', '') or ''
        last = emp.get('employee__last_name', '') or ''
        employee_list.append({
            'name': f"{first} {last}".strip() or 'Bilinmeyen',
            'sale_count': emp['total_sale_count'],
            'total_sales_eur': str(emp['total_sales_eur']),
            'total_gross_profit': str(emp['total_gross_profit']),
        })

    # ── 6. Kasa dağılımı ──
    kasa = {
        'cash': {
            'in': str(kpi['cash_in']),
            'out': str(kpi['cash_out']),
            'net': str(kpi['cash_in'] - kpi['cash_out']),
        },
        'card': {
            'in': str(kpi['card_in']),
            'out': str(kpi['card_out']),
            'net': str(kpi['card_in'] - kpi['card_out']),
        },
        'transfer': {
            'in': str(kpi['transfer_in']),
            'out': str(kpi['transfer_out']),
            'net': str(kpi['transfer_in'] - kpi['transfer_out']),
        },
    }

    return JsonResponse({
        # Bugünkü KPI
        'today': {
            'total_sales_eur': str(kpi['total_sales_eur']),
            'total_sales_hs': str(kpi['total_sales_hs']),
            'total_purchases_eur': str(kpi['total_purchases_eur']),
            'total_purchases_hs': str(kpi['total_purchases_hs']),
            'total_gross_profit': str(kpi['total_gross_profit']),
            'total_net_profit': str(kpi['total_net_profit']),
            'sale_count': kpi['sale_count'],
            'purchase_count': kpi['purchase_count'],
            'transaction_count': kpi['transaction_count'],
            'unique_customers': kpi['unique_customers'],
            'net_cash_flow': str(kpi['net_cash_flow']),
            'commission_total': str(kpi['commission_total']),
        },
        # Stok
        'stock': {
            'value_tl': str(kpi['stock_value_eur']),
            'value_hs': str(kpi['stock_value_hs']),
            'has_kur_sell': has_kur_sell,
            'has_kur_buy': has_kur_buy,
        },
        # Kasa dağılımı
        'kasa': kasa,
        # Haftalık trend
        'weekly_trend': weekly_trend,
        # Mutabakat
        'pending_reconciliation': pending_reconciliation,
        # Personel
        'employees': employee_list,
        # Meta
        'computed_at': kpi.get('computed_at'),
        'report_date': today.isoformat(),
    })


# ============================================================================
# FAZ R-5: TARİH ARALIKLI RAPOR ÖZETİ API
# ============================================================================

@login_required(login_url='login')
def api_date_range_summary(request):
    """
    Tarih aralığı bazında toplam KPI raporu.
    DailyStoreReport rollup'larından aggregate — milisaniyeler.
    """
    from apps.dashboard.services import get_date_range_summary
    from apps.dashboard.tasks import _parse_date_params

    store = request.user.store
    period = request.GET.get('period', 'monthly')
    start_date_param = request.GET.get('start_date')
    end_date_param = request.GET.get('end_date')
    today_date = _local_today()

    start_date, end_date, period_days = _parse_date_params(
        start_date_param, end_date_param, period, today_date
    )
    if start_date is None:
        return JsonResponse({'error': 'Geçersiz tarih'}, status=400)

    summary = get_date_range_summary(store, start_date, end_date)

    # Decimal -> str dönüşümü (JSON serializable)
    result = {}
    for key, val in summary.items():
        result[key] = str(val) if isinstance(val, Decimal) else val

    result['period'] = f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}"
    result['period_days'] = period_days

    return JsonResponse(result)
