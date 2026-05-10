# apps/dashboard/tasks.py
import io
import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from collections import defaultdict

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.db.models import Sum, Q
from xhtml2pdf import pisa

from apps.process.models import Process, Payment
from apps.customers.models import Customers
from apps.products.models import Products
# --- FAZ 4: StockSnapshot entegrasyonu (Inventories yerine) ---
from apps.stock_management.models import StockSnapshot
from apps.definitions.categories.models import Categories
from apps.accounts.models import Users
from apps.dashboard.models import GeneratedReports
from django.core.files.base import ContentFile

font_path = os.path.join(settings.BASE_DIR, "static", "management", "fonts", "DejaVuSansCustom.ttf")
if os.path.exists(font_path):
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from xhtml2pdf.default import DEFAULT_FONT

        font_name = "DejaVuSansCustom"
        pdfmetrics.registerFont(TTFont(font_name, font_path))
        DEFAULT_FONT['helvetica'] = font_name
        DEFAULT_FONT['Times-Roman'] = font_name
    except Exception:
        pass

FX_CODES = {'USD', 'EUR', 'CAD', 'QAR', 'TRY', 'GBP', 'CHF', 'AUD', 'SAR'}


def _dec(val, q='0.01'):
    try:
        return Decimal(str(val if val is not None else "0")).quantize(Decimal(q))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _fmt_tr(val, d=2):
    try:
        q = Decimal(str(val if val is not None else "0"))
    except (InvalidOperation, ValueError, TypeError):
        q = Decimal("0")
    s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _parse_date_params(start_date_param, end_date_param, period, today_date):
    if start_date_param and end_date_param:
        try:
            start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
            period_days = (end_date - start_date).days + 1
        except (ValueError, TypeError):
            start_date = end_date = today_date
            period_days = 1
    elif period == 'daily':
        start_date = end_date = today_date
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
        return None, None, None
    return start_date, end_date, period_days


def _build_process_report_context(store, user, start_date, end_date, period_days):
    processes = (
        Process.objects
        .filter(is_deleted=False, store=store, is_status='COMPLETED',
                date__date__gte=start_date, date__date__lte=end_date)
        .select_related('employee', 'customer', 'product')
        .order_by('date')
    )
    total_sales = processes.filter(transaction_type='SALE').aggregate(Sum('amount'))['amount__sum'] or 0
    total_purchases = processes.filter(transaction_type__in=['PURCHASE', 'RETURN']).aggregate(Sum('amount'))[
                          'amount__sum'] or 0
    total_sale_has = processes.filter(transaction_type='SALE').aggregate(Sum('price_hs'))['price_hs__sum'] or 0
    net_total = Decimal(str(total_sales)) - Decimal(str(total_purchases))

    def fmt_dt(dt):
        return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M") if timezone.is_aware(dt) else dt.strftime(
            "%d/%m/%Y %H:%M")

    rows = []
    for p in processes:
        rows.append({
            "process_no": p.process_no,
            "datetime": fmt_dt(p.date),
            "customer": f"{getattr(p.customer, 'first_name', '-') or '-'} {getattr(p.customer, 'last_name', '-') or '-'}" if p.customer else "-",
            "product": p.product.name if p.product else "-",
            "tx_type": p.get_transaction_type_display() if hasattr(p,
                                                                   'get_transaction_type_display') else p.transaction_type,
            "piece": _fmt_tr(p.piece or 0, 0),
            "unit_price": _fmt_tr(p.unit_price or 0, 2),
            "total": _fmt_tr(p.amount or 0, 2),
            "has": _fmt_tr(p.price_hs or 0, 3),
        })

    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
    store_id_text = str(getattr(store, "store_id", "") or getattr(store, "display_id", "") or getattr(store, "id", "-"))

    return {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Profesyonel Kuyum Yönetim Sistemi",
        "report_title": "İşlem Raporu",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "branch_name": getattr(store, "branch_name", getattr(store, "name", "-")),
        "authorized_person": (f"{user.first_name} {user.last_name}".strip() or user.username),
        "period_days": period_days,
        "sum_total_sales_eur": _fmt_tr(total_sales, 2),
        "sum_total_purchases_eur": _fmt_tr(total_purchases, 2),
        "sum_total_sales_has": _fmt_tr(total_sale_has, 3),
        "sum_net_total_tl": _fmt_tr(net_total, 2),
        "is_net_negative": net_total < 0,
        "rows": rows,
        "company_address": getattr(store, "address", ""),
        "company_email": getattr(store, "email", ""),
        "company_contact": getattr(store, "phone", ""),
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
        "store_id": store_id_text,
        "store_display_id": store_id_text,
        "is_preview": False,
    }


