from __future__ import annotations

import json
from typing import Tuple

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max  # ⬅️ EKLE
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.whatsapp.services import *
from apps.whatsapp.services import _e164

from django.utils import timezone
from django.contrib.auth.decorators import user_passes_test
from apps.whatsapp.models import WhatsAppCreditRequest

from apps.orders.models import *
from decimal import Decimal
from apps.orders.models import *

log = logging.getLogger(__name__)


def resolve_default_store() -> Stores | None:
    """
    Tek WABA/hat senaryosu:
    - settings.META_PHONE_NUMBER_ID ile StoreWhatsAppEndpoint üzerinden mağazayı bul
    - yoksa ilk aktif mağazayı dön
    - bulunamazsa None
    """
    pnid = str(getattr(settings, "META_PHONE_NUMBER_ID", "") or "")
    if pnid:
        swe = (
            StoreWhatsAppEndpoint.objects
            .filter(phone_number_id=pnid, is_active=True)
            .select_related("store").first()
        )
        if swe:
            return swe.store

    st = Stores.objects.filter(is_deleted=False).order_by("id").first()
    return st  # bulunamazsa None


def _get_store_or_400(request) -> Tuple[Stores | None, JsonResponse | None]:
    """
    Öncelik: ?store_id= (GET/POST) -> ilgili mağaza
    Yoksa tek hat eşlemesi -> resolve_default_store()
    """
    sid = (request.GET.get("store_id") or request.POST.get("store_id") or "").strip()
    if sid:
        s = Stores.objects.filter(id=sid, is_deleted=False).first()
        if not s:
            return None, JsonResponse({"error": True, "error_msg": "store_id geçersiz."}, status=400)
        return s, None

    s = resolve_default_store()
    if not s:
        return None, JsonResponse(
            {"error": True, "error_msg": "WABA mağazası bulunamadı (PHONE_NUMBER_ID eşlemesi yapılandırılmalı)."},
            status=400
        )
    return s, None


@login_required(login_url="login")
@require_GET
def api_store_list(request):
    store, err = _get_store_or_400(request)
    if err: return err
    disp = StoreWhatsAppEndpoint.objects.filter(store=store, is_active=True).first()
    label = (disp.display_phone_number or disp.phone_number_id) if disp else f"Store #{store.id}"
    return JsonResponse({"items": [{"id": store.id, "label": label}]})
    # Not: tek hat → UI store_id gönderse de backend ignore ediyor.


@login_required(login_url="login")
@require_GET
def api_conversations(request):
    store, err = _get_store_or_400(request)
    if err: return err

    q = WhatsAppConversation.objects.filter(store=store)
    started_by = request.GET.get("started_by")
    if started_by in ("CUSTOMER", "STORE"):
        q = q.filter(started_by=started_by)

    q = q.order_by("-last_message_at")[:200]
    data = [{
        "id": c.id,
        "phone": c.phone_number,
        "status": c.status,
        "unread": c.unread_count,
        "last_at": c.last_message_at.isoformat() if c.last_message_at else None,
    } for c in q]
    return JsonResponse({"data": data})


# -------- API: conversation messages --------
@login_required(login_url="login")
@require_GET
def api_messages(request, conv_id: int):
    conv = get_object_or_404(WhatsAppConversation, id=conv_id)
    limit = int(request.GET.get("limit", 200))
    msgs = (
        WhatsAppChatMessage.objects
        .filter(conversation=conv)
        .order_by("-timestamp")[:limit]
    )
    data = [{
        "id": m.id,
        "dir": m.direction,
        "kind": m.kind,
        "text": m.text,
        "media": m.media,
        "from": m.from_number,
        "to": m.to_number,
        "ts": m.timestamp.isoformat(),
        "template": m.template_name,
        "status": m.status,
        "error": m.error,
    } for m in reversed(list(msgs))]

    if conv.unread_count:
        conv.unread_count = 0
        conv.save(update_fields=["unread_count", "updated_at"])

    return JsonResponse({"messages": data})


