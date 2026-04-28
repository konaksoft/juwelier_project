from __future__ import annotations
import json
from decimal import Decimal
from typing import Tuple

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from apps.stores.models import Stores
from apps.accounts.models import Users
from apps.invoices.models import StoreEInvoiceSettings, EInvoiceCreditRequest
from apps.orders.models import *
from apps.invoices.models import Invoice


# ---------- helpers ----------

def _store_for_user(request) -> Tuple[Stores | None, JsonResponse | None]:
    """
    If ?store_id= given, use it; else use request.user.store (if any).
    Non-admins can only access their own store.
    """
    sid = (request.GET.get("store_id") or request.POST.get("store_id") or "").strip()
    if sid:
        s = Stores.objects.filter(id=sid, is_deleted=False).first()
        if not s:
            return None, JsonResponse({"error": True, "error_msg": "store_id geçersiz."}, status=400)
    else:
        s = getattr(request.user, "store", None)
        if not s:
            return None, JsonResponse({"error": True, "error_msg": "Mağaza bulunamadı."}, status=400)

    if not (request.user.is_superuser or getattr(request.user, "store_id", None) == s.id):
        return None, JsonResponse({"error": True, "error_msg": "forbidden"}, status=403)
    return s, None


# ---------- quota ----------

@login_required(login_url="login")
@require_GET
def api_einvoice_quota(request):
    store, err = _store_for_user(request)
    if err: return err

    st, _ = StoreEInvoiceSettings.objects.get_or_create(store=store)

    now = timezone.now()

    base_qs = Invoice.objects.filter(
        store=store,
        is_einvoice=True,
        is_deleted=False
    ).exclude(status='DRAFT')

    daily_count = base_qs.filter(issue_date__date=now.date()).count()
    monthly_count = base_qs.filter(issue_date__year=now.year, issue_date__month=now.month).count()

    return JsonResponse({
        "store_id": str(store.id),
        "enabled": bool(st.enabled),
        "credit_balance": st.credit_balance,
        "last_topup_at": st.last_topup_at.isoformat() if st.last_topup_at else None,
        "daily_count": daily_count,
        "monthly_count": monthly_count
    })


# ---------- request list ----------

@login_required(login_url="login")
@require_GET
def api_einvoice_requests(request):
    store, err = _store_for_user(request)
    if err: return err
    status_f = (request.GET.get("status") or "").upper()
    qs = (EInvoiceCreditRequest.objects
          .select_related("store", "requester", "decided_by")
          .filter(store=store))
    valid_status = dict(EInvoiceCreditRequest.Status.choices)
    if status_f and status_f != "ALL" and status_f in valid_status:
        qs = qs.filter(status=status_f)
    qs = qs.order_by("-created_at")
    data = [{
        "id": r.id,
        "created_at": r.created_at.isoformat(),
        "requester": (r.requester and r.requester.get_full_name()) or (getattr(r.requester, "username", "") or ""),
        "requested_amount": r.requested_amount,
        "note": r.note or "",
        "status": r.status,
        "decided_by": (r.decided_by and r.decided_by.get_full_name()) or "",
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_note": r.decision_note or "",
        # Order ile bağlandıysa UI tarafında göstermek isteyebilirsiniz:
        "order_id": getattr(getattr(r, "order_item", None), "order_id", None),
    } for r in qs]
    return JsonResponse({"data": data})


# ---------- request create (Order oluşturur) ----------

@login_required(login_url="login")
@require_POST
def api_einvoice_request_create(request):
    store, err = _store_for_user(request)
    if err: return err

    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        body = request.POST

    try:
        amount = int(body.get("amount") or 0)
    except Exception:
        return JsonResponse({"error": True, "error_msg": "Geçersiz miktar"}, status=400)
    if amount <= 0:
        return JsonResponse({"error": True, "error_msg": "Miktar > 0 olmalı"}, status=400)

    note = (body.get("note") or "").strip()
    unit_price = Decimal(str(body.get("unit_price") or "1.00"))  # fiyat politikanıza göre set edin

    # 1) Talebi oluştur
    rec = EInvoiceCreditRequest.objects.create(
        store=store,
        requester=request.user,
        requested_amount=amount,
        note=note,
        status=EInvoiceCreditRequest.Status.PENDING
    )
    # 2) Talebe bağlı Order(EINVOICE_CREDIT) oluştur
    od = create_order_from_einvoice_credit_request(
        req=rec,
        unit_price=unit_price,
        currency=MoneyCurrency.TRY,
        note="e-Fatura Kontör Talebi"
    )
    return JsonResponse({"result": True, "request_id": rec.id, "order_id": str(od.id), "order_no": od.order_no})