def _build_profit_report_context(store, user, start_date, end_date, period_days, report_type):
    from django.db.models import OuterRef, Subquery, Q
    from apps.stock_management.models import StockSnapshot
    from apps.chambers.models import ChamberProductPrice
    from apps.settings.models import StoreConfiguration
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    from apps.process.models import Process
    from apps.products.models import Products
    from django.utils import timezone

    config = StoreConfiguration.objects.filter(store=store).first()
    use_manual_has = config.use_manual_has_calculation if config else False
    active_chamber_id = config.active_pricing_chamber_id if config else None

    def fmt_tr(val, d=2):
        try:
            q = Decimal(str(val if val is not None else "0"))
        except (InvalidOperation, ValueError, TypeError):
            q = Decimal("0")
        s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    # 2. Temel Sorgu
    base_qs = Process.objects.filter(
        is_deleted=False, store=store, is_status='COMPLETED',
        date__date__gte=start_date, date__date__lte=end_date,
        transaction_type__in=['SALE', 'ORDER_IN']
    ).select_related('product', 'product__category', 'employee', 'customer')

    if report_type in ('ziynet', 'altin'):
        if report_type == 'ziynet':
            processes = base_qs.filter(product__category__name__icontains='Ziynet')
            report_title = "Ziynet Satış Kâr Raporu"
            template_name = "management/dashboard/profit_report_ziynet.html"
        else:
            processes = base_qs.filter(
                Q(product__category__name__icontains='Altın') | Q(product__name__icontains='Has Altın')
            ).exclude(product__category__name__icontains='Ziynet').exclude(product__category__name__icontains='Bilezik')
            report_title = "Altın / Sarrafiye Kâr Raporu"
            template_name = "management/dashboard/profit_report_gold.html"

        inv_sq_weighted = StockSnapshot.objects.filter(product=OuterRef('product'), store=store).values(
            'weighted_avg_cost_hs')[:1]
        inv_sq_custom_buy = StockSnapshot.objects.filter(product=OuterRef('product'), store=store).values(
            'custom_buy_price_hs')[:1]
        inv_sq_use_custom = StockSnapshot.objects.filter(product=OuterRef('product'), store=store).values(
            'use_custom_pricing')[:1]
        chamber_sq_buy = ChamberProductPrice.objects.filter(chamber_id=active_chamber_id,
                                                            product=OuterRef('product')).values('buy_price_hs')[:1]

        processes = processes.annotate(
            inv_weighted_buy=Subquery(inv_sq_weighted),
            inv_custom_buy=Subquery(inv_sq_custom_buy),
            inv_use_custom=Subquery(inv_sq_use_custom),
            chamber_buy=Subquery(chamber_sq_buy),
        ).order_by('date')

        rows = []
        total_sale_hs = Decimal('0')
        total_cost_hs = Decimal('0')
        total_profit_hs = Decimal('0')

        for p in processes:
            qty_val = Decimal(p.piece) if p.piece > 0 else Decimal(p.gram or 0)
            unit_label = "Ad" if p.piece > 0 else "Gr"
            qty_display = f"{fmt_tr(qty_val, 0 if p.piece > 0 else 2)} {unit_label}"
            if qty_val <= Decimal('0'): qty_val = Decimal('1')

            try:
                sell_hs = Decimal(str(p.price_hs)).quantize(Decimal('0.001')) if p.price_hs else Decimal('0')
            except (InvalidOperation, ValueError, TypeError):
                sell_hs = Decimal('0')

            birim_satis_has = (sell_hs / qty_val) if qty_val > 0 else Decimal('0')
            birim_maliyet_has = Decimal('0')
            inv_weighted = Decimal(str(p.inv_weighted_buy)) if getattr(p, 'inv_weighted_buy', None) else Decimal('0')

            if inv_weighted > Decimal('0'):
                birim_maliyet_has = inv_weighted
            else:
                if use_manual_has and getattr(p, 'inv_use_custom', False) and getattr(p, 'inv_custom_buy',
                                                                                      None) is not None:
                    birim_maliyet_has = Decimal(str(p.inv_custom_buy))
                elif active_chamber_id and getattr(p, 'chamber_buy', None) is not None:
                    birim_maliyet_has = Decimal(str(p.chamber_buy))
                elif p.product and getattr(p.product, 'buy_price_hs', None):
                    # FAZ 44 — 1.05 EŞİK KURALI:
                    # Products.buy_price_hs iki çağdan veriyi karışık tutuyor:
                    #   - gold_purchases formundan giriş: TOPLAM maliyet (örn. 10.575)
                    #   - retail_views.py:1355 üzerine yazıyor: BİRİM maliyet (örn. 0.705)
                    # Burada birim maliyet bekliyoruz; 1.05 üzerindeki değerler
                    # legacy "toplam" demektir → ürünün gramına bölerek normalize et.
                    raw_buy = Decimal(str(p.product.buy_price_hs))
                    prod_gram = Decimal(str(getattr(p.product, 'gram', 0) or 0))
                    if raw_buy > Decimal('1.05') and prod_gram > Decimal('0'):
                        birim_maliyet_has = (raw_buy / prod_gram).quantize(
                            Decimal('0.0001'), rounding=ROUND_HALF_UP
                        )
                    else:
                        birim_maliyet_has = raw_buy

            if birim_maliyet_has == Decimal('0'):
                cost_amount_eur = Decimal(str(p.cost_amount_eur)) if getattr(p, 'cost_amount_eur', None) else Decimal('0')
                if cost_amount_eur == Decimal('0'):
                    sale_amount = Decimal(str(p.amount)) if getattr(p, 'amount', None) else Decimal('0')
                    gross_profit = Decimal(str(p.gross_profit)) if getattr(p, 'gross_profit', None) else Decimal('0')
                    cost_amount_eur = sale_amount - gross_profit

                if cost_amount_eur > Decimal('0'):
                    kur = getattr(p, 'hs_rate_buy_eur', 0) or getattr(p, 'hs_rate_sale_eur', 0)
                    if kur and Decimal(str(kur)) > Decimal('0'):
                        birim_maliyet_has = (cost_amount_eur / Decimal(str(kur))) / qty_val

            birim_maliyet_has = birim_maliyet_has.quantize(Decimal('0.001'))
            cost_amount_hs = (birim_maliyet_has * qty_val).quantize(Decimal('0.001'))
            kar_has = sell_hs - cost_amount_hs

            total_sale_hs += sell_hs
            total_cost_hs += cost_amount_hs
            total_profit_hs += kar_has

            rows.append({
                "process_no": p.process_no,
                "datetime": p.date.strftime("%d/%m/%Y %H:%M"),
                "product": p.product.name if p.product else "-",
                "qty": qty_display,
                "customer": f"{p.customer.first_name} {p.customer.last_name}" if p.customer else "-",
                "birim_maliyet_has": fmt_tr(birim_maliyet_has, 3),
                "birim_satis_has": fmt_tr(birim_satis_has, 3),
                "kar_has": fmt_tr(kar_has, 3),
                "is_profit_negative": kar_has < 0,
            })

        has_product = Products.objects.filter(name__icontains='Has Altın').first()
        guncel_has_kur = Decimal(str(has_product.buy_price_eur)) if has_product and getattr(has_product, 'buy_price_eur',
                                                                                           None) else Decimal('0')
        anlik_tl_karsiligi = total_profit_hs * guncel_has_kur

        n = timezone.now()
        now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
        store_id_text = str(
            getattr(store, "store_id", "") or getattr(store, "display_id", "") or getattr(store, "id", "-"))

        return {
            "template_name": template_name,
            "company_name": getattr(store, "name", "Kuyum Plus"),
            "company_subtitle": "Kâr/Zarar Analiz Raporu",
            "report_title": report_title,
            "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
            "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
            "generated_date": now_loc.date().strftime("%d/%m/%Y"),
            "generated_time": now_loc.strftime("%H:%M"),
            "authorized_person": (f"{user.first_name} {user.last_name}".strip() or user.username),
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
            "is_preview": False,
        }

    # ==========================================================
    # DİĞER RAPORLAR (Bilezik, Hurda, Barkod - TL MANTIĞI)
    # ==========================================================
    else:
        if report_type == 'bilezik':
            processes = base_qs.filter(product__category__name__icontains='Bilezik')
            report_title = "Bilezik Satış Kâr Raporu"
            template_name = "management/dashboard/profit_report_bilezik.html"
        elif report_type == 'hurda':
            processes = base_qs.filter(Q(product__is_scrap=True) | Q(product__category__name__icontains='Hurda'))
            report_title = "Hurda Satış Kâr Raporu"
            template_name = "management/dashboard/profit_report_scrap.html"
        elif report_type == 'barkod':
            processes = base_qs.filter(product__barcode__isnull=False).exclude(product__barcode='')
            report_title = "Barkodlu Ürün Satış Kâr Raporu"
            template_name = "management/dashboard/profit_report_barcode.html"
        else:
            processes = base_qs.none()
            report_title = "Satış Kâr Raporu"
            template_name = "management/dashboard/profit_report_ziynet.html"

        processes = processes.order_by('date')
        rows = []
        total_revenue = Decimal('0')
        total_cost = Decimal('0')
        total_profit = Decimal('0')

        for p in processes:
            sale_amount = Decimal(p.amount or 0)
            gross_profit = Decimal(p.gross_profit or 0)

            if gross_profit == Decimal('0') and sale_amount > Decimal('0'):
                maliyet_tl = Decimal('0')
                if getattr(p, 'cost_amount_eur', 0) and p.cost_amount_eur > 0:
                    maliyet_tl = Decimal(str(p.cost_amount_eur))
                elif p.product and getattr(p.product, 'buy_price_hs', 0) > 0:
                    kur = getattr(p, 'hs_rate_buy_eur', 0)
                    if not kur or kur == 0: kur = getattr(p, 'hs_rate_sale_eur', 0)
                    if kur and Decimal(str(kur)) > Decimal('0'):
                        maliyet_tl = Decimal(str(p.product.buy_price_hs)) * Decimal(str(kur))
                if maliyet_tl > Decimal('0'):
                    gross_profit = sale_amount - maliyet_tl

            cost_amount_eur = Decimal(str(p.cost_amount_eur)) if getattr(p, 'cost_amount_eur', None) else Decimal('0')
            if cost_amount_eur == Decimal('0'):
                cost_amount_eur = sale_amount - gross_profit

            qty_val = Decimal(p.piece) if p.piece > 0 else Decimal(p.gram or 0)
            unit_label = "Ad" if p.piece > 0 else "Gr"
            qty_display = f"{fmt_tr(qty_val, 0 if p.piece > 0 else 2)} {unit_label}"

            unit_cost = (cost_amount_eur / qty_val) if qty_val > 0 else Decimal('0')

            total_revenue += sale_amount
            total_cost += cost_amount_eur
            total_profit += gross_profit

            rows.append({
                "process_no": p.process_no,
                "datetime": p.date.strftime("%d/%m/%Y %H:%M"),
                "product": p.product.name if p.product else "-",
                "barcode": p.product.barcode if p.product and p.product.barcode else "-",
                "qty": qty_display,
                "unit_cost": fmt_tr(unit_cost, 2),
                "unit_sale": fmt_tr(p.unit_price, 2),
                "total_sale": fmt_tr(sale_amount, 2),
                "profit": fmt_tr(gross_profit, 2),
                "is_profit_negative": gross_profit < 0,
                "customer": f"{p.customer.first_name} {p.customer.last_name}" if p.customer else "-",
            })

        n = timezone.now()
        now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
        store_id_text = str(
            getattr(store, "store_id", "") or getattr(store, "display_id", "") or getattr(store, "id", "-"))

        return {
            "template_name": template_name,
            "company_name": getattr(store, "name", "Kuyum Plus"),
            "company_subtitle": "Kâr/Zarar Analiz Raporu",
            "report_title": report_title,
            "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
            "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
            "generated_date": now_loc.date().strftime("%d/%m/%Y"),
            "generated_time": now_loc.strftime("%H:%M"),
            "authorized_person": (f"{user.first_name} {user.last_name}".strip() or user.username),
            "period_days": period_days,
            "store_id": store_id_text,
            "rows": rows,
            "sum_total_revenue": fmt_tr(total_revenue, 2),
            "sum_total_cost": fmt_tr(total_cost, 2),
            "sum_total_profit": fmt_tr(total_profit, 2),
            "is_total_profit_negative": total_profit < 0,
            "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
            "is_preview": False,
        }


