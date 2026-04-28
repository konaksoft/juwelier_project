from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.dateparse import parse_date
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from rest_framework import viewsets, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.invoices.models import Invoice, q2
from apps.stores.models import Stores

# Bu importlar projenizde mevcutsa kalabilir, yoksa kendi client yollarınızı kontrol edin
from apps.pavo.clients import PavoClient
from apps.pavo.local_client import PavoLocalClient, build_jewellery_sale_payload_from_invoice
from apps.pavo.serializers import InvoiceSerializer


# ------------------------------
# Yardımcılar
# ------------------------------
def _user_store(request) -> Stores:
    st = getattr(request.user, "store", None)
    if not st:
        # Eğer request.user.store yoksa, fallback veya hata yönetimi
        raise ValueError("Kullanıcıya ait mağaza (store) bulunamadı.")
    return st


def _safe_decimal(v, default="0"):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


# ------------------------------
# Invoice (salt-okunur API)
# ------------------------------
class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    # PERF-02: Serializer alanlarına göre N+1 önlemi.
    # InvoiceSerializer: store, customer, supplier, process FK'leri + items (many) + items.product FK.
    queryset = (
        Invoice.objects
        .filter(is_deleted=False)
        .select_related('store', 'customer', 'supplier', 'process')
        .prefetch_related('items', 'items__product')
    )
    serializer_class = InvoiceSerializer