@login_required(login_url="login")
@require_POST
def api_send_text(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "invalid json"}, status=400)

    conv_id = body.get("conversation_id")
    text = (body.get("text") or "").strip()
    if not (conv_id and text):
        return JsonResponse({"error": True, "error_msg": "missing fields"}, status=400)

    conv = get_object_or_404(WhatsAppConversation, id=conv_id)
    ok, reason = send_whatsapp_text_guarded(
        store=conv.store, user=request.user, to=conv.phone_number, text=text,
        customer=None, return_reason=True
    )
    if not ok:
        reason_messages = {
            "CREDIT_EMPTY": "WhatsApp kontörünüz bitmiştir. Lütfen yeni kontör satın alın.",
            "DISABLED": "WhatsApp gönderimi bu mağaza için devre dışıdır.",
            "DAILY_LIMIT": "Günlük WhatsApp mesaj limitine ulaşıldı.",
            "MONTHLY_LIMIT": "Aylık WhatsApp mesaj limitine ulaşıldı.",
            "CONFIG": "WhatsApp API yapılandırması eksik. Lütfen yöneticinize başvurun.",
        }
        msg = reason_messages.get(reason, "Mesaj gönderilemedi.")
        return JsonResponse({"result": False, "reason": reason, "error_msg": msg})
    return JsonResponse({"result": True})


@login_required(login_url="login")
@require_GET
def api_usage(request):
    store, err = _get_store_or_400(request)
    if err:
        return err

    st, created = StoreWhatsAppSettings.objects.get_or_create(store=store)
    try:
        today = timezone.localdate()
    except:
        from datetime import date
        today = date.today()

    display_daily = st.daily_count
    display_monthly = st.monthly_count

    if st.daily_reset != today:
        display_daily = 0

    if st.monthly_reset is None or st.monthly_reset.month != today.month or st.monthly_reset.year != today.year:
        display_monthly = 0

    snap = {
        "daily": {
            "count": display_daily,
            "limit": st.daily_limit,
            "remaining": None,
            "progress": 0
        },
        "monthly": {
            "count": display_monthly,
            "limit": st.monthly_limit,
            "remaining": None,
            "progress": 0
        },
        "enabled": bool(st.enabled),
        "allowed": st.allowed_templates or [],
        "allowed_templates": st.allowed_templates or [],
        "credit_balance": st.credit_balance
    }

    return JsonResponse(snap)


@login_required(login_url="login")
@require_GET
def api_templates(request):
    store, err = _get_store_or_400(request)
    if err: return err

    st, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
    allowed = st.allowed_templates or []

    def _alias_for(tpl: "WhatsAppTemplateCatalog") -> str:
        meta = tpl.meta or {}
        return (meta.get("aliases", {}) or {}).get(str(store.id), "") or ""

    if request.user.is_superuser:
        # Superuser → Meta’dan tüm şablonları çek + katalogla senk
        rows = list_message_templates_grouped()  # services.py’deki fonksiyon
        names = [r["name"] for r in rows]
        cat = {t.name: t for t in WhatsAppTemplateCatalog.objects.filter(name__in=names)}

        items = []
        for r in rows:
            t = cat.get(r["name"])
            alias = _alias_for(t) if t else ""
            items.append({
                "name": r["name"],
                "title": r["name"],  # Meta title alanı yoksa adı göster
                "category": (r.get("category") or "").upper(),
                "languages": r.get("languages", []) or [],
                "alias": alias,
            })
        return JsonResponse({"items": items, "allowed": allowed, "readonly": False})

    # Normal kullanıcı → sadece allowlist + aktif katalog
    qs = WhatsAppTemplateCatalog.objects.filter(is_active=True, name__in=allowed)
    items = [{
        "name": t.name,
        "title": t.title or t.name,
        "category": t.category,
        "languages": t.languages,
        "alias": _alias_for(t),
    } for t in qs]
    return JsonResponse({"items": items, "allowed": allowed, "readonly": True})


