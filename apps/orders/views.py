
# apps/orders/views.py
from __future__ import annotations
import json
from decimal import Decimal
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.orders.models import *
from apps.stores.models import Stores
from apps.whatsapp.models import WhatsAppCreditRequest
from apps.invoices.models import EInvoiceCreditRequest


def _is_orders_admin(u):
    return u.is_superuser or u.is_staff


@login_required(login_url="login")
@user_passes_test(_is_orders_admin)
def index(request):
    return render(request, "management/orders/index.html", {"title": "Siparişler"})


@login_required(login_url="login")
@user_passes_test(_is_orders_admin)
def detail(request, pk):
    """Sipariş Detay / Özet Sayfası"""
    order = get_object_or_404(Order.objects.select_related('store', 'requester', 'approver', 'proposal'), pk=pk)
    return render(request, "management/orders/detail.html", {
        "title": f"Sipariş Detayı - {order.order_no}",
        "order": order
    })


@login_required(login_url="login")
@user_passes_test(_is_orders_admin)
@require_GET
def api_orders_list(request):
    status_f = (request.GET.get("status") or "").upper()
    type_f = (request.GET.get("type") or "").upper()

    qs = (Order.objects
          .select_related("store", "requester", "approver", "proposal")
          .prefetch_related("items")
          .order_by("-created_at"))

    if status_f in dict(Order.Status.choices):
        qs = qs.filter(status=status_f)
    if type_f in dict(Order.Type.choices):
        qs = qs.filter(order_type=type_f)

    data = []
    for o in qs:
        it = o.items.first()
        qty = it.quantity if it else 0
        type_label = o.get_order_type_display()

        amount_label = ""
        if o.order_type == Order.Type.WA_CREDIT:
            amount_label = f"{qty} kontör"
        elif o.order_type == Order.Type.EINVOICE_CREDIT:
            amount_label = f"{qty} kontör (e-Fatura) [Kaldırıldı]"
        elif o.order_type == Order.Type.PACKAGE and it and it.package:
            amount_label = f"{qty} ay · {it.package.name}"
        elif o.order_type == Order.Type.PROPOSAL:
            item_count = o.items.count()
            prop_no = o.proposal.proposal_no if o.proposal else ""
            amount_label = f"{item_count} kalem ({prop_no})"

        if getattr(o, "sequence_no", None):
            short_no = f"{o.sequence_no:06d}"
        else:
            ono = o.order_no or ""
            short_no = ono[-6:] if len(ono) >= 6 else ono

        # Detay URL
        detail_url = reverse('orders:detail', args=[o.id])

        data.append({
            "id": str(o.id),
            "created_at": o.created_at.isoformat(),
            "order_no": o.order_no,
            "short_no": short_no,
            "detail_url": detail_url,
            "store_label": o.store.title or o.store.email or o.store.store_id,
            "type": o.order_type,
            "type_label": type_label,
            "amount": amount_label,
            "status": o.status,
            "payment_status": o.payment_status,
            "paid": o.payment_status == Order.PaymentStatus.PAID,
            "note": o.notes or "",
            "decided_by": (o.approver and o.approver.get_full_name()) or "",
            "decided_at": o.decided_at.isoformat() if o.decided_at else None,
        })

    return JsonResponse({"data": data})