def _build_customer_report_context(store, user, start_date, end_date, period_days):
    """Müşteri cari raporu context'i."""
    customers = Customers.objects.filter(store=store, is_deleted=False)

    rows = []
    for c in customers:
        payable_hs = _dec(c.payable_hs, '0.001')
        receivable_hs = _dec(c.receivable_hs, '0.001')
        net_hs = receivable_hs - payable_hs
        sales = Process.objects.filter(store=store, customer=c, transaction_type='SALE', is_deleted=False,
                                       date__date__gte=start_date, date__date__lte=end_date).aggregate(s=Sum('amount'))[
                    's'] or Decimal('0')
        purchases = Process.objects.filter(store=store, customer=c, transaction_type='PURCHASE', is_deleted=False,
                                           date__date__gte=start_date, date__date__lte=end_date).aggregate(
            s=Sum('amount'))['s'] or Decimal('0')

        full_name = f"{getattr(c, 'first_name', '') or ''} {getattr(c, 'last_name', '') or ''}".strip() or "-"
        rows.append({
            "customer": full_name,
            "payable_hs": _fmt_tr(payable_hs, 3),
            "receivable_hs": _fmt_tr(receivable_hs, 3),
            "total_sales": _fmt_tr(sales, 2),
            "total_purchases": _fmt_tr(purchases, 2),
            "net_balance_hs": _fmt_tr(net_hs, 3),
            "_net_hs_raw": net_hs,
        })

    rows.sort(key=lambda x: abs(x["_net_hs_raw"]), reverse=True)
    for r in rows:
        del r["_net_hs_raw"]
    top_debt = rows[:10] if len(rows) >= 10 else rows

    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
    store_id_text = str(getattr(store, "store_id", "") or getattr(store, "display_id", "") or getattr(store, "id", "-"))

    return {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Müşteri Cari Raporu",
        "report_title": "Müşteri Cari ve Bakiye Özeti",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "authorized_person": (f"{user.first_name} {user.last_name}".strip() or user.username),
        "period_days": period_days,
        "store_id": store_id_text,
        "rows": rows,
        "top_debt": top_debt,
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
        "is_preview": False,
    }


