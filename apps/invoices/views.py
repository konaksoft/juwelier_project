from __future__ import annotations

import json
import logging
import os
import traceback
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.db import transaction
from django.db.models import Q, Sum, Count, Case, When, IntegerField
from django.db.models.functions import TruncDate, Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.urls import reverse

from xhtml2pdf import pisa

# --- Modeller ---
from apps.invoices.models import Invoice, InvoiceItem, InvoiceSyncLog, InvoiceSequence
from apps.process.models import Process
from apps.products.models import Products
from apps.customers.models import Customers
from apps.suppliers.models import Suppliers
from apps.stores.models import Stores
from apps.roles.decorators import role_required

log = logging.getLogger(__name__)


def _user_store(request) -> Stores:
    st = getattr(request.user, "store", None)
    if not st:
        raise ValueError("Kullanıcıya bağlı mağaza bulunamadı.")
    return st


def _safe_decimal(v, default="0"):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def q2(x: Decimal) -> Decimal:
    return (x or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def q3(x: Decimal) -> Decimal:
    return (x or Decimal('0')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


@login_required(login_url='login')
@role_required('INVOICES_INVOICES_INDEX')
def invoices_index(request):
    return render(request, 'management/invoices/index.html', {'title': 'Fatura Yönetimi'})


@login_required(login_url='login')
@role_required('INVOICES_DASHBOARD_INDEX')
def dashboard_index(request):
    return render(request, 'management/invoices/dashboard.html', {'title': 'Fatura Paneli'})


@login_required(login_url='login')
@role_required('INVOICES_DASHBOARD_METRICS')
def dashboard_metrics(request):
    store = _user_store(request)
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    status = (request.GET.get('status') or '').upper()

    qs = Invoice.objects.filter(is_deleted=False, store=store)

    if date_from:
        qs = qs.filter(issue_date__date__gte=parse_date(date_from))
    if date_to:
        qs = qs.filter(issue_date__date__lte=parse_date(date_to))
    if status:
        qs = qs.filter(status=status)

    totals = qs.aggregate(
        total=Coalesce(Sum('grand_total'), Decimal('0')),
        count=Coalesce(Count('id'), 0),
        issued=Coalesce(Sum(Case(When(status=Invoice.Status.ISSUED, then=1), default=0, output_field=IntegerField())),
                        0),
        draft=Coalesce(Sum(Case(When(status=Invoice.Status.DRAFT, then=1), default=0, output_field=IntegerField())), 0),
        canceled=Coalesce(
            Sum(Case(When(status=Invoice.Status.CANCELED, then=1), default=0, output_field=IntegerField())), 0),
        sent=Coalesce(Sum(Case(When(status=Invoice.Status.SENT, then=1), default=0, output_field=IntegerField())), 0),
        efatura=Coalesce(
            Sum(Case(When(doc_class=Invoice.DocumentClass.E_INVOICE, then=1), default=0, output_field=IntegerField())),
            0),
        earsiv=Coalesce(
            Sum(Case(When(doc_class=Invoice.DocumentClass.E_ARCHIVE, then=1), default=0, output_field=IntegerField())),
            0),
    )

    trend_qs = qs.annotate(d=TruncDate('issue_date')).values('d').annotate(
        total=Coalesce(Sum('grand_total'), Decimal('0'))).order_by('d')
    trend = [{'date': x['d'].isoformat() if x['d'] else None, 'total': str(x['total'])} for x in trend_qs]

    latest_qs = qs.select_related('customer', 'supplier').order_by('-issue_date')[:10]
    latest = []
    for inv in latest_qs:
        pos = 'PAID' if inv.is_paid else ('PENDING' if inv.paid_total > 0 else '')

        edoc = 'BELGE'
        if inv.doc_class == Invoice.DocumentClass.E_INVOICE:
            edoc = 'EFATURA'
        elif inv.doc_class == Invoice.DocumentClass.E_ARCHIVE:
            edoc = 'EARSIV'
        elif inv.doc_class == Invoice.DocumentClass.EXPENSE_VOUCHER:
            edoc = 'GIDER'
        elif inv.doc_class == Invoice.DocumentClass.PROFORMA:
            edoc = 'PROFORMA'

        cust = "-"
        if inv.customer:
            cust = f"{inv.customer.first_name} {inv.customer.last_name}"
        elif inv.supplier:
            cust = f"{inv.supplier.company_name}"

        latest.append({
            'id': str(inv.id),
            'invoice_no': inv.invoice_no,
            'customer': cust,
            'issue_date': inv.issue_date.strftime('%d/%m/%Y %H:%M') if inv.issue_date else '',
            'grand_total': str(inv.grand_total or 0),
            'status': inv.status,
            'pos_status': pos,
            'edoc_status': inv.gib_status_desc or '',
            'edoc_type': edoc,
        })

    return JsonResponse({
        'kpis': {
            'total': str(totals['total']),
            'count': int(totals['count']),
            'issued': int(totals['issued']),
            'draft': int(totals['draft']),
            'canceled': int(totals['canceled']),
            'sent': int(totals['sent']),
            'efatura': int(totals['efatura']),
            'earsiv': int(totals['earsiv']),
        },
        'trend': trend,
        'latest': latest
    })


@login_required(login_url='login')
@role_required('INVOICES_INVOICE_DETAIL_PAGE')
def invoice_detail_page(request, record_id: uuid.UUID):
    inv = get_object_or_404(Invoice, id=record_id, is_deleted=False)
    if inv.invoice_type == Invoice.Type.PURCHASE:
        return expense_note_detail_page(request, record_id)

    return render(request, 'management/invoices/detail.html', {
        'title': f'Fatura · {inv.invoice_no}',
        'record': inv,
    })


@login_required(login_url='login')
@role_required('INVOICES_INVOICE_DETAIL_PAGE')
def expense_note_detail_page(request, record_id: uuid.UUID):
    inv = get_object_or_404(Invoice, id=record_id, is_deleted=False)
    return render(request, 'management/invoices/expense_note_detail.html', {
        'title': f'Gider Pusulası · {inv.invoice_no}',
        'record': inv,
    })


@login_required(login_url='login')
@role_required('INVOICES_CREATE_FROM_PROCESS_GROUP_VIEW')
def create_from_process_group_view(request):
    """Fatura Oluşturma Ekranı (Frontend Data Hazırlığı)"""
    if request.method != 'GET':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'}, status=400)

    process_no = (request.GET.get('process_no') or '').strip()
    store = _user_store(request)

    limits = {
        'cash_limit': 30000,
        'invoice_limit': 36000,
        'masak_limit': 185000
    }

    procs = Process.objects.select_related('product', 'customer', 'supplier').filter(
        is_deleted=False, store=store, process_no=process_no
    ).order_by('date', 'id')

    if not procs.exists():
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Kayıt bulunamadı.'}, status=404)

    existing_invoice = Invoice.objects.filter(store=store, is_deleted=False, process__process_no=process_no).first()

    target_customer = None
    if existing_invoice:
        target_customer = existing_invoice.customer
    elif procs[0].customer:
        target_customer = procs[0].customer

    seed = {
        'invoice_id': str(existing_invoice.id) if existing_invoice else "",
        'process_no': process_no,
        'issue_dt': timezone.now().strftime('%Y-%m-%dT%H:%M'),
        'customer_id': str(target_customer.id) if target_customer else '',
        'items': []
    }

    for p in procs:
        prod = p.product

        is_gram = bool(Decimal(str(p.gram or 0)) > 0)
        qty = q3(Decimal(str(p.gram)) if is_gram else Decimal(str(p.piece or 0)))
        if qty <= 0: qty = Decimal('1.000')
        unit = 'GR' if is_gram else 'AD'

        labor_val = q2(_safe_decimal(p.labor_amount))
        metal_unit_price = q2(_safe_decimal(p.unit_price))
        buy_price = Decimal(str(getattr(prod, 'buy_price_tl', 0) or 0))

        seed['items'].append({
            'process_id': str(p.id),
            'product_id': str(prod.id) if prod else '',
            'product_name': getattr(prod, 'name', '') or 'Ürün',
            'unit': unit,
            'qty': f'{qty:.3f}',
            'process_unit_price': f'{metal_unit_price:.2f}',
            'labor_amount': f'{labor_val:.2f}',
            'buy_price': f'{buy_price:.2f}'
        })

    customers_data = list(Customers.objects.filter(store=store, is_deleted=False, is_active=True)
                          .values('id', 'first_name', 'last_name', 'identification_number',
                                  'identification_front_image', 'identification_back_image')
                          .order_by('first_name'))

    final_cust_data = []
    for c in customers_data:
        has_img = bool(c['identification_front_image'] or c['identification_back_image'])
        final_cust_data.append({
            'id': str(c['id']),
            'name': f"{c['first_name']} {c['last_name']}",
            'tckn': c['identification_number'] or '',
            'has_image': has_img
        })

    return render(request, 'management/invoices/create_from_process_group.html', {
        'seed': seed,
        'customers': final_cust_data,
        'limits': limits
    })


@login_required(login_url='login')
@role_required('INVOICES_ADD_INVOICE')
def add_invoice(request):
    """Fatura/Proforma Kaydetme"""
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Method Not Allowed'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz JSON verisi'}, status=400)

    store = _user_store(request)
    invoice_id = payload.get('invoice_id')
    process_no_header = payload.get('process_no')

    LIMIT_INVOICE_REQUIRED = Decimal('36000')
    LIMIT_MASAK = Decimal('185000')

    try:
        with transaction.atomic():
            invoice_type = Invoice.Type.SALE
            doc_class = Invoice.DocumentClass.PROFORMA

            is_purchase = False
            if process_no_header:
                is_purchase = Process.objects.filter(
                    store=store, process_no=process_no_header,
                    transaction_type__in=['PURCHASE', 'RETURN']
                ).exists()

            if is_purchase:
                invoice_type = Invoice.Type.PURCHASE
                doc_class = Invoice.DocumentClass.EXPENSE_VOUCHER

            if invoice_id:
                inv = Invoice.objects.filter(id=invoice_id, store=store).first()
                if not inv:
                    return JsonResponse({'error': True, 'error_msg': 'Fatura bulunamadı.'}, status=404)
            else:
                invoice_no, seq = Invoice.next_number_for(store, doc_class=doc_class)
                inv = Invoice(
                    store=store,
                    invoice_no=invoice_no,
                    sequence_no=seq,
                    status=Invoice.Status.DRAFT,
                    invoice_type=invoice_type,
                    doc_class=doc_class
                )

            if payload.get('customer_id'):
                cust = Customers.objects.filter(id=payload.get('customer_id')).first()
                inv.customer = cust

            issue_date_str = payload.get('issue_date')
            if issue_date_str:
                try:
                    dt = datetime.fromisoformat(issue_date_str.replace('Z', '+00:00'))
                    inv.issue_date = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
                except (ValueError, TypeError):
                    inv.issue_date = timezone.now()

            due_date_str = payload.get('due_date')
            if due_date_str:
                inv.due_date = parse_date(due_date_str)

            inv.notes = payload.get('notes', '')

            if process_no_header and not inv.process:
                main_process = Process.objects.filter(store=store, process_no=process_no_header).first()
                if main_process: inv.process = main_process

            inv.save()

            inv.items.all().delete()
            items_data = payload.get('items') or []

            for row in items_data:
                raw_pid = row.get('product_id')
                proc_id = row.get('process_id')

                qty = q3(_safe_decimal(row.get('quantity')))
                if qty <= 0: qty = Decimal('1.000')

                metal_unit_price = q2(_safe_decimal(row.get('unit_price')))
                labor_total_val = q2(_safe_decimal(row.get('process_labor_val')))

                prod = Products.objects.filter(id=raw_pid).first() if raw_pid else None
                p_name = row.get('product_name') or (prod.name if prod else 'Ürün')

                if proc_id:
                    target_process = Process.objects.filter(id=proc_id, store=store).first()
                    if target_process:
                        target_process.labor_amount = labor_total_val
                        metal_total = q2(metal_unit_price * qty)
                        labor_vat_amount = q2(labor_total_val * Decimal('0.20'))
                        target_process.amount = q2(metal_total + labor_total_val + labor_vat_amount)

                        if labor_total_val > 0:
                            target_process.gross_profit = labor_total_val
                            target_process.net_profit = q2(labor_total_val / Decimal('1.20'))

                        target_process.save(update_fields=['labor_amount', 'amount', 'gross_profit', 'net_profit'])

                InvoiceItem.objects.create(
                    invoice=inv, product=prod, product_name=p_name,
                    quantity=qty, unit=row.get('unit', 'AD'),
                    unit_price=metal_unit_price, vat_rate=Decimal('0.00'),
                    discount_rate=Decimal('0.00'), notes="Has Metal Bedeli (KDV 0)"
                ).recompute(save=True)

                if labor_total_val > 0 and inv.invoice_type == Invoice.Type.SALE:
                    InvoiceItem.objects.create(
                        invoice=inv, product=None,
                        product_name=f"{p_name} - İşçilik Hizmeti",
                        quantity=Decimal('1.000'), unit=InvoiceItem.Unit.PIECE,
                        unit_price=labor_total_val, vat_rate=Decimal('20.00'),
                        discount_rate=Decimal('0.00'), notes="İşçilik Bedeli (KDV 20)"
                    ).recompute(save=True)

            inv.recompute_totals(save=True)

            if inv.invoice_type == Invoice.Type.SALE:
                if inv.grand_total >= LIMIT_INVOICE_REQUIRED:
                    if not inv.customer or not inv.customer.identification_number:
                        raise ValueError(
                            f"Fatura tutarı {LIMIT_INVOICE_REQUIRED:,.0f} TL üzerindedir. Müşteri TCKN zorunludur!")

                if inv.grand_total >= LIMIT_MASAK:
                    has_img = inv.customer and (
                            inv.customer.identification_front_image or inv.customer.identification_back_image)
                    if not has_img:
                        raise ValueError(
                            f"MASAK Sınırı ({LIMIT_MASAK:,.0f} TL) aşıldı! Müşteri kimlik görseli zorunludur.")

        return JsonResponse({
            'result': True,
            'invoice_id': str(inv.id),
            'invoice_no': inv.invoice_no,
            'redirect_url': reverse('invoices:index')
        })

    except ValueError as ve:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(ve)}, status=200)
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'result': False, 'error': True, 'error_msg': f'Kayıt hatası: {str(e)}'}, status=500)