@login_required(login_url="login")
@require_POST
def api_templates_save(request):
    if not request.user.is_superuser:
        return JsonResponse({"error": True, "error_msg": "forbidden"}, status=403)

    store, err = _get_store_or_400(request)
    if err:
        return err

    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "invalid json"}, status=400)

    allowed = body.get("allowed") or []
    aliases = body.get("aliases") or {}

    if not isinstance(allowed, list):
        return JsonResponse({"error": True, "error_msg": "allowed must be list"}, status=400)
    if not isinstance(aliases, dict):
        return JsonResponse({"error": True, "error_msg": "aliases must be object"}, status=400)

    st, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
    st.allowed_templates = allowed
    if "enabled" in body:
        st.enabled = bool(body.get("enabled"))
    st.save(update_fields=["allowed_templates", "enabled", "updated_at"])

    # alias kaydet (store-bazlı)
    for name, alias in aliases.items():
        tpl = WhatsAppTemplateCatalog.objects.filter(name=name).first()
        if not tpl:
            continue
        meta = tpl.meta or {}
        meta_aliases = meta.get("aliases", {})
        if alias:
            meta_aliases[str(store.id)] = alias
        else:
            meta_aliases.pop(str(store.id), None)
        meta["aliases"] = meta_aliases
        tpl.meta = meta
        tpl.save(update_fields=["meta"])

    return JsonResponse({"result": True, "allowed": allowed, "enabled": st.enabled})


@login_required(login_url="login")
@require_GET
def api_template_logs(request):
    store, err = _get_store_or_400(request)
    if err: return err

    name = request.GET.get("name") or ""
    qs = (
        WhatsAppMessageLog.objects
        .filter(store=store, template=name)
        .order_by("-created_at")[:50]
    )
    data = [{
        "id": x.id,
        "to": x.to,
        "status": x.status,
        "created_at": x.created_at.isoformat(),
        "err": x.error_message,
        "user": (x.user and f"{x.user.first_name} {x.user.last_name}") or "",
    } for x in qs]
    return JsonResponse({"name": name, "count": qs.count(), "data": data})


@login_required(login_url="login")
@require_POST
def api_start_chat(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "invalid json"}, status=400)

    phone = _e164(body.get("phone") or "")
    text = (body.get("text") or "").strip()
    if not phone:
        return JsonResponse({"error": True, "error_msg": "phone zorunludur."}, status=400)

    store, err = _get_store_or_400(request)
    if err:
        return err

    conv = get_or_create_conversation(store, phone)
    ok = True
    reason = None
    if text:
        ok, reason = send_whatsapp_text_guarded(
            store=store, user=request.user, to=phone, text=text,
            customer=None, return_reason=True
        )

    conv.last_message_at = timezone.now()
    conv.save(update_fields=["last_message_at", "updated_at"])

    if not ok and reason:
        reason_messages = {
            "CREDIT_EMPTY": "WhatsApp kontörünüz bitmiştir. Lütfen yeni kontör satın alın.",
            "DISABLED": "WhatsApp gönderimi bu mağaza için devre dışıdır.",
            "DAILY_LIMIT": "Günlük WhatsApp mesaj limitine ulaşıldı.",
            "MONTHLY_LIMIT": "Aylık WhatsApp mesaj limitine ulaşıldı.",
            "CONFIG": "WhatsApp API yapılandırması eksik. Lütfen yöneticinize başvurun.",
        }
        msg = reason_messages.get(reason, "Mesaj gönderilemedi.")
        return JsonResponse({"result": False, "reason": reason, "error_msg": msg, "conversation_id": conv.id})
    return JsonResponse({"result": ok, "conversation_id": conv.id})


@login_required(login_url="login")
@require_GET
def api_user_totals(request):
    store, err = _get_store_or_400(request)
    if err: return err

    # Yalnızca OUT (mağaza gönderimleri) sayılır
    agg = (
        WhatsAppChatMessage.objects
        .filter(store=store, direction="OUT")
        .values("user_id")
        .annotate(sent_count=Count("id"), last_at=Max("timestamp"))
        .order_by("-sent_count")
    )

    # Kullanıcı adlarını bağla
    from apps.accounts.models import Users
    users = {u.id: u for u in Users.objects.filter(id__in=[x["user_id"] for x in agg if x["user_id"]])}

    items = []
    for row in agg:
        u = users.get(row["user_id"])
        if u:
            name = (u.first_name or "") + " " + (u.last_name or "")
            name = name.strip() or (u.username or f"User #{u.id}")
        else:
            name = "Sistem/Belirsiz"
        items.append({
            "user_id": row["user_id"],
            "name": name,
            "sent_count": row["sent_count"],
            "last_at": row["last_at"].isoformat() if row["last_at"] else None,
        })

    return JsonResponse({"items": items})