def _render_pdf(template_name, context):
    """HTML'den PDF üretir."""
    html = render_to_string(template_name, context)
    pdf_buffer = io.BytesIO()
    pisa.CreatePDF(io.BytesIO(html.encode("utf-8")), dest=pdf_buffer)
    pdf_buffer.seek(0)
    return pdf_buffer


def _build_customer_detail_report_context(store, user, customer, start_date, end_date, period_days):
    """
    Tek bir müşteriye özel detaylı rapor context'i.
    İçerik:
      - Müşteri kimlik bilgileri
      - Has Altın bakiye özeti (borç/alacak)
      - Tüm işlemler tablosu (satış, alış, iade)
      - Ödeme geçmişi tablosu
      - Açık (ödenmemiş) işlemler tablosu
      - Özet istatistikler
    """
    from apps.process.models import Process, Payment
    from apps.products.models import Products
    from django.db.models import Sum, Q, OuterRef, Subquery, DecimalField
    from django.db.models.functions import Coalesce

    # ─── 1. MÜŞTERİ BİLGİLERİ ──────────────────────────────
    customer_info = {
        "full_name": f"{customer.first_name or ''} {customer.last_name or ''}".strip() or "-",
        "customer_number": customer.customer_number or "-",
        "phone": customer.phone or "-",
        "email": customer.email or "-",
        "identification_number": customer.identification_number or "-",
        "address": customer.address or "-",
        "city": str(customer.city) if customer.city else "-",
        "district": str(customer.district) if customer.district else "-",
    }

    # ─── 2. BAKİYE DURUMU ───────────────────────────────────
    receivable_hs = _dec(customer.receivable_hs, '0.001')
    payable_hs = _dec(customer.payable_hs, '0.001')
    net_balance_hs = receivable_hs - payable_hs  # (+) alacak, (-) borç

    hs_product = Products.objects.filter(
        name__icontains='Has Altın'
    ).only('sale_price_eur', 'buy_price_eur').first()

    guncel_has_kur = Decimal('0.00')
    if hs_product and hs_product.sale_price_eur:
        guncel_has_kur = _dec(hs_product.sale_price_eur)

    receivable_tl = receivable_hs * guncel_has_kur
    payable_tl = payable_hs * guncel_has_kur
    net_balance_eur = net_balance_hs * guncel_has_kur

    if net_balance_hs > Decimal('0.001'):
        balance_status = "ALACAKLI"
        balance_status_desc = "Müşterinin mağazadan alacağı var"
    elif net_balance_hs < Decimal('-0.001'):
        balance_status = "BORÇLU"
        balance_status_desc = "Müşterinin mağazaya borcu var"
    else:
        balance_status = "DENGEDE"
        balance_status_desc = "Bakiye sıfır"

    balance_info = {
        "receivable_hs": _fmt_tr(receivable_hs, 3),
        "payable_hs": _fmt_tr(payable_hs, 3),
        "net_balance_hs": _fmt_tr(abs(net_balance_hs), 3),
        "receivable_tl": _fmt_tr(receivable_tl, 2),
        "payable_tl": _fmt_tr(payable_tl, 2),
        "net_balance_eur": _fmt_tr(abs(net_balance_eur), 2),
        "is_net_negative": net_balance_hs < 0,
        "balance_status": balance_status,
        "balance_status_desc": balance_status_desc,
        "guncel_has_kur": _fmt_tr(guncel_has_kur, 2),
    }

    # ─── 3. TÜM İŞLEMLER ───────────────────────────────────
    ENTRY_SET = {'PURCHASE', 'STOCK_IN', 'RETURN', 'ORDER_IN'}
    TX_LABELS = {
        'SALE': 'Satış',
        'PURCHASE': 'Alış',
        'RETURN': 'İade',
        'STOCK_IN': 'Stok Giriş',
        'ORDER_IN': 'Sipariş',
        'PAYMENT': 'Ödeme',
    }

    processes_qs = (
        Process.objects
        .filter(
            customer=customer,
            store=store,
            is_deleted=False,
            is_status='COMPLETED',
            date__date__gte=start_date,
            date__date__lte=end_date,
        )
        .select_related('product', 'employee')
        .order_by('-date')
    )

    # Paid subquery — her process_no için toplam ödenen tutarı bul
    paid_subquery = (
        Payment.objects
        .filter(process_no=OuterRef('process_no'))
        .values('process_no')
        .annotate(total_paid=Sum('amount'))
        .values('total_paid')
    )

    processes_annotated = processes_qs.annotate(
        paid_amount=Coalesce(
            Subquery(paid_subquery, output_field=DecimalField()),
            Decimal('0.00')
        )
    )

    # İstatistikler
    total_sale_count = 0
    total_purchase_count = 0
    total_sale_tl = Decimal('0')
    total_purchase_tl = Decimal('0')
    total_sale_hs = Decimal('0')
    total_purchase_hs = Decimal('0')

    process_rows = []
    unpaid_rows = []

    for p in processes_annotated:
        tx_type = p.transaction_type or ''
        tx_label = TX_LABELS.get(tx_type, tx_type)
        is_entry = tx_type in ENTRY_SET

        amount = _dec(p.amount)
        price_hs = _dec(p.price_hs, '0.001')
        paid = _dec(getattr(p, 'paid_amount', 0))
        remaining_tl = amount - paid

        # Has cinsinden kalan borç hesapla
        remaining_hs = Decimal('0.000')
        if price_hs > Decimal('0.001') and amount > 0:
            remaining_hs = _dec(price_hs * (remaining_tl / amount), '0.001')

        # Miktar
        if p.gram and p.gram > 0:
            qty_display = f"{_fmt_tr(p.gram, 2)} gr"
        elif p.piece and p.piece > 0:
            qty_display = f"{int(p.piece)} ad"
        else:
            qty_display = "-"

        product_name = p.product.name if p.product else "-"
        employee_name = (
            f"{p.employee.first_name or ''} {p.employee.last_name or ''}".strip()
            if p.employee else "-"
        )

        # İstatistik toplama
        if tx_type == 'SALE':
            total_sale_count += 1
            total_sale_tl += amount
            total_sale_hs += price_hs
        elif tx_type in ('PURCHASE', 'RETURN'):
            total_purchase_count += 1
            total_purchase_tl += amount
            total_purchase_hs += price_hs

        row = {
            "process_no": p.process_no,
            "datetime": p.date.strftime("%d/%m/%Y %H:%M") if p.date else "-",
            "date_short": p.date.strftime("%d/%m/%Y") if p.date else "-",
            "tx_type": tx_label,
            "tx_code": tx_type,
            "is_entry": is_entry,
            "product": product_name,
            "qty": qty_display,
            "unit_price": _fmt_tr(p.unit_price or 0, 2),
            "amount": _fmt_tr(amount, 2),
            "price_hs": _fmt_tr(price_hs, 3),
            "paid": _fmt_tr(paid, 2),
            "remaining_tl": _fmt_tr(abs(remaining_tl), 2),
            "remaining_hs": _fmt_tr(abs(remaining_hs), 3),
            "employee": employee_name,
            "is_fully_paid": abs(remaining_tl) < Decimal('0.01'),
        }
        process_rows.append(row)

        # Ödenmemiş işlemler (kalan > 0.01 TL)
        if abs(remaining_tl) > Decimal('0.01') and tx_type != 'PAYMENT':
            unpaid_rows.append(row)

    # ─── 4. ÖDEME GEÇMİŞİ ──────────────────────────────────
    # Müşteriye ait tüm process_no'ları bul
    customer_process_nos = list(
        Process.objects
        .filter(customer=customer, store=store)
        .values_list('process_no', flat=True)
        .distinct()
    )

    payments_qs = (
        Payment.objects
        .filter(
            process_no__in=customer_process_nos,
            date__date__gte=start_date,
            date__date__lte=end_date,
        )
        .order_by('-date')
    )

    PAYMENT_LABELS = {
        'CASH': 'Nakit',
        'CREDIT_CARD': 'Kredi Kartı',
        'TRANSFER': 'Havale/EFT',
    }

    payment_rows = []
    total_payment_in = Decimal('0')
    total_payment_out = Decimal('0')
    payment_by_type = {
        'CASH': {'in': Decimal('0'), 'out': Decimal('0')},
        'CREDIT_CARD': {'in': Decimal('0'), 'out': Decimal('0')},
        'TRANSFER': {'in': Decimal('0'), 'out': Decimal('0')},
    }

    for pay in payments_qs:
        pay_amount = _dec(pay.amount)
        pay_label = PAYMENT_LABELS.get(pay.payment_type, pay.payment_type or 'Diğer')
        direction = "Çıkış" if pay.is_output else "Giriş"

        if pay.is_output:
            total_payment_out += pay_amount
        else:
            total_payment_in += pay_amount

        if pay.payment_type in payment_by_type:
            if pay.is_output:
                payment_by_type[pay.payment_type]['out'] += pay_amount
            else:
                payment_by_type[pay.payment_type]['in'] += pay_amount

        payment_rows.append({
            "process_no": pay.process_no,
            "datetime": pay.date.strftime("%d/%m/%Y %H:%M") if pay.date else "-",
            "payment_type": pay_label,
            "direction": direction,
            "is_output": pay.is_output,
            "amount": _fmt_tr(pay_amount, 2),
            "reference": getattr(pay, 'reference', '') or '',
        })

    net_payment = total_payment_in - total_payment_out

    payment_summary = {
        "total_in": _fmt_tr(total_payment_in, 2),
        "total_out": _fmt_tr(total_payment_out, 2),
        "net_payment": _fmt_tr(abs(net_payment), 2),
        "is_net_out": net_payment < 0,
        "cash_in": _fmt_tr(payment_by_type['CASH']['in'], 2),
        "cash_out": _fmt_tr(payment_by_type['CASH']['out'], 2),
        "card_in": _fmt_tr(payment_by_type['CREDIT_CARD']['in'], 2),
        "card_out": _fmt_tr(payment_by_type['CREDIT_CARD']['out'], 2),
        "transfer_in": _fmt_tr(payment_by_type['TRANSFER']['in'], 2),
        "transfer_out": _fmt_tr(payment_by_type['TRANSFER']['out'], 2),
    }

    # ─── 5. ÖZET İSTATİSTİKLER ─────────────────────────────
    net_tl = total_sale_tl - total_purchase_tl

    statistics = {
        "total_process_count": len(process_rows),
        "total_sale_count": total_sale_count,
        "total_purchase_count": total_purchase_count,
        "total_sale_tl": _fmt_tr(total_sale_tl, 2),
        "total_purchase_tl": _fmt_tr(total_purchase_tl, 2),
        "net_tl": _fmt_tr(abs(net_tl), 2),
        "is_net_tl_negative": net_tl < 0,
        "total_sale_hs": _fmt_tr(total_sale_hs, 3),
        "total_purchase_hs": _fmt_tr(total_purchase_hs, 3),
        "net_hs": _fmt_tr(abs(total_sale_hs - total_purchase_hs), 3),
        "is_net_hs_negative": (total_sale_hs - total_purchase_hs) < 0,
        "total_payment_count": len(payment_rows),
        "unpaid_count": len(unpaid_rows),
    }

    # ─── 6. CONTEXT ─────────────────────────────────────────
    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
    store_id_text = str(
        getattr(store, "store_id", "")
        or getattr(store, "display_id", "")
        or getattr(store, "id", "-")
    )

    return {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Müşteri Detay Raporu",
        "report_title": f"Müşteri Detay Raporu — {customer_info['full_name']}",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "authorized_person": (
                f"{user.first_name} {user.last_name}".strip() or user.username
        ),
        "period_days": period_days,
        "store_id": store_id_text,

        # Müşteri
        "customer_info": customer_info,
        "balance_info": balance_info,

        # Tablolar
        "process_rows": process_rows,
        "payment_rows": payment_rows,
        "unpaid_rows": unpaid_rows,

        # Özetler
        "payment_summary": payment_summary,
        "statistics": statistics,

        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
        "is_preview": False,
    }