@login_required(login_url='login')
@role_required('INVOICES_GET_ALL')
def get_all(request):
    """Datatables için Veri Kaynağı"""
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 25))
    search_value = (request.GET.get('search[value]') or '').strip()

    # PERF-01: Sunucu taraflı üst limit — frontend length=-1 veya çok yüksek değer gönderse bile
    # tek istekte 500'den fazla fatura yüklenmez. Büyük listeler için asenkron rapor/export akışı kullanılmalı.
    MAX_PAGE_SIZE = 500
    if length == -1 or length > MAX_PAGE_SIZE:
        length = MAX_PAGE_SIZE
    if length <= 0:
        length = 25
    if start < 0:
        start = 0

    invoice_type_filter = request.GET.get('invoice_type')
    status_filter = request.GET.get('status')

    quick_date = request.GET.get('quick_date')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    # SY-02: PavoPOS fatura izolasyonu
    source_filter = request.GET.get('source', 'all')

    store = _user_store(request)
    qs = Invoice.objects.filter(is_deleted=False, store=store).select_related('customer', 'supplier', 'process')

    # ── tab_type: Kesin Sekme İzolasyonu ──
    # Frontend'den tab_type='draft' veya 'issued' geldiğinde
    # gib_status_code'a göre filtrelenir. Bu sayede aynı fatura
    # iki sekmede asla görünmez.
    tab_type = request.GET.get('tab_type')

    if invoice_type_filter == 'PURCHASE':
        qs = qs.filter(invoice_type=Invoice.Type.PURCHASE)
    else:
        qs = qs.filter(invoice_type=Invoice.Type.SALE)

        if tab_type == 'draft':
            # ── PROFORMA/TASLAK sekmesi ──
            # Sadece GİB'e gönderilMEMİŞ faturalar:
            #   gib_status_code = NULL, '', '0', '10'
            qs = qs.filter(
                Q(gib_status_code__isnull=True) |
                Q(gib_status_code='') |
                Q(gib_status_code='0') |
                Q(gib_status_code='10')
            )
        elif tab_type == 'issued':
            # ── KESİLEN FATURALAR sekmesi ──
            # Sadece GİB'e GÖNDERİLMİŞ faturalar:
            #   gib_status_code '0', '10', '' veya NULL olmayan her şey
            #   (100, 1000, 1100, 1200, 1300, 1400, 1500, 1230 vb.)
            qs = qs.exclude(
                Q(gib_status_code__isnull=True) |
                Q(gib_status_code='') |
                Q(gib_status_code='0') |
                Q(gib_status_code='10')
            )
        elif status_filter == 'DRAFT':
            # Eski mantık (tab_type yoksa fallback)
            qs = qs.filter(Q(status=Invoice.Status.DRAFT) | Q(status=Invoice.Status.QUEUED) | Q(doc_class=Invoice.DocumentClass.PROFORMA))
        elif status_filter:
            allowed_statuses = [s.strip() for s in status_filter.split(',') if s.strip()]
            qs = qs.filter(status__in=allowed_statuses)

    # SY-02: PavoPOS kaynak filtresi
    if source_filter == 'pavo':
        qs = qs.exclude(pavo_sale_number='')
    elif source_filter == 'manual':
        qs = qs.filter(pavo_sale_number='')

    if search_value:
        qs = qs.filter(
            Q(invoice_no__icontains=search_value) |
            Q(customer__first_name__icontains=search_value) |
            Q(customer__last_name__icontains=search_value) |
            Q(supplier__company_name__icontains=search_value)
        )

    today = timezone.now().date()
    if quick_date:
        if quick_date == 'today':
            qs = qs.filter(issue_date__date=today)
        elif quick_date == 'yesterday':
            qs = qs.filter(issue_date__date=today - timezone.timedelta(days=1))
        elif quick_date == 'this_week':
            start_week = today - timezone.timedelta(days=today.weekday())
            qs = qs.filter(issue_date__date__gte=start_week)
        elif quick_date == 'this_month':
            qs = qs.filter(issue_date__month=today.month, issue_date__year=today.year)
    elif date_from or date_to:
        if date_from: qs = qs.filter(issue_date__date__gte=parse_date(date_from))
        if date_to: qs = qs.filter(issue_date__date__lte=parse_date(date_to))

    total = qs.count()
    qs = qs.order_by('-issue_date')

    # PERF-01: length zaten üstte [1, MAX_PAGE_SIZE] aralığına sıkıştırıldı.
    page_qs = qs[start:start + length]

    rows = []
    for inv in page_qs:
        pos_status = 'PAID' if inv.is_paid else ('PENDING' if inv.paid_total > 0 else '')

        edoc_type = 'BELGE'
        if inv.doc_class == Invoice.DocumentClass.E_INVOICE:
            edoc_type = 'EFATURA'
        elif inv.doc_class == Invoice.DocumentClass.E_ARCHIVE:
            edoc_type = 'EARSIV'
        elif inv.doc_class == Invoice.DocumentClass.PROFORMA:
            edoc_type = 'PROFORMA'

        cust_name = "-"
        if inv.customer:
            cust_name = f"{inv.customer.first_name} {inv.customer.last_name}"
        elif inv.supplier:
            cust_name = f"{inv.supplier.company_name}"

        process_no = inv.process.process_no if inv.process else ''

        rows.append({
            'id': str(inv.id),
            'invoice_no': inv.invoice_no,
            'invoice_type': inv.invoice_type,
            'process_no': process_no,
            'customer': cust_name,
            'issue_date': inv.issue_date.strftime('%d/%m/%Y %H:%M') if inv.issue_date else '',
            'grand_total': f'{q2(inv.grand_total):.2f}',
            'status': inv.status,
            'pos_status': pos_status,
            'edoc_status': inv.gib_status_desc or '',
            'edoc_type': edoc_type,
            'xml_url': request.build_absolute_uri(inv.xml_file.url) if inv.xml_file else '',
            'gib_status_code': inv.gib_status_code or '',
            'gib_status_desc': inv.gib_status_desc or '',
            'gib_error': inv.gib_error or '',
            # SY-02: PavoPOS izolasyon verileri
            'is_pavo': bool(inv.pavo_sale_number),
            'pavo_sale_no': inv.pavo_sale_number or '',
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total,
        'recordsFiltered': total,
        'data': rows,
    })