@login_required(login_url="login")
@require_GET
def api_store_quotas(request):
    store_id = request.GET.get("store_id")
    qs = Stores.objects.filter(is_deleted=False)
    if store_id:
        qs = qs.filter(id=store_id)

    qs = qs.order_by("id")
    items = []
    for s in qs:
        st, _ = StoreWhatsAppSettings.objects.get_or_create(store=s)
        # sayaçları güncel tut

        label = s.email or getattr(s, "store_id", f"Store #{s.id}")
        items.append({
            "store_id": str(s.id),
            "label": label,
            "enabled": st.enabled,
            # ⬇️ LIMITLER YERİNE KONTÖR BAKİYESİ
            "credit_balance": st.credit_balance,
            # bilgi amaçlı istatistikler
            "daily_count": st.daily_count,
            "monthly_count": st.monthly_count,
        })
    return JsonResponse({"items": items})


@login_required(login_url="login")
@require_POST
def api_store_quotas_save(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "invalid json"}, status=400)

    items = body.get("items") or []
    if not isinstance(items, list):
        return JsonResponse({"error": True, "error_msg": "items must be list"}, status=400)

    for x in items:
        sid = x.get("store_id")
        if not sid:
            continue
        s = Stores.objects.filter(id=sid, is_deleted=False).first()
        if not s:
            continue

        st, _ = StoreWhatsAppSettings.objects.get_or_create(store=s)

        # enabled
        if "enabled" in x:
            st.enabled = bool(x.get("enabled"))

        # ⬇️ YENİ: credit_balance (boş/null → sınırsız)
        if "credit_balance" in x:
            val = x.get("credit_balance")
            if val in ("", None):
                st.credit_balance = None
            else:
                try:
                    st.credit_balance = int(val)
                except Exception:
                    return JsonResponse({"error": True, "error_msg": "credit_balance must be integer or null"},
                                        status=400)

        # ⬇️ GERİ UYUMLULUK: Eski anahtarlar gelirse yok say (değiştirmiyoruz)
        # st.daily_limit / st.monthly_limit artık kotayı belirlemiyor.

        st.save(update_fields=["enabled", "credit_balance", "updated_at"])

    return JsonResponse({"result": True})


@login_required(login_url="login")
@require_GET
def api_credit_requests(request):
    store, err = _get_store_or_400(request)
    if err:
        return err
    if not (request.user.is_superuser or getattr(request.user, "store_id", None) == store.id):
        return JsonResponse({"error": True, "error_msg": "forbidden"}, status=403)

    status_f = (request.GET.get("status") or "").upper().strip()

    qs = (WhatsAppCreditRequest.objects
          .select_related("store", "requester", "decided_by")
          .filter(store=store))

    valid_statuses = dict(WhatsAppCreditRequest.Status.choices)
    if status_f and status_f != "ALL" and status_f in valid_statuses:
        qs = qs.filter(status=status_f)

    qs = qs.order_by("-created_at")

    data = []
    for r in qs:
        data.append({
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "requester": (r.requester and r.requester.get_full_name()) or (getattr(r.requester, "username", "") or ""),
            "requested_amount": r.requested_amount,
            "note": r.note or "",
            "status": r.status,
            "decided_by": (r.decided_by and r.decided_by.get_full_name()) or "",
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "decision_note": r.decision_note or "",
        })
    return JsonResponse({"data": data})


@login_required(login_url="login")
@require_POST
def api_credit_request_create(request):
    store, err = _get_store_or_400(request)
    if err:
        return err
    if not (request.user.is_superuser or getattr(request.user, "store_id", None) == store.id):
        return JsonResponse({"error": True, "error_msg": "forbidden"}, status=403)

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

    if amount == 2000:
        total_price = 1800
    elif amount == 1000:
        total_price = 900
    elif amount == 500:
        total_price = 450
    else:
        total_price = amount

    calculated_unit_price = Decimal(total_price) / Decimal(amount)

    rec = WhatsAppCreditRequest.objects.create(
        store=store,
        requester=request.user,
        requested_amount=amount,
        note=note,
        status=WhatsAppCreditRequest.Status.PENDING
    )

    od = create_order_from_wa_credit_request(
        req=rec,
        unit_price=calculated_unit_price,
        currency=MoneyCurrency.TRY,
        note=f"WhatsApp Kontör Talebi ({amount} Adet)"
    )

    return JsonResponse({
        "result": True,
        "id": rec.id,
        "order_no": od.order_no
    })