def _build_currency_report_context(store, user, start_date, end_date, period_days):
    from apps.process.models import Process
    from decimal import Decimal, InvalidOperation
    from collections import defaultdict
    from django.db.models import Q
    from django.utils import timezone

    FX_CODES = {'USD', 'EUR', 'CAD', 'QAR', 'TRY', 'GBP', 'CHF', 'AUD', 'SAR'}

    def D(val, q='0'):
        try:
            return Decimal(str(val if val is not None else q))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(q)

    def fmt_tr(val, d=2):
        q = D(val)
        s = format(q, f",.{d}f") if d > 0 else format(q, ",.0f")
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    def full_name(obj):
        if not obj: return '-'
        fn = getattr(obj, 'first_name', '') or ''
        ln = getattr(obj, 'last_name', '') or ''
        val = f"{fn} {ln}".strip()
        return val if val else getattr(obj, 'username', '-') if hasattr(obj, 'username') else '-'

    qs_base = Process.objects.filter(
        is_deleted=False, is_status='COMPLETED',
        date__date__gte=start_date, date__date__lte=end_date, store=store,
    ).select_related('employee', 'customer', 'product').order_by('date')

    regex = r'^(' + '|'.join(sorted(FX_CODES)) + r')\b'
    fx_q = Q(product__price_currency__in=FX_CODES) | Q(product__currency__in=FX_CODES) | Q(product__name__iregex=regex)
    qs = qs_base.filter(fx_q)

    total_buy_tl = Decimal('0')
    total_sell_tl = Decimal('0')
    by_currency = defaultdict(
        lambda: {'BUY': {'fx': Decimal('0'), 'tl': Decimal('0')}, 'SELL': {'fx': Decimal('0'), 'tl': Decimal('0')}})
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
                for c in FX_CODES:
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
            "datetime": p.date.strftime("%d/%m/%Y %H:%M"),
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
            "currency": cur, "buy_fx": fmt_tr(buy_fx, 2), "sell_fx": fmt_tr(sell_fx, 2),
            "net_fx": fmt_tr(sell_fx - buy_fx, 2), "buy_tl": fmt_tr(buy_tl, 2),
            "sell_tl": fmt_tr(sell_tl, 2), "net_tl": fmt_tr(sell_tl - buy_tl, 2),
        })

    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
    store_id_text = str(getattr(store, "store_id", "") or getattr(store, "display_id", "") or getattr(store, "id", "-"))

    return {
        "company_name": getattr(store, "name", "Kuyum Plus"),
        "company_subtitle": "Profesyonel Kuyum Yönetim Sistemi",
        "report_title": "Döviz İşlem Raporu",
        "period_text": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        "report_no": now_loc.strftime("%Y%m%d%H%M%S"),
        "generated_date": now_loc.date().strftime("%d/%m/%Y"),
        "generated_time": now_loc.strftime("%H:%M"),
        "branch_name": getattr(store, "branch_name", getattr(store, "name", "-")),
        "authorized_person": full_name(user),
        "period_days": period_days,
        "sum_total_sales_eur": fmt_tr(total_sell_tl, 2),
        "sum_total_purchases_eur": fmt_tr(total_buy_tl, 2),
        "sum_net_total_tl": fmt_tr(net_tl, 2),
        "is_net_negative": (net_tl < 0),
        "rows": rows,
        "currency_summary": currency_summary,
        "print_datetime": now_loc.strftime("%d/%m/%Y %H:%M"),
        "store_id": store_id_text,
        "is_preview": False
    }