@login_required(login_url='login')
@role_required('INVOICES_CHANGE_STATUS')
def change_status(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})

    ids = request.POST.getlist('ids[]')
    action = (request.POST.get('action') or '').lower()

    valid_actions = {
        'issue': Invoice.Status.ISSUED,
        'cancel': Invoice.Status.CANCELED,
        'draft': Invoice.Status.DRAFT,
        'sent': Invoice.Status.SENT
    }

    if action not in valid_actions:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz işlem.'})

    try:
        qs = Invoice.objects.filter(id__in=ids, is_deleted=False, store=_user_store(request))

        # GİB kuyruğundaki faturalara manuel ISSUED atamasını engelle
        if action == 'issue':
            blocked = qs.filter(status__in=[Invoice.Status.QUEUED, Invoice.Status.SENT]).count()
            if blocked > 0:
                return JsonResponse({
                    'result': False,
                    'error': True,
                    'error_msg': f'{blocked} fatura GİB sürecinde olduğu için manuel olarak kesinleştirilemez. '
                                 'GİB onayını bekleyin veya "Durum Sorgula" butonunu kullanın.'
                })

        update_fields = {'status': valid_actions[action], 'updated_at': timezone.now()}

        if action == 'sent':
            edoc_type = (request.POST.get('edoc_type') or '').upper()
            if edoc_type == 'EFATURA':
                update_fields['doc_class'] = Invoice.DocumentClass.E_INVOICE
                update_fields['is_einvoice'] = True
            elif edoc_type == 'EARSIV':
                update_fields['doc_class'] = Invoice.DocumentClass.E_ARCHIVE
                update_fields['is_einvoice'] = False

            update_fields['gib_status_desc'] = 'Gönderildi'

        qs.update(**update_fields)
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('INVOICES_DELETE')
def delete(request):
    if request.method != 'POST': return JsonResponse({'error': True}, status=405)
    ids = request.POST.getlist('ids[]')
    Invoice.objects.filter(id__in=ids, store=_user_store(request)).update(is_deleted=True)
    return JsonResponse({'result': True})