# ------------------------------
# Pavo Cloud Ödeme
# ------------------------------
class PavoCreatePaymentView(APIView):
    def post(self, request):
        invoice_id = request.data.get('invoice_id')
        amount = request.data.get('amount')
        currency = request.data.get('currency') or 'TRY'
        description = request.data.get('description')

        try:
            inv = Invoice.objects.get(id=invoice_id, is_deleted=False)
        except Invoice.DoesNotExist:
            return Response({'detail': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)

        payable = inv.balance  # Modeldeki property kullanıldı
        amt = Decimal(str(amount)) if amount is not None else payable

        if amt <= 0:
            return Response({'detail': 'Nothing to pay'}, status=status.HTTP_400_BAD_REQUEST)

        client = PavoClient()
        try:
            out = client.create_payment(
                external_id=inv.invoice_no,
                amount=amt,
                currency=currency,
                description=description
            )
            # Notlara Pavo linkini ekle
            inv.notes = (inv.notes or '') + f"\nPAVO:{out.get('pavo_id')}:{out.get('payment_url')}"
            inv.save(update_fields=['notes'])

            return Response({
                'pavo_id': out.get('pavo_id'),
                'payment_url': out.get('payment_url')
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PavoPaymentStatusView(APIView):
    def get(self, request, pavo_id: str):
        client = PavoClient()
        try:
            data = client.payment_status(pavo_id)
            return Response(data)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@method_decorator(csrf_exempt, name='dispatch')
class PavoWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        raw = request.body or b''
        signature = request.headers.get('X-Pavo-Signature') or request.headers.get('Pavo-Signature')

        if not PavoClient.verify_webhook(signature, raw):
            return HttpResponse(status=401)

        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            return HttpResponse(status=400)

        external_id = str(payload.get('external_id') or '')
        status_text = str(payload.get('status') or '').upper()
        paid_amount = Decimal(str(payload.get('paid_amount') or '0'))

        if not external_id:
            return HttpResponse(status=400)

        try:
            with transaction.atomic():
                # select_for_update ile kilitle
                inv = Invoice.objects.select_for_update().get(invoice_no=external_id, is_deleted=False)

                if status_text in ['PAID', 'SUCCEEDED', 'APPROVED'] and paid_amount > 0:
                    new_paid = inv.paid_total + paid_amount

                    # Fazla ödeme kontrolü
                    if new_paid > inv.grand_total:
                        new_paid = inv.grand_total

                    inv.paid_total = new_paid

                    # Eğer tamamı ödendiyse durumu güncelle
                    if inv.is_paid:
                        inv.status = Invoice.Status.ISSUED  # veya APPROVED
                        inv.save(update_fields=['paid_total', 'status', 'updated_at'])
                    else:
                        inv.save(update_fields=['paid_total', 'updated_at'])

        except Invoice.DoesNotExist:
            return HttpResponse(status=404)

        return HttpResponse(status=200)


# ------------------------------
# Pavo Lokal Cihaz (Pairing/JewellerySale vb.)
# ------------------------------
class PavoLocalPairView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ip = request.data.get("ip")
        secure = bool(request.data.get("secure", True))
        serial_number = request.data.get("serial_number")
        fingerprint = request.data.get("fingerprint")
        port = request.data.get("port")
        raw_handle = request.data.get("raw_handle")

        if not (ip and serial_number and fingerprint):
            return Response({"detail": "ip, serial_number, fingerprint zorunlu"}, status=400)

        client = PavoLocalClient(ip=ip, secure=secure, serial_number=serial_number, fingerprint=fingerprint, port=port)
        try:
            if raw_handle and isinstance(raw_handle, dict):
                data = client.pairing_with_handle(raw_handle)
            else:
                data = client.pairing()
        except Exception as e:
            return Response({"detail": str(e)}, status=502)
        return Response(data, status=200)


class PavoLocalJewellerySaleView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        invoice_id = request.data.get("invoice_id")
        ip = request.data.get("ip")
        secure = bool(request.data.get("secure", True))
        serial_number = request.data.get("serial_number")
        fingerprint = request.data.get("fingerprint")
        port = request.data.get("port")

        if not all([invoice_id, ip, serial_number, fingerprint]):
            return Response({"detail": "invoice_id, ip, serial_number, fingerprint zorunlu"}, status=400)

        try:
            # prefetch_related ile items çekiliyor
            inv = Invoice.objects.prefetch_related("items").get(id=invoice_id, is_deleted=False)
        except Invoice.DoesNotExist:
            return Response({"detail": "Invoice not found"}, status=404)

        client = PavoLocalClient(ip=ip, secure=secure, serial_number=serial_number, fingerprint=fingerprint, port=port)

        # Helper fonksiyonunuzu kullanarak payload oluşturma
        try:
            sale_payload = build_jewellery_sale_payload_from_invoice(inv)
            data = client.jewellery_sale(sale_payload)

            # Başarılı satış sonrası fatura üzerine Pavo bilgilerini işleyebiliriz
            if data and 'SaleNumber' in data:  # Pavo'dan dönen response yapısına göre değişebilir
                inv.pavo_sale_number = data.get('SaleNumber')
                inv.save(update_fields=['pavo_sale_number'])

        except Exception as e:
            return Response({"detail": str(e)}, status=502)

        return Response({"request": sale_payload, "response": data}, status=200)


class PavoLocalGetSaleResultView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ip = request.data.get("ip")
        secure = bool(request.data.get("secure", True))
        serial_number = request.data.get("serial_number")
        fingerprint = request.data.get("fingerprint")
        port = request.data.get("port")
        extra = request.data.get("extra") or {}

        if not (ip and serial_number and fingerprint):
            return Response({"detail": "ip, serial_number, fingerprint zorunlu"}, status=400)

        client = PavoLocalClient(ip=ip, secure=secure, serial_number=serial_number, fingerprint=fingerprint, port=port)
        try:
            data = client.get_sale_result(extra)
        except Exception as e:
            return Response({"detail": str(e)}, status=502)
        return Response(data, status=200)


class PavoLocalCancelView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ip = request.data.get("ip")
        secure = bool(request.data.get("secure", True))
        serial_number = request.data.get("serial_number")
        fingerprint = request.data.get("fingerprint")
        port = request.data.get("port")
        extra = request.data.get("extra") or {}

        if not (ip and serial_number and fingerprint):
            return Response({"detail": "ip, serial_number, fingerprint zorunlu"}, status=400)

        client = PavoLocalClient(ip=ip, secure=secure, serial_number=serial_number, fingerprint=fingerprint, port=port)
        try:
            data = client.cancel_sale(extra)
        except Exception as e:
            return Response({"detail": str(e)}, status=502)
        return Response(data, status=200)


# Mock / Demo: lokal ödeme tamamlandı say
@csrf_exempt
@login_required(login_url='login')
def pavo_local_jewellery_sale(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'detail': 'invalid method'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'result': False, 'error': True, 'detail': 'json'}, status=400)

    invoice_id = body.get('invoice_id')
    with transaction.atomic():
        # select_for_update: ayni fatura ayni anda iki POST tarafindan
        # okunursa ikinci istek ilk tamamlanana kadar bekler (deadlock yok,
        # sira bekler). Cift odeme ve negatif bakiye onlenir.
        inv = Invoice.objects.select_for_update().filter(
            id=invoice_id, is_deleted=False, store=_user_store(request)
        ).first()

        if not inv:
            return JsonResponse({'result': False, 'error': True, 'detail': 'Fatura bulunamadi.'}, status=404)

        # Idempotency: fatura zaten odendiyse tekrar isleme
        if inv.paid_total >= inv.grand_total:
            return JsonResponse({'result': True, 'response': {'Message': 'Fatura zaten odenmis durumda.'}})

        inv.paid_total = q2(inv.grand_total)
        inv.status = Invoice.Status.ISSUED
        inv.save(update_fields=['paid_total', 'status', 'updated_at'])

    return JsonResponse({'result': True, 'response': {'Message': 'Ödeme isteği alındı, onaylandı.'}})


# ------------------------------
# E-Doc (e-Fatura / e-Arşiv) Mock Endpoint’ler
# ------------------------------
@csrf_exempt
@login_required(login_url='login')
def edoc_send(request):
    """
    Seçili faturaları GİB'e gönderir (Simülasyon).
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'detail': 'invalid method'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'result': False, 'error': True, 'detail': 'json'}, status=400)

    ids = body.get('invoice_ids') or []
    edoc_type = (body.get('edoc_type') or 'EARSIV').upper()

    with transaction.atomic():
        for iid in ids:
            try:
                inv = Invoice.objects.get(id=iid, is_deleted=False, store=_user_store(request))
            except Invoice.DoesNotExist:
                continue

            # E-Fatura işaretini güncelle
            inv.is_einvoice = (edoc_type == 'EFATURA')

            # GİB UUID oluştur ve durumu güncelle
            # NOT: Modelinizde gib_status alanı YOK. gib_status_code kullanıyoruz.
            inv.gib_uuid = str(uuid.uuid4())
            inv.gib_status_code = '1000'  # Örn: 1000 (Zarf Kuyruğa Eklendi)
            inv.gib_status_desc = 'GİB\'e Gönderildi'
            inv.status = Invoice.Status.SENT

            inv.save(
                update_fields=['is_einvoice', 'gib_uuid', 'gib_status_code', 'gib_status_desc', 'status', 'updated_at'])

    return JsonResponse({'result': True})


@csrf_exempt
@login_required(login_url='login')
def edoc_status(request):
    """
    Faturaların GİB durumunu sorgular ve listeler.
    """
    if request.method == 'GET':
        draw = int(request.GET.get('draw', 1))
        start = int(request.GET.get('start', 0))
        length = int(request.GET.get('length', 25))
        search_value = (request.GET.get('search[value]') or '').strip()

        # PERF-02: sunucu tarafı üst limit
        MAX_PAGE_SIZE = 500
        if length == -1 or length > MAX_PAGE_SIZE:
            length = MAX_PAGE_SIZE
        if length <= 0:
            length = 25
        if start < 0:
            start = 0
        status_filter = (request.GET.get('status') or '').upper()  # Bu artık GİB kodları olabilir (1200, 1300 vb.)
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')

        store = _user_store(request)

        # Only alanları güncellendi (gib_status yerine gib_status_code)
        qs = (Invoice.objects
              .filter(is_deleted=False, store=store)
              .only('id', 'invoice_no', 'issue_date', 'is_einvoice', 'gib_uuid', 'gib_status_code', 'gib_status_desc'))

        total = qs.count()

        if search_value:
            qs = qs.filter(Q(invoice_no__icontains=search_value) | Q(gib_uuid__icontains=search_value))

        if status_filter:
            # status_filter artık gib_status_code ile eşleşmeli
            qs = qs.filter(gib_status_code=status_filter)

        if date_from:
            qs = qs.filter(issue_date__date__gte=parse_date(date_from))
        if date_to:
            qs = qs.filter(issue_date__date__lte=parse_date(date_to))

        count = qs.count()

        page_qs = qs.order_by('-issue_date')[start:start + length]

        data = []
        for inv in page_qs:
            data.append({
                'invoice_id': str(inv.id),
                'invoice_no': inv.invoice_no,
                'edoc_type': 'EFATURA' if bool(inv.is_einvoice) else 'EARSIV',
                'provider': 'Pavo',
                'gib_uuid': inv.gib_uuid or '',
                'edoc_status': inv.gib_status_code or '',  # Code döndürüyoruz
                'edoc_status_desc': inv.gib_status_desc or '',  # Açıklama da eklendi
            })
        return JsonResponse({'draw': draw, 'recordsTotal': total, 'recordsFiltered': count, 'data': data})

    if request.method == 'POST':
        # Statü sorgulama (Manuel tetikleme)
        try:
            body = json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'result': False, 'error': True, 'detail': 'json'}, status=400)

        ids = body.get('invoice_ids') or []
        store = _user_store(request)

        if ids:
            qs = Invoice.objects.filter(id__in=ids, store=store, is_deleted=False)
        else:
            # Onaylanmamışları sorgula (1300 = Başarıyla Tamamlandı diyelim)
            qs = Invoice.objects.filter(store=store, is_deleted=False).exclude(gib_status_code='1300')

        with transaction.atomic():
            for inv in qs:
                if not inv.gib_uuid:
                    # UUID yoksa oluştur ve gönderildi yap
                    inv.gib_uuid = str(uuid.uuid4())
                    inv.gib_status_code = '1000'
                    inv.gib_status_desc = 'Kuyruğa Eklendi'
                    inv.status = Invoice.Status.SENT
                else:
                    # Varsa simülasyon gereği Onaylandı yap
                    inv.gib_status_code = '1300'
                    inv.gib_status_desc = 'Başarıyla Tamamlandı'
                    inv.status = Invoice.Status.APPROVED

                inv.save(update_fields=['gib_uuid', 'gib_status_code', 'gib_status_desc', 'status', 'updated_at'])

        return JsonResponse({'result': True})

    return HttpResponseBadRequest('method')


@csrf_exempt
@login_required(login_url='login')
def edoc_cancel(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'detail': 'invalid method'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'result': False, 'error': True, 'detail': 'json'}, status=400)

    ids = body.get('invoice_ids') or []
    reason = (body.get('reason') or '').strip()
    store = _user_store(request)

    with transaction.atomic():
        for iid in ids:
            try:
                inv = Invoice.objects.get(id=iid, is_deleted=False, store=store)
            except Invoice.DoesNotExist:
                continue

            inv.gib_status_code = '1230'  # Örn: İptal edildi kodu (Dummy)
            inv.gib_status_desc = 'Kullanıcı İptali'
            inv.gib_error = reason
            inv.status = Invoice.Status.CANCELED

            inv.save(update_fields=['gib_status_code', 'gib_status_desc', 'gib_error', 'status', 'updated_at'])

    return JsonResponse({'result': True})


@login_required(login_url='login')
def edoc_download_pdf(request):
    invoice_id = request.GET.get('invoice_id')
    inv = get_object_or_404(Invoice, id=invoice_id, is_deleted=False, store=_user_store(request))
    # Invoices app urls dosyanızda 'download' isminde bir url path olmalı
    return redirect('invoices:download', record_id=inv.id)


@login_required(login_url='login')
def edoc_download_xml(request):
    invoice_id = request.GET.get('invoice_id')
    inv = get_object_or_404(Invoice, id=invoice_id, is_deleted=False, store=_user_store(request))

    edoc_type = 'EFATURA' if bool(inv.is_einvoice) else 'EARSIV'

    # XML içeriği yeni modele göre düzenlendi
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNo>{inv.invoice_no}</InvoiceNo>
  <IssueDate>{inv.issue_date.strftime('%Y-%m-%dT%H:%M:%S') if inv.issue_date else ''}</IssueDate>
  <Type>{edoc_type}</Type>
  <Currency>{inv.currency}</Currency>
  <Totals>
    <SubTotal>{q2(inv.subtotal):.2f}</SubTotal>
    <DiscountTotal>{q2(inv.discount_total):.2f}</DiscountTotal>
    <TaxTotal>{q2(inv.tax_total):.2f}</TaxTotal>
    <GrandTotal>{q2(inv.grand_total):.2f}</GrandTotal>
    <PaidTotal>{q2(inv.paid_total):.2f}</PaidTotal>
    <Balance>{q2(inv.balance):.2f}</Balance>
  </Totals>
  <GIB>
    <UUID>{inv.gib_uuid or ''}</UUID>
    <StatusCode>{inv.gib_status_code or ''}</StatusCode>
    <StatusDesc>{inv.gib_status_desc or ''}</StatusDesc>
  </GIB>
</Invoice>"""

    resp = HttpResponse(xml, content_type='application/xml; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="invoice-{inv.invoice_no}.xml"'
    return resp