def _build_current_stock_report_context(store, user, start_date, end_date, period_days):
    from apps.process.models import Process
    from apps.stock_management.models import StockLedger, StockSnapshot
    from django.db.models import Sum, Case, When, Value, DecimalField, IntegerField
    from django.db.models.functions import Coalesce
    from decimal import Decimal, InvalidOperation
    from collections import defaultdict
    from django.utils import timezone

    def D(val, default='0'):
        try:
            if val is None: return Decimal(default)
            return Decimal(str(val))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    def fmt_tr(x, d=2):
        q = D(x)
        s = format(q, f',.{d}f') if d > 0 else format(q, ',.0f')
        return s.replace(',', '§').replace('.', ',').replace('§', '.')

    FX_CODES_SET = {'USD', 'EUR', 'CAD', 'QAR', 'TRY', 'GBP', 'CHF', 'AUD', 'SAR'}

    def _fx_token_from_name(name):
        if not name: return None
        nm = ''.join(ch for ch in name.upper() if ch.isalpha())
        if len(nm) >= 6:
            left, right = nm[:3], nm[3:6]
            if left in FX_CODES_SET and right in FX_CODES_SET: return left + right
        if len(nm) >= 3 and nm[:3] in FX_CODES_SET: return nm[:3]
        return None

    def classify_product(prod):
        if not prod: return 'DIGER', 'adet'
        cat_name = getattr(getattr(prod, 'category', None), 'name', '').strip().lower()
        if getattr(prod, 'is_scrap', False): return 'HURDA', 'gr'
        if cat_name == 'ziynet': return 'ZIYNET', 'adet'
        if 'bilezik' in cat_name: return 'BILEZIK', 'gr'
        pc = str(getattr(prod, 'price_currency', '') or '')
        cc = str(getattr(prod, 'currency', '') or '')
        name_upper = (getattr(prod, 'name', '') or '').upper()
        fx_foreign = FX_CODES_SET - {'HS'}
        if (pc in fx_foreign) or (cc in fx_foreign) or _fx_token_from_name(name_upper): return 'DOVIZ', 'fx'
        if cat_name in ('altın', 'altin') or pc == 'HS' or cc == 'HS': return 'ALTIN_HAS', 'gr'
        if (getattr(prod, 'barcode', '') or '').strip(): return 'BARKODLU', 'adet'
        return 'DIGER', 'adet'

    def is_weight_unit(kind, prod=None):
        if kind in ('ALTIN_HAS', 'BILEZIK', 'HURDA', 'DOVIZ'): return True
        if kind == 'ZIYNET' and prod and getattr(prod, 'is_gram_bullion', False): return True
        return False

    def decimals_for(kind):
        if kind in ('ZIYNET', 'BARKODLU', 'DIGER'): return 0
        if kind == 'DOVIZ': return 2
        return 3

    def product_label(kind, prod):
        if not prod: return '-'
        barcode = (getattr(prod, 'barcode', '') or '').strip()
        name = (getattr(prod, 'name', '') or '').strip() or f"Ürün-{str(getattr(prod, 'id', ''))[:8]}"
        if kind == 'BARKODLU': return barcode or name
        return name

    process_qs = Process.objects.filter(is_deleted=False, is_status='COMPLETED', date__date__gte=start_date,
                                        date__date__lte=end_date, store=store)
    total_sales_eur = D(process_qs.filter(transaction_type='SALE').aggregate(s=Sum('amount'))['s'])
    total_purchases_eur = D(
        process_qs.filter(transaction_type__in=['PURCHASE', 'RETURN']).aggregate(s=Sum('amount'))['s'])
    net_total_tl = total_sales_eur - total_purchases_eur

    ledger_period_qs = StockLedger.objects.filter(store=store, created_on__date__gte=start_date,
                                                  created_on__date__lte=end_date).select_related('product',
                                                                                                 'product__category')
    per_prod, label_order, idx = {}, {}, 0

    for entry in ledger_period_qs.iterator():
        prod = entry.product
        if not prod: continue
        pid = prod.id
        if pid not in per_prod:
            kind, unit = classify_product(prod)
            weight = is_weight_unit(kind, prod)
            lbl = product_label(kind, prod)
            per_prod[pid] = {
                'product': prod, 'kind': kind, 'unit': unit, 'weight_based': weight, 'label': lbl,
                'in_total': D('0'), 'out_total': D('0'), 'decimals': decimals_for(kind),
            }
            if lbl not in label_order: label_order[lbl] = idx; idx += 1
        info = per_prod[pid]
        amt = D(entry.quantity_gram) if info['weight_based'] else D(entry.quantity_pieces)
        if entry.direction == StockLedger.Direction.IN:
            info['in_total'] += abs(amt)
        else:
            info['out_total'] += abs(amt)

    product_ids = list(per_prod.keys())
    DEC_FIELD = DecimalField(max_digits=18, decimal_places=4)
    INT_FIELD = IntegerField()
    ZERO_DEC = Value(Decimal('0'), output_field=DEC_FIELD)
    ZERO_INT = Value(0, output_field=INT_FIELD)

    all_time_map = {}
    if product_ids:
        all_time_raw = StockLedger.objects.filter(store=store, product_id__in=product_ids).values(
            'product_id').annotate(
            all_in_gram=Coalesce(
                Sum(Case(When(direction=StockLedger.Direction.IN, then='quantity_gram'), default=ZERO_DEC,
                         output_field=DEC_FIELD)), Decimal('0'), output_field=DEC_FIELD),
            all_out_gram=Coalesce(
                Sum(Case(When(direction=StockLedger.Direction.OUT, then='quantity_gram'), default=ZERO_DEC,
                         output_field=DEC_FIELD)), Decimal('0'), output_field=DEC_FIELD),
            all_in_pcs=Coalesce(
                Sum(Case(When(direction=StockLedger.Direction.IN, then='quantity_pieces'), default=ZERO_INT,
                         output_field=INT_FIELD)), Value(0), output_field=INT_FIELD),
            all_out_pcs=Coalesce(
                Sum(Case(When(direction=StockLedger.Direction.OUT, then='quantity_pieces'), default=ZERO_INT,
                         output_field=INT_FIELD)), Value(0), output_field=INT_FIELD),
        )
        for row in all_time_raw:
            all_time_map[row['product_id']] = {'in_gram': D(row['all_in_gram']), 'out_gram': D(row['all_out_gram']),
                                               'in_pcs': D(row['all_in_pcs']), 'out_pcs': D(row['all_out_pcs'])}

    snap_map = {}
    if product_ids:
        snap_rows = StockSnapshot.objects.filter(store=store, product_id__in=product_ids).values('product_id').annotate(
            pcs=Sum('stock_pieces'), wt=Sum('stock_gram'))
        snap_map = {r['product_id']: {'pieces': D(r['pcs']), 'weight': D(r['wt'])} for r in snap_rows}

    by_label = defaultdict(
        lambda: {'kind': None, 'unit': None, 'decimals': 0, 'in_total': D('0'), 'out_total': D('0'), 'opening': D('0'),
                 'expected': D('0'), 'actual': D('0')})
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
        lbl = info['label']
        blk = by_label[lbl]
        blk['kind'] = info['kind']
        blk['decimals'] = max(blk['decimals'], info['decimals'])
        blk['in_total'] += info['in_total']
        blk['out_total'] += info['out_total']
        blk['opening'] += opening
        blk['expected'] += ledger_balance
        blk['actual'] += actual

    items = []
    for label in sorted(by_label.keys(), key=lambda k: label_order.get(k, 10 ** 9)):
        blk = by_label[label]
        decs = blk['decimals']
        items.append({
            'kind': blk['kind'], 'stok_kodu': label, 'ilk_stok': fmt_tr(blk['opening'], decs),
            'giren': fmt_tr(blk['in_total'], decs), 'cikan': fmt_tr(blk['out_total'], decs),
            'olmali': fmt_tr(blk['expected'], decs), 'stok_durum': fmt_tr(blk['actual'], decs),
            'is_integrity_ok': ((blk['actual'] - blk['expected']) == D('0')),
        })

    GROUP_ORDER = ['ALTIN_HAS', 'ZIYNET', 'BILEZIK', 'HURDA', 'DOVIZ', 'BARKODLU', 'DIGER']
    GROUP_TITLES = {'ALTIN_HAS': 'ALTIN (HAS)', 'ZIYNET': 'ZİYNET', 'BILEZIK': 'BİLEZİK', 'HURDA': 'HURDA',
                    'DOVIZ': 'DÖVİZ', 'BARKODLU': 'BARKODLU ÜRÜN', 'DIGER': 'DİĞER'}
    grouped_rows = []
    for k in GROUP_ORDER:
        group_items = [it for it in items if it['kind'] == k]
        if group_items:
            grouped_rows.append({'is_group': True, 'group': GROUP_TITLES.get(k, k)})
            grouped_rows.extend([{**it, 'is_group': False} for it in group_items])

    n = timezone.now()
    now_loc = timezone.localtime(n) if timezone.is_aware(n) else n
    return {
        'company_name': getattr(store, 'name', 'Kuyum Plus'),
        'company_subtitle': getattr(store, 'address', '') or '',
        'report_title': 'Güncel Rapor – Stok Durumu',
        'report_no': f"STK-{now_loc.strftime('%Y%m%d%H%M')}",
        'period_text': f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
        'generated_date': now_loc.date().strftime('%d/%m/%Y'),
        'generated_time': now_loc.strftime('%H:%M'),
        'print_datetime': now_loc.strftime('%d/%m/%Y %H:%M'),
        'authorized_person': (f"{user.first_name} {user.last_name}".strip() or user.username),
        'period_days': period_days,
        'store_id': str(getattr(store, 'store_id', '') or getattr(store, 'id', '-')),
        'rows': grouped_rows,
        'is_preview': False,
        'sum_total_sales_eur': fmt_tr(total_sales_eur),
        'sum_total_purchases_eur': fmt_tr(total_purchases_eur),
        'sum_net_total_tl': fmt_tr(net_total_tl),
        'is_net_negative': net_total_tl < 0,
    }