@login_required(login_url='login')
@role_required('INVOICES_PRODUCT_BRIEF')
def product_brief(request):
    pid = request.GET.get('id')
    bc = request.GET.get('barcode')
    qs = Products.objects.all()

    if pid:
        p = qs.filter(id=pid).first()
    elif bc:
        p = qs.filter(barcode=bc).first()
    else:
        return JsonResponse({'error': True}, status=400)

    if not p: return JsonResponse({'error': True}, status=404)

    return JsonResponse({
        'result': True,
        'product': {
            'id': str(p.id),
            'name': p.name,
            'barcode': p.barcode,
            'is_gram_bullion': bool(p.is_gram_bullion),
            'sale_price_tl': str(p.sale_price_tl or 0),
            'buy_price_tl': str(p.buy_price_tl or 0),
        }
    })


# --- PDF GENERATION ---
def link_callback(uri, rel):
    if settings.MEDIA_URL and uri.startswith(settings.MEDIA_URL):
        path = os.path.join(settings.MEDIA_ROOT, uri.replace(settings.MEDIA_URL, ""))
    elif settings.STATIC_URL and uri.startswith(settings.STATIC_URL):
        static_path = uri.replace(settings.STATIC_URL, "")
        path = finders.find(static_path)
        if not path:
            path = os.path.join(settings.STATIC_ROOT, static_path)
    else:
        path = uri
    if not path or not os.path.isfile(path): return None
    return path


