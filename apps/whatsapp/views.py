from __future__ import annotations
import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.whatsapp.models import WhatsAppConversation, WhatsAppChatMessage
from apps.whatsapp.services import get_or_create_conversation  # services'ta var
from apps.whatsapp.services import send_whatsapp_template_guarded, MessageType

logger = logging.getLogger(__name__)


@login_required(login_url="login")
def dashboard(request):
    """WhatsApp ana sayfa (Inbox + Kullanım + Şablonlar)."""
    return render(request, "management/whatsapp/dashboard.html", {"title": "WhatsApp"})


@login_required(login_url="login")
def conversation_detail(request, pk: int):
    conv = get_object_or_404(WhatsAppConversation.objects.select_related("store"), pk=pk)
    # mağaza sahibi kendi sohbetini açtıysa okunmamış sayacı sıfırla
    if getattr(request.user, "store_id", None) == conv.store_id and conv.unread_count:
        conv.unread_count = 0
        conv.save(update_fields=["unread_count"])
    return render(request, "management/whatsapp/conversation_detail.html", {
        "title": f"Sohbet · {conv.phone_number}",
        "conversation": conv,
    })


@csrf_exempt
def meta_whatsapp_webhook(request):
    # Meta doğrulama
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        if mode == "subscribe" and token == (settings.META_VERIFY_TOKEN or ""):
            return HttpResponse(challenge)
        return HttpResponseForbidden("Verification failed")

    # İnbound mesaj akışı
    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            return HttpResponseBadRequest("invalid json")

        try:
            entry = (payload.get("entry") or [{}])[0]
            change = (entry.get("changes") or [{}])[0]
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "")
            display_num = metadata.get("display_phone_number", "")

            # phone_number_id → mağaza bul
            from apps.whatsapp.models import StoreWhatsAppEndpoint
            swe = (StoreWhatsAppEndpoint.objects
                   .filter(phone_number_id=phone_number_id, is_active=True)
                   .select_related("store").first())
            store = swe.store if swe else None

            # fallback: sistemdeki ilk aktif mağaza (tek hatlı kurulumlarda iş görür)
            if store is None:
                from apps.stores.models import Stores
                store = Stores.objects.filter(is_deleted=False).first()

            # Mesajları işle
            for m in value.get("messages") or []:
                from_number = m.get("from")  # müşteri MSISDN (E.164'suz gelebilir)
                msg_type = m.get("type")
                text = ""
                media = {}

                if msg_type == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif msg_type in ("image", "audio", "video", "document", "sticker"):
                    media = {"type": msg_type, **(m.get(msg_type) or {})}
                    text = (m.get("caption") or "") if "caption" in m else ""
                elif msg_type == "button":
                    text = (m.get("button") or {}).get("text", "")
                elif msg_type == "interactive":
                    # buton/menü seçimleri
                    text = json.dumps(m.get("interactive") or {}, ensure_ascii=False)

                if store and from_number:
                    from_e164 = "+" + from_number if not str(from_number).startswith("+") else str(from_number)
                    conv = get_or_create_conversation(store, from_e164)
                    # unread + son mesaj zamanı
                    conv.unread_count = (conv.unread_count or 0) + 1
                    conv.last_message_at = timezone.now()
                    conv.save(update_fields=["unread_count", "last_message_at", "updated_at"])

                    WhatsAppChatMessage.objects.create(
                        store=store,
                        conversation=conv,
                        user=None,
                        customer=None,
                        direction="IN",
                        kind=("MEDIA" if media else "TEXT"),
                        wa_message_id=m.get("id", ""),
                        from_number=from_e164,
                        to_number=display_num,
                        text=text or "",
                        media=media,
                    )
        except Exception as e:
            logger.exception("META WHATSAPP WEBHOOK parse error: %s", e)

        logger.info("META WHATSAPP PAYLOAD: %s", payload)
        return HttpResponse(status=200)

    return HttpResponseBadRequest("Method not allowed")


@login_required(login_url='login')
def send_meta_template_test(request):
    """
    Guard'lı template test (kota + politika + log). Only DEBUG.
    /whatsapp/send-meta-template-test/?to=+9055...&tpl=islem_ozeti_kp_min_v1&lang=tr_TR&type=GENERIC
    """
    if not settings.DEBUG:
        return HttpResponseForbidden("Disabled in production")

    user = request.user
    store = getattr(user, "store", None)
    to = request.GET.get("to") or getattr(settings, "META_DEFAULT_TEST_TO", None)
    name = request.GET.get("tpl") or getattr(settings, "META_DEFAULT_TEMPLATE_NAME", "hello_world")
    lang = request.GET.get("lang") or getattr(settings, "META_DEFAULT_TEMPLATE_LANG", "tr_TR")
    typ = request.GET.get("type", MessageType.GENERIC)

    if not (to and store):
        return HttpResponseBadRequest("to/store missing")

    ok = send_whatsapp_template_guarded(
        store=store, user=user, to=to, template=name, language=lang,
        message_type=typ, header_params=None, body_params=None, button_params=None, validate=False
    )
    return JsonResponse({"sent": ok, "to": to, "template": name, "language": lang, "type": typ})