@shared_task(bind=True)
def generate_report_task(self, report_type, user_id, start_date_str, end_date_str, period='daily',
                         profit_type='ziynet', customer_id=''):
    task_id = self.request.id
    try:
        rec = GeneratedReports.objects.get(task_id=task_id)
    except GeneratedReports.DoesNotExist:
        rec = GeneratedReports.objects.create(task_id=task_id, report_type=report_type, status='PENDING')

    try:
        user = Users.objects.get(pk=user_id)
        store = user.store
        if not store:
            raise ValueError("Kullanıcının mağazası yok")

        today_date = date.today()
        start_date, end_date, period_days = _parse_date_params(start_date_str, end_date_str, period, today_date)
        if start_date is None:
            raise ValueError("Geçersiz tarih parametreleri")

        if report_type == 'process':
            context = _build_process_report_context(store, user, start_date, end_date, period_days)
            template_name = "management/dashboard/process_report.html"
        elif report_type == 'profit':
            context = _build_profit_report_context(store, user, start_date, end_date, period_days, profit_type)
            template_name = context.pop("template_name")
        elif report_type == 'customer':
            context = _build_customer_report_context(store, user, start_date, end_date, period_days)
            template_name = "management/dashboard/customer_report.html"
        elif report_type == 'customer_detail':
            if not customer_id:
                raise ValueError("Müşteri ID eksik")
            from apps.customers.models import Customers
            customer = Customers.objects.get(pk=customer_id, store=store, is_deleted=False)
            context = _build_customer_detail_report_context(
                store, user, customer, start_date, end_date, period_days
            )
            template_name = "management/dashboard/customer_detail_report.html"
        elif report_type == 'currency':
            context = _build_currency_report_context(store, user, start_date, end_date, period_days)
            template_name = "management/dashboard/currency_report.html"
        elif report_type == 'stock':
            context = _build_current_stock_report_context(store, user, start_date, end_date, period_days)
            template_name = "management/dashboard/current_stock_report.html"
        else:
            rec.status = 'FAILED'
            rec.error_message = f"Desteklenmeyen rapor tipi: {report_type}"
            rec.save()
            return {"status": "FAILED", "error": rec.error_message}

        date_str = timezone.now().strftime("%d-%m-%Y_%H%M")

        if report_type == 'profit':
            clean_name = f"{profit_type.capitalize()}_Kar_Raporu_{date_str}.pdf"
        elif report_type == 'process':
            clean_name = f"Islem_Raporu_{date_str}.pdf"
        elif report_type == 'customer':
            clean_name = f"Musteri_Cari_Raporu_{date_str}.pdf"
        elif report_type == 'currency':
            clean_name = f"Doviz_Islem_Raporu_{date_str}.pdf"
        elif report_type == 'stock':
            clean_name = f"Guncel_Stok_Raporu_{date_str}.pdf"
        elif report_type == 'customer_detail':
            clean_name = f"Musteri_Detay_Raporu_{date_str}.pdf"
        else:
            clean_name = f"KuyumPlus_Rapor_{date_str}.pdf"

        pdf_buffer = _render_pdf(template_name, context)
        rec.file.save(clean_name, ContentFile(pdf_buffer.read()), save=False)
        rec.status = 'SUCCESS'
        rec.save()
        return {"status": "SUCCESS", "task_id": task_id}
    except Exception as e:
        rec.status = 'FAILED'
        rec.error_message = str(e)
        rec.save()
        return {"status": "FAILED", "error": str(e)}