@xframe_options_sameorigin
@login_required(login_url='login')
@role_required('INVOICES_INVOICE_PDF_DOWNLOAD')
def invoice_pdf_download(request, record_id: uuid.UUID):
    try:
        inv = get_object_or_404(
            Invoice.objects.select_related('customer', 'supplier', 'store', 'store__company').prefetch_related('items'),
            id=record_id, is_deleted=False
        )

        if inv.invoice_type == Invoice.Type.PURCHASE:
            template_name = "management/invoices/expense_note_detail.html"
        else:
            template_name = "management/invoices/detail.html"

        context = {"record": inv, "request": request}
        html = render_to_string(template_name, context)

        pdf_io = BytesIO()
        pisa_status = pisa.CreatePDF(src=html, dest=pdf_io, link_callback=link_callback)

        if pisa_status.err:
            return HttpResponse(f"PDF Hatası: {pisa_status.err}", status=500)

        filename = f"{inv.invoice_no}.pdf"
        response = HttpResponse(pdf_io.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response

    except Exception as e:
        return HttpResponse(f"<h1>Hata</h1><pre>{str(e)}</pre>", status=500)


@login_required(login_url='login')
@role_required('INVOICES_INVOICE_DETAIL_JSON')
def invoice_detail_json(request, record_id: uuid.UUID):
    inv = get_object_or_404(Invoice, id=record_id, is_deleted=False)
    items = [{
        'id': it.id,
        'product_id': str(it.product_id) if it.product_id else None,
        'product_name': it.product_name,
        'quantity': f'{q3(it.quantity):.3f}',
        'unit': it.unit,
        'unit_price': f'{q3(it.unit_price):.3f}',
        'total_incl_vat': f'{q2(it.total_incl_vat):.2f}',
    } for it in inv.items.all()]

    return JsonResponse({
        'id': str(inv.id),
        'invoice_no': inv.invoice_no,
        'status': inv.status,
        'grand_total': f'{q2(inv.grand_total):.2f}',
        'items': items,
    })


@login_required(login_url='login')
@role_required('INVOICES_ADD_OR_UPDATE_ITEM')
def add_or_update_item(request, invoice_id: uuid.UUID):
    if request.method != 'POST': return JsonResponse({'error': True}, status=405)
    inv = get_object_or_404(Invoice, id=invoice_id, is_deleted=False)
    payload = json.loads(request.body.decode('utf-8'))

    with transaction.atomic():
        item_id = payload.get('id')
        if item_id:
            it = get_object_or_404(InvoiceItem, id=item_id, invoice=inv)
        else:
            it = InvoiceItem(invoice=inv)

        it.product_name = payload.get('product_name', it.product_name)
        it.quantity = q3(_safe_decimal(payload.get('quantity'), it.quantity))
        it.unit_price = q3(_safe_decimal(payload.get('unit_price'), it.unit_price))
        it.save()
        it.recompute(save=True)
        inv.recompute_totals(save=True)

    return JsonResponse({'result': True})


@login_required(login_url='login')
@role_required('INVOICES_DELETE_ITEM')
def delete_item(request, invoice_id: uuid.UUID, item_id: int):
    if request.method != 'POST': return JsonResponse({'error': True}, status=405)
    inv = get_object_or_404(Invoice, id=invoice_id)
    InvoiceItem.objects.filter(id=item_id, invoice=inv).delete()
    inv.recompute_totals(save=True)
    return JsonResponse({'result': True})


@login_required(login_url='login')
@role_required('INVOICES_ALLOCATE_PAYMENT')
def allocate_payment(request):
    return JsonResponse({'result': True})


@login_required(login_url='login')
@role_required('INVOICES_GET_INVOICE_ALLOCATIONS')
def get_invoice_allocations(request, record_id: uuid.UUID):
    return JsonResponse({'items': [], 'paid_total': '0.00', 'balance': '0.00'})


# ======================================================================
# SERBEST (BAĞIMSIZ) FATURA OLUŞTURMA
# ======================================================================

# Serbest fatura için desteklenen ürün tipleri
FREE_INVOICE_PRODUCT_TYPES = [
    {'value': 'Ziynet Altın', 'label': 'Ziynet Altın'},
    {'value': '14 Ayar Altın', 'label': '14 Ayar Altın'},
    {'value': '22 Ayar Altın', 'label': '22 Ayar Altın'},
    {'value': 'Has Altın (24 Ayar)', 'label': 'Has Altın (24 Ayar)'},
    {'value': 'Gümüş', 'label': 'Gümüş'},
    {'value': 'Hurda Altın', 'label': 'Hurda Altın'},
    {'value': 'Elmas / Pırlanta', 'label': 'Elmas / Pırlanta'},
    {'value': 'Diğer', 'label': 'Diğer'},
]


@login_required(login_url='login')
@role_required('INVOICES_ADD_INVOICE')
def create_free_invoice_view(request):
    """
    Bağımsız / Serbest Fatura Oluşturma Ekranı.
    Herhangi bir Process kaydına bağlı kalmadan, doğrudan fatura kesmek için.
    """
    store = _user_store(request)

    customers_data = list(
        Customers.objects.filter(store=store, is_deleted=False, is_active=True)
        .values('id', 'first_name', 'last_name', 'identification_number',
                'identification_front_image', 'identification_back_image')
        .order_by('first_name')
    )

    customers = []
    for c in customers_data:
        has_img = bool(c['identification_front_image'] or c['identification_back_image'])
        customers.append({
            'id': str(c['id']),
            'name': f"{c['first_name']} {c['last_name']}",
            'tckn': c['identification_number'] or '',
            'has_image': has_img,
        })

    return render(request, 'management/invoices/create_free.html', {
        'title': 'Serbest Fatura Oluştur',
        'customers': customers,
        'product_types': FREE_INVOICE_PRODUCT_TYPES,
        'issue_dt': timezone.now().strftime('%Y-%m-%dT%H:%M'),
    })


@login_required(login_url='login')
@role_required('INVOICES_ADD_INVOICE')
def save_free_invoice(request):
    """
    Bağımsız serbest fatura kaydetme endpoint'i.
    POST body: {
        customer_id, issue_date, notes,
        items: [{product_name, quantity, unit, unit_price, vat_rate, notes}]
    }
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Method Not Allowed'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz JSON verisi'}, status=400)

    store = _user_store(request)

    LIMIT_INVOICE_REQUIRED = Decimal('36000')
    LIMIT_MASAK = Decimal('185000')

    try:
        with transaction.atomic():
            # Müşteri
            customer = None
            customer_id = payload.get('customer_id')
            if customer_id:
                customer = Customers.objects.filter(id=customer_id, store=store).first()

            # Tarih
            # KRİTİK: Bu projede USE_TZ=False → timezone.now() naive datetime döner.
            # make_aware() KULLANMA — aware/naive karışıklığı TypeError verir.
            # datetime-local input: "2026-03-24T14:30" (timezone bilgisi YOK → naive).
            issue_date = timezone.now()
            issue_date_str = payload.get('issue_date')
            if issue_date_str:
                try:
                    dt = datetime.fromisoformat(issue_date_str.replace('Z', '+00:00'))
                    # Aware gelirse (Z veya +offset), yerel naive'e çevir
                    if timezone.is_aware(dt):
                        dt = timezone.make_naive(dt, timezone.get_current_timezone())
                    issue_date = dt
                except (ValueError, TypeError):
                    issue_date = timezone.now()

            # ── GİB 7 Gün Kuralı: Fatura tarihi validasyonu (saf date objesi) ──
            # Her iki date objesi de naive → TypeError riski SIFIR.
            today = timezone.now().date()
            min_allowed = today - timedelta(days=7)
            issue_date_only = issue_date.date() if hasattr(issue_date, 'date') else issue_date

            if issue_date_only > today:
                return JsonResponse({
                    'result': False, 'error': True,
                    'error_msg': 'Fatura tarihi bugünden ileri bir tarih olamaz.',
                }, status=200)

            if issue_date_only < min_allowed:
                return JsonResponse({
                    'result': False, 'error': True,
                    'error_msg': 'Fatura tarihi bugünden en fazla 7 gün geriye tarihlenebilir (GİB 7 Gün Kuralı).',
                }, status=200)

            # Fatura numarası
            invoice_no, seq = Invoice.next_number_for(store, doc_class=Invoice.DocumentClass.PROFORMA)

            inv = Invoice.objects.create(
                store=store,
                customer=customer,
                invoice_no=invoice_no,
                sequence_no=seq,
                issue_date=issue_date,
                invoice_type=Invoice.Type.SALE,
                doc_class=Invoice.DocumentClass.PROFORMA,
                status=Invoice.Status.DRAFT,
                notes=payload.get('notes', ''),
            )

            items_data = payload.get('items') or []
            if not items_data:
                raise ValueError("En az bir fatura kalemi girilmelidir.")

            valid_item_count = 0
            for row in items_data:
                qty = q3(_safe_decimal(row.get('quantity', '0')))
                if qty <= 0:
                    continue

                unit_price = q3(_safe_decimal(row.get('unit_price', '0')))
                vat_rate = _safe_decimal(row.get('vat_rate', '0'))
                unit = row.get('unit', InvoiceItem.Unit.GRAM)
                product_name = (row.get('product_name') or '').strip() or 'Altın'
                item_notes = row.get('notes', '')
                is_gram_bullion = row.get('is_gram_bullion', True)

                item = InvoiceItem.objects.create(
                    invoice=inv,
                    product=None,
                    product_name=product_name,
                    quantity=qty,
                    unit=unit,
                    unit_price=unit_price,
                    vat_rate=vat_rate,
                    discount_rate=Decimal('0.00'),
                    notes=item_notes,
                    is_gram_bullion=is_gram_bullion,
                )
                item.recompute(save=True)
                valid_item_count += 1

            if valid_item_count == 0:
                raise ValueError("Geçerli kalem bulunamadı. Miktar sıfırdan büyük olmalıdır.")

            inv.recompute_totals(save=True)

            # Limit kontrolleri
            if inv.grand_total >= LIMIT_INVOICE_REQUIRED:
                if not customer or not customer.identification_number:
                    raise ValueError(
                        f"Fatura tutarı {LIMIT_INVOICE_REQUIRED:,.0f} TL üzerindedir. Müşteri TCKN zorunludur!")

            if inv.grand_total >= LIMIT_MASAK:
                has_img = customer and (
                    customer.identification_front_image or customer.identification_back_image
                )
                if not has_img:
                    raise ValueError(
                        f"MASAK Sınırı ({LIMIT_MASAK:,.0f} TL) aşıldı! Müşteri kimlik görseli zorunludur.")

            # ── Banka Hareketi Bağlantısı (Açık Bankacılık → Serbest Fatura) ──
            # Bankacılık ekranından bank_txn_id ile yönlendirildiyse,
            # faturayı o banka hareketine bağla ve durumunu güncelle.
            bank_txn_id = payload.get('bank_txn_id')
            if bank_txn_id:
                from apps.banking.models import BankTransaction as BankTxn
                try:
                    bank_txn = BankTxn.objects.select_for_update().get(
                        id=bank_txn_id, store=store
                    )
                    # İdempotency: Zaten faturası varsa tekrar bağlama
                    if not bank_txn.invoice_id:
                        bank_txn.invoice = inv
                        bank_txn.payment_status = 'PAID'
                        update_fields = ['invoice', 'payment_status', 'updated_on']

                        # Eğer müşteri atanmamışsa faturanın müşterisini ata
                        if not bank_txn.customer_id and customer:
                            bank_txn.customer = customer
                            bank_txn.match_status = 'MANUAL'
                            bank_txn.match_score = 100
                            update_fields.extend(['customer', 'match_status', 'match_score'])

                        bank_txn.save(update_fields=update_fields)
                        log.info(
                            "[FreeInvoice] Banka hareketi faturaya bağlandı: "
                            "bank_txn=%s → invoice=%s",
                            bank_txn_id, inv.invoice_no,
                        )
                except BankTxn.DoesNotExist:
                    log.warning(
                        "[FreeInvoice] Banka hareketi bulunamadı: bank_txn_id=%s",
                        bank_txn_id,
                    )

        return JsonResponse({
            'result': True,
            'invoice_id': str(inv.id),
            'invoice_no': inv.invoice_no,
            'grand_total': f'{q2(inv.grand_total):.2f}',
            'redirect_url': reverse('invoices:index'),
        })

    except ValueError as ve:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(ve)}, status=200)
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'result': False, 'error': True, 'error_msg': f'Kayıt hatası: {str(e)}'}, status=500)


# ======================================================================
# PERF-04: ASENKRON PDF İNDİRME AKIŞI (Zero Downtime)
# ----------------------------------------------------------------------
# Sync endpoint `invoice_pdf_download` dokunulmadan korunuyor; bu üç yeni
# endpoint frontend'in aşamalı olarak asenkron akışa geçmesi için sunuluyor.
# ======================================================================

@login_required(login_url='login')
@role_required('INVOICES_INVOICE_PDF_DOWNLOAD')
def invoice_pdf_async_request(request, record_id: uuid.UUID):
    """Fatura PDF üretimini Celery kuyruğuna atar, task_id döndürür."""
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Method Not Allowed'}, status=405)

    inv = get_object_or_404(Invoice, id=record_id, is_deleted=False, store=_user_store(request))

    from apps.invoices.tasks import render_invoice_pdf_task
    async_result = render_invoice_pdf_task.delay(str(inv.id))

    return JsonResponse({
        'result': True,
        'task_id': async_result.id,
        'invoice_id': str(inv.id),
        'status_url': reverse('invoices:pdf-async-status', args=[async_result.id]),
        'result_url': reverse('invoices:pdf-async-result', args=[inv.id]),
    })


@login_required(login_url='login')
@role_required('INVOICES_INVOICE_PDF_DOWNLOAD')
def invoice_pdf_async_status(request, task_id: str):
    """Celery task durumunu raporlar. Frontend bu endpoint'i polling ile çağırır."""
    from celery.result import AsyncResult
    res = AsyncResult(task_id)

    payload = {'task_id': task_id, 'state': res.state, 'ready': res.ready()}

    if res.ready():
        try:
            info = res.result
            if isinstance(info, dict):
                payload['ok'] = bool(info.get('ok'))
                payload['error'] = info.get('error')
                payload['url'] = info.get('url')
                payload['invoice_no'] = info.get('invoice_no')
            else:
                payload['ok'] = res.successful()
        except Exception as e:
            payload['ok'] = False
            payload['error'] = f'{type(e).__name__}: {e}'

    return JsonResponse(payload)


@xframe_options_sameorigin
@login_required(login_url='login')
@role_required('INVOICES_INVOICE_PDF_DOWNLOAD')
def invoice_pdf_async_result(request, record_id: uuid.UUID):
    """Hazır PDF dosyasını stream eder. Henüz üretilmemişse 404 döner."""
    inv = get_object_or_404(Invoice, id=record_id, is_deleted=False, store=_user_store(request))

    cache_path = os.path.join(settings.MEDIA_ROOT, 'Invoices', 'pdf_cache', f"{inv.id}.pdf")
    if not os.path.isfile(cache_path):
        return JsonResponse(
            {'error': True, 'error_msg': 'PDF henüz hazır değil. Önce /pdf/async ile üretimi tetikleyin.'},
            status=404
        )

    with open(cache_path, 'rb') as f:
        data = f.read()
    response = HttpResponse(data, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{inv.invoice_no}.pdf"'
    return response