@login_required(login_url="login")
@user_passes_test(_is_orders_admin)
@require_POST
def api_order_decide(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        body = request.POST

    oid = body.get("id")
    action = (body.get("action") or "").lower()
    note = (body.get("note") or "").strip()

    # Not: Amount güncellemesi sadece detay sayfasında değil, özel işlemde yapılır.
    # Burası sadece onay/red mekanizmasıdır.

    if not (oid and action in ("approve", "reject")):
        return JsonResponse({"error": True, "error_msg": "Geçersiz parametre"}, status=400)

    od = get_object_or_404(Order.objects.select_related("store"), id=oid)

    # 1. KURAL: Karar verilmiş sipariş değiştirilemez.
    if od.status in (Order.Status.APPROVED, Order.Status.REJECTED, Order.Status.FULFILLED):
        return JsonResponse(
            {"error": True, "error_msg": f"Sipariş zaten sonuçlanmış ({od.get_status_display()}). İşlem yapılamaz."},
            status=400)

    # 2. KURAL: ONAY İÇİN ÖDEME ŞARTI
    if action == "approve":
        # Sipariş Ödenmediyse (PAID değilse) onaylanamaz.
        if od.payment_status != Order.PaymentStatus.PAID:
            return JsonResponse({
                "error": True,
                "error_msg": "ÖDEME EKSİK! Siparişin onaylanabilmesi için önce 'Ödeme Alındı' olarak işaretlenmesi gerekir."
            }, status=400)

    # RED İŞLEMİ
    if action == "reject":
        od.reject(by=request.user, note=note)
        # Bağlı talepleri de reddet
        it = od.items.first()
        if it:
            if it.wa_credit_request:
                it.wa_credit_request.status = WhatsAppCreditRequest.Status.REJECTED
                it.wa_credit_request.save(update_fields=["status"])
            if it.einv_credit_request:
                it.einv_credit_request.status = EInvoiceCreditRequest.Status.REJECTED
                it.einv_credit_request.save(update_fields=["status"])
        return JsonResponse({"result": True})

    # ONAY İŞLEMİ (Buraya geldiyse ödeme PAID demektir)
    od.approve(by=request.user, note=note)

    # Hizmeti Tamamla (Fulfill)
    od.fulfill()

    # Mağaza Aktivasyonu (Paket/Teklif ise)
    if od.order_type in (Order.Type.PACKAGE, Order.Type.PROPOSAL):
        if not od.store.is_active:
            od.store.is_active = True
            od.store.save(update_fields=["is_active"])

    return JsonResponse({"result": True})


@login_required(login_url="login")
@user_passes_test(_is_orders_admin)
@require_POST
def api_orders_bulk_decide(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "invalid json"}, status=400)

    ids = body.get("ids") or []
    action = (body.get("action") or "").lower()

    if not ids or not isinstance(ids, list):
        return JsonResponse({"error": True, "error_msg": "ids must be a list"}, status=400)
    if action not in ("approve", "reject"):
        return JsonResponse({"error": True, "error_msg": "invalid action"}, status=400)

    qs = (Order.objects
          .select_related("store")
          .prefetch_related("items")
          .filter(id__in=ids, status=Order.Status.PENDING))

    approved = rejected = skipped = 0

    for od in qs:
        try:
            if action == "reject":
                od.reject(by=request.user, note="Toplu Red")
                rejected += 1
            else:
                # Toplu Onayda Ödeme Kontrolü
                if od.payment_status != Order.PaymentStatus.PAID:
                    skipped += 1
                    continue  # Ödenmemişse atla

                od.approve(by=request.user, note="Toplu Onay")
                od.fulfill()
                if od.order_type in (Order.Type.PACKAGE, Order.Type.PROPOSAL) and not od.store.is_active:
                    od.store.is_active = True
                    od.store.save(update_fields=["is_active"])
                approved += 1
        except Exception:
            skipped += 1

    return JsonResponse({
        "result": True,
        "approved": approved,
        "rejected": rejected,
        "skipped": skipped,
        "total": len(ids),
    })


@login_required(login_url="login")
@user_passes_test(_is_orders_admin)
@require_POST
def api_orders_delete(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        body = request.POST

    ids = body.get("ids") or body.get("ids[]") or []
    if not ids: return JsonResponse({"error": True, "error_msg": "Silinecek kayıt yok."}, status=400)

    # Sadece PENDING olanlar silinebilir (Güvenlik)
    qs = Order.objects.filter(id__in=ids, status=Order.Status.PENDING)
    n = qs.count()
    qs.delete()
    return JsonResponse({"result": True, "deleted": n})


@require_POST
def api_order_set_paid(request):
    """
    Sadece ödeme durumunu günceller (PAID/UNPAID).
    Onay mekanizması ayrıdır.
    """
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "invalid json"}, status=400)

    oid = body.get("id")
    paid = body.get("paid")
    if oid is None or paid is None:
        return JsonResponse({"error": True, "error_msg": "id ve paid zorunlu"}, status=400)

    od = get_object_or_404(Order.objects.select_related("store"), id=oid)

    # Karar verilmiş siparişin ödemesi değiştirilemez (Muhasebe güvenliği)
    if od.status in (Order.Status.APPROVED, Order.Status.REJECTED, Order.Status.FULFILLED):
        return JsonResponse({"error": True, "error_msg": "Sonuçlanmış siparişin ödeme durumu değiştirilemez."},
                            status=400)

    now = timezone.now()

    if bool(paid):
        # Ödeme Alındı
        if hasattr(od, "mark_paid"):
            od.mark_paid()
        else:
            od.payment_status = Order.PaymentStatus.PAID
            od.save(update_fields=["payment_status"])

        note = f"{request.user.get_full_name()} tarafından 'Ödeme Alındı' işaretlendi."
        od.notes = (od.notes + "\n" + note).strip() if od.notes else note
        od.save(update_fields=["notes", "updated_at"])

    else:
        # Ödeme İptal
        od.payment_status = Order.PaymentStatus.UNPAID
        od.paid_total = Decimal("0.00")
        od.save(update_fields=["payment_status", "paid_total"])

        note = f"{request.user.get_full_name()} tarafından 'Ödeme İptal Edildi' işaretlendi."
        od.notes = (od.notes + "\n" + note).strip() if od.notes else note
        od.save(update_fields=["notes", "updated_at"])

    return JsonResponse({"result": True})