# ============================================================================
# FAZ R-3: GÜNLÜK ROLLUP HESAPLAMA TASK'LARI
# ============================================================================

@shared_task(
    name='dashboard.compute_daily_rollups',
    ignore_result=True,
    soft_time_limit=300,
    time_limit=600,
)
def compute_daily_rollups():
    """
    Tüm mağazalar için DÜN'ün DailyStoreReport + DailyEmployeeReport'unu hesaplar.
    Her gece 02:05'te çalıştırılması önerilir.
    Son 3 günü de yeniden hesaplar (geç kapanan işlemler için).
    """
    import logging
    from datetime import date, timedelta
    from apps.dashboard.services import compute_reports_for_all_stores

    logger = logging.getLogger('dashboard.reports')
    today = date.today()

    total = 0
    for days_ago in range(3, -1, -1):  # 3 gün önce → bugün
        target = today - timedelta(days=days_ago)
        count = compute_reports_for_all_stores(target)
        total += count
        logger.info(f"Rollup hesaplandı: date={target}, stores={count}")

    return f"{total} mağaza-gün rollup'ı hesaplandı."


@shared_task(
    name='dashboard.compute_today_rollup',
    ignore_result=True,
    soft_time_limit=120,
    time_limit=180,
)
def compute_today_rollup():
    """
    Sadece BUGÜN'ün rollup'ını hesaplar.
    15 dakikada bir çalıştırılması önerilir (delta güncelleme).
    Redis cache'ini de temizler.
    """
    import logging
    from datetime import date
    from django.core.cache import cache
    from apps.stores.models import Stores
    from apps.dashboard.services import (
        compute_daily_store_report,
        compute_daily_employee_reports,
    )

    logger = logging.getLogger('dashboard.reports')
    today = date.today()
    updated = 0

    for store in Stores.objects.filter(is_active=True):
        try:
            compute_daily_store_report(store, today)
            compute_daily_employee_reports(store, today)
            # Redis cache'i temizle (get_dashboard_summary yeniden hesaplayacak)
            cache.delete(f"dashboard_kpi:{store.id}:{today.isoformat()}")
            updated += 1
        except Exception as e:
            logger.error(f"Bugün rollup hatası: store={store}, err={e}")

    return f"{updated} mağaza bugünkü rollup güncellendi."
