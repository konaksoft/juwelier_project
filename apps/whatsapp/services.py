from __future__ import annotations

import logging
import re
from collections import defaultdict
from inspect import signature
from typing import Iterable, Optional

import requests
from django.conf import settings
from django.core.cache import cache

from apps.whatsapp.meta_api import *
from apps.whatsapp.models import *

log = logging.getLogger(__name__)

GRAPH_VER = getattr(settings, "META_GRAPH_VERSION", "v20.0")
PLACEHOLDER_RE = re.compile(r"\{\{\d+\}\}")

from django.core.cache import cache
import threading

NEG_KEY_TTL = 3600  # 1 saat (132001 gibi hatalar için "negatif cache")


def _neg_key(tpl: str, lang: str) -> str:
    return f"wa:neg:{tpl}:{lang}".lower()


def wa_preflight(store: Stores, template: str, language: str) -> tuple[bool, str, str]:
    st = _get_store_settings(store)
    if not st.enabled:
        return False, "disabled", language

    msg_type = guess_message_type(template)
    if not st.is_template_allowed(template, msg_type):
        return False, "not_allowed", language

    if cache.get(_neg_key(template, language)):
        return False, "neg_cache", language

    chosen_lang = _short_lang(language) if "_" in language else language

    try:
        rec = WhatsAppTemplateCatalog.objects.only("languages").get(name=template)
        langs = rec.languages or []
        if langs:
            chosen_lang = _resolve_language(chosen_lang, langs)
    except WhatsAppTemplateCatalog.DoesNotExist:
        pass  # Katalog yoksa sorun değil; yine de deneriz.

    return True, "", chosen_lang


def note_negative_template_lang(template: str, lang: str, ttl: int = NEG_KEY_TTL):
    """132001 gibi hatalarda tekrar denemeleri engellemek için işaretle."""
    cache.set(_neg_key(template, lang), True, ttl)


def _e164(phone: str, default_cc: str = "90") -> Optional[str]:
    if not phone: return None
    s = "".join(ch for ch in str(phone) if ch.isdigit() or ch == "+")
    if not s: return None
    if s.startswith("+"): return s
    if s.startswith("00"): return "+" + s[2:]
    if len(s) == 11 and s.startswith("0"): return f"+{default_cc}{s[1:]}"
    if len(s) == 10: return f"+{default_cc}{s}"
    return f"+{s}"


def _get_waba_id() -> str:
    waba = getattr(settings, "META_WABA_ID", None)
    if waba: return str(waba)
    ck = "wa:auto_waba_id"
    cached = cache.get(ck)
    if cached: return cached
    pnid = str(getattr(settings, "META_PHONE_NUMBER_ID", "")).strip()
    tok = getattr(settings, "META_ACCESS_TOKEN", None)
    if not (pnid and tok): raise RuntimeError("PHONE_NUMBER_ID/ACCESS_TOKEN eksik; WABA_ID çözülemedi.")
    url = f"https://graph.facebook.com/{GRAPH_VER}/{pnid}"
    headers = {"Authorization": f"Bearer {tok}"}
    params = {"fields": "whatsapp_business_account"}
    r = requests.get(url, headers=headers, params=params, timeout=20);
    r.raise_for_status()
    waba = r.json().get("whatsapp_business_account", {}).get("id")
    if not waba: raise RuntimeError("Phone number için WABA bulunamadı.")
    cache.set(ck, waba, 24 * 3600)
    return waba


def _as_list(vals: Optional[Iterable]) -> Optional[list[str]]:
    if vals is None: return None
    out = []
    for v in vals: out.append(" " if v is None else str(v))
    return out


def _short_lang(code: str) -> str:
    return (code or "").split("_")[0].lower() or code


def _resolve_language(preferred: str, available: list[str]) -> str:
    if not available: return preferred
    if preferred in available: return preferred
    p_short = (preferred or "").split("_")[0].lower()
    for lang in available:
        if lang and lang.split("_")[0].lower() == p_short: return lang
    return "en_US" if "en_US" in available else available[0]


def _count_phs(text: Optional[str]) -> int:
    return len(PLACEHOLDER_RE.findall(text or ""))


def _fetch_template_schema(name: str) -> dict:
    """
    {'header': N, 'body': N, 'buttons_total': N, 'buttons_by_idx': [], 'languages': [...]}
    """
    cache_key = f"wa_tpl_schema:{name}"
    cached = cache.get(cache_key)
    if cached: return cached

    tok = getattr(settings, "META_ACCESS_TOKEN", None)
    if not tok: raise RuntimeError("META_ACCESS_TOKEN eksik.")
    waba_id = _get_waba_id()
    url = f"https://graph.facebook.com/{GRAPH_VER}/{waba_id}/message_templates"
    params = {"name": name, "limit": 100}
    headers = {"Authorization": f"Bearer {tok}"}
    resp = requests.get(url, params=params, headers=headers, timeout=20);
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data: raise ValueError(f"Şablon bulunamadı: {name}")

    tpl = data[0]
    comps = tpl.get("components", []) or []
    header_cnt = body_cnt = btns_total = 0
    btns_by_idx = []
    for c in comps:
        t = (c.get("type") or "").upper()
        if t == "HEADER":
            if (c.get("format") or "").upper() == "TEXT" or "text" in c:
                header_cnt = _count_phs(c.get("text"))
        elif t == "BODY":
            body_cnt = _count_phs(c.get("text"))
        elif t == "BUTTONS":
            local_sum = 0
            for b in c.get("buttons", []) or []:
                n = _count_phs(b.get("url")) if (b.get("type") or "").upper() == "URL" else 0
                btns_by_idx.append(n);
                local_sum += n
            btns_total += local_sum
    languages = [row.get("language") for row in data if row.get("language")]
    schema = {"header": header_cnt, "body": body_cnt, "buttons_total": btns_total,
              "buttons_by_idx": btns_by_idx, "languages": languages}
    cache.set(cache_key, schema, 3600)
    return schema


def _get_store_settings(store: Stores) -> StoreWhatsAppSettings:
    obj, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
    return obj


def _log_activity(store, message_log, actor, kind: str, desc: str, meta: dict | None = None):
    try:
        WhatsAppActivity.objects.create(
            store=store, message=message_log, actor=actor, kind=kind, description=desc, meta=(meta or {})
        )
    except Exception:
        log.exception("WA activity log write failed")


# şablon→tip eşleşmesi (opsiyonel override)
TEMPLATE_TYPE_MAP = {}  # örn: {"islem_ozeti_kp_min_v1": MessageType.OP_SUM}


def guess_message_type(tpl: str) -> str:
    return TEMPLATE_TYPE_MAP.get(tpl, MessageType.GENERIC)


class WhatsAppTemplateError(ValueError): pass


# ---- Eski alias: güvenli çağrı (artık tek noktaya yönlendiriyor) ----
def send_wa_template_safe(**kwargs):
    """Geriye dönük uyumluluk için alias; yukarıdaki guard'lı göndericiye yönlendirir."""
    return send_whatsapp_template_guarded(**kwargs)


def _ensure_conversation(store: Stores, msisdn: str, started_by: str = "CUSTOMER") -> WhatsAppConversation:
    conv, created = WhatsAppConversation.objects.get_or_create(
        store=store, phone_number=msisdn,
        defaults={"started_by": started_by, "status": "OPEN", "last_message_at": timezone.now()}
    )
    if created: return conv
    return conv


def _touch_conv(conv: WhatsAppConversation, outbound: bool):
    conv.last_message_at = timezone.now()
    if not outbound:  # inbound ise mağaza için unread++
        conv.unread_count = (conv.unread_count or 0) + 1
    conv.save(update_fields=["last_message_at", "unread_count", "updated_at"])


def _append_chat_out(store, user, customer, to, text, status="DELIVERED", error="", wa_id=""):
    conv = _ensure_conversation(store, _e164(to) or to, started_by="STORE")
    WhatsAppChatMessage.objects.create(
        store=store, conversation=conv, user=user, customer=customer,
        direction="OUT", kind="TEXT", from_number="", to_number=_e164(to) or to,
        text=text, wa_message_id=wa_id, status=status, error=error
    )
    _touch_conv(conv, outbound=True)


# -------------------- Inbound webhook parse + kayıt --------------------
def handle_inbound_webhook(payload: dict, store: Stores):
    """
    Meta webhook JSON’ını parse eder, inbound mesajları ve status eventlerini kaydeder.
    Sadece TEXT ve MEDIA örneklenmiştir.
    """
    try:
        entries = payload.get("entry", []) or []
        for entry in entries:
            changes = entry.get("changes", []) or []
            for ch in changes:
                v = ch.get("value", {}) or {}
                # messages
                for m in v.get("messages", []) or []:
                    _store_inbound_msg(v, m, store)
                # statuses (okundu/teslim vb) -> istersen ileride eşleştir
                # for st in v.get("statuses", []) or []:
                #     pass
    except Exception:
        log.exception("handle_inbound_webhook failed")


def _store_inbound_msg(value: dict, msg: dict, store: Stores):
    # Gönderen alıcı çöz
    contacts = value.get("contacts", []) or []
    from_msisdn = _e164(msg.get("from", "")) or (contacts[0].get("wa_id") if contacts else "")
    to_msisdn = _e164(value.get("metadata", {}).get("display_phone_number", "")) or ""
    conv = _ensure_conversation(store, from_msisdn, started_by="CUSTOMER")

    kind = "TEXT";
    text = msg.get("text", {}).get("body", "")
    media = {}
    if msg.get("type") in ("image", "audio", "video", "document", "sticker"):
        kind = "MEDIA"
        media = {"type": msg.get("type"), "id": msg.get(msg.get("type"), {}).get("id"),
                 "caption": msg.get("caption", "")}
    WhatsAppChatMessage.objects.create(
        store=store, conversation=conv, user=None, customer=None,
        direction="IN", kind=kind, wa_message_id=msg.get("id", ""),
        from_number=from_msisdn, to_number=to_msisdn, text=text, media=media,
        status="DELIVERED"
    )
    _touch_conv(conv, outbound=False)


# -------------------- Şablon liste + kullanım snapshot --------------------
def list_message_templates_grouped() -> list[dict]:
    waba_id = _get_waba_id()
    tok = getattr(settings, "META_ACCESS_TOKEN", None)
    if not tok: return []
    url = f"https://graph.facebook.com/{GRAPH_VER}/{waba_id}/message_templates"
    params = {"limit": 200, "fields": "name,language,status,category"}
    headers = {"Authorization": f"Bearer {tok}"}
    out = defaultdict(lambda: {"name": None, "category": None, "statuses": {}, "languages": []})
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20);
        resp.raise_for_status()
        data = resp.json().get("data", []) or []
        for row in data:
            nm = row.get("name");
            lng = row.get("language") or "";
            st = (row.get("status") or "").upper()
            cat = (row.get("category") or "").upper()
            if not nm: continue
            rec = out[nm];
            rec["name"] = nm;
            rec["category"] = rec["category"] or cat or "TRANSACTIONAL"
            if lng: rec["statuses"][lng] = st;
            if lng and lng not in rec["languages"]: rec["languages"].append(lng)
        res = list(out.values());
        res.sort(key=lambda x: x["name"])
        # Kataloğa senk et (isteğe bağlı)
        for it in res:
            WhatsAppTemplateCatalog.objects.update_or_create(
                name=it["name"],
                defaults={"category": it["category"], "languages": it["languages"], "last_synced": timezone.now()}
            )
        return res
    except Exception:
        log.exception("list_message_templates_grouped failed")
        return []


def _safe_localdate():
    try:
        return timezone.localdate()
    except Exception:
        return date.today()


def get_usage_snapshot(store):
    st = _get_store_settings(store)
    st.save(update_fields=["daily_count", "monthly_count", "daily_reset", "monthly_reset", "updated_at"])

    today = _safe_localdate()
    month_start = today.replace(day=1)

    def pack(cur, lim):
        if lim is None:
            return {"count": cur, "limit": None, "remaining": None, "progress": None}
        remain = max(lim - cur, 0)
        pct = int((cur / lim) * 100) if lim > 0 else 0
        return {"count": cur, "limit": lim, "remaining": remain, "progress": pct}

    return {
        "daily": pack(st.daily_count, st.daily_limit),
        "monthly": pack(st.monthly_count, st.monthly_limit),
        "resets": {"daily": str(today), "monthly": str(month_start)},
        "enabled": st.enabled,
        "allowed_templates": st.allowed_templates or [],
    }


def get_or_create_conversation(store, phone_e164: str) -> WhatsAppConversation:
    conv, created = WhatsAppConversation.objects.get_or_create(
        store=store, phone_number=phone_e164,
        defaults={"started_by": "CUSTOMER", "status": "OPEN", "last_message_at": timezone.now()}
    )
    if not created:
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at", "updated_at"])
    return conv


def send_whatsapp_template_guarded(*,
                                   store: Stores,
                                   user: Users | None = None,
                                   customer=None,
                                   to: str,
                                   template: str | None = None,
                                   name: str | None = None,
                                   template_name: str | None = None,
                                   template_code: str | None = None,
                                   language: str = "tr_TR",
                                   header_params=None,
                                   body_params=None,
                                   button_params=None,
                                   validate: bool = True,
                                   return_reason: bool = False,  # ✅ YENİ: geriye (ok, code) döndürmek için
                                   ) -> bool | tuple[bool, Optional[str]]:
    """
    WhatsApp HSM/template gönderimi (guarded).
    - Varsayılan davranış: bool döner (geriye dönük uyum).
    - return_reason=True verilirse: (ok, code) döner. code bazı olası değerler:
        'DISABLED', 'NOT_ALLOWED', 'DAILY_LIMIT', 'MONTHLY_LIMIT',
        'INVALID_MSISDN', 'HTTP_ERROR', 'ERROR'
    """
    tpl = template or name or template_name or template_code
    if not tpl:
        if return_reason:
            return False, "ERROR"
        raise WhatsAppTemplateError("Template adı boş.")

    st = _get_store_settings(store)
    msg_type = guess_message_type(tpl)

    # Mağaza/policy kontrolü
    if not st.enabled or not st.is_template_allowed(tpl, msg_type):
        reason = "DISABLED" if not st.enabled else "NOT_ALLOWED"
        WhatsAppMessageLog.objects.create(
            store=store, user=user, customer=customer, to=str(to),
            template=tpl, language=language, msg_type=msg_type,
            status=WhatsAppMessageLog.Status.BLOCKED_POLICY, error_message=reason,
        )
        conv = get_or_create_conversation(store, _e164(to) or to)
        WhatsAppChatMessage.objects.create(
            store=store, conversation=conv, user=user, customer=customer,
            direction="OUT", kind="TEMPLATE", from_number="", to_number=_e164(to) or to,
            template_name=tpl,
            template_params={"header": header_params, "body": body_params, "button": button_params},
            status="BLOCKED", error=reason
        )
        _touch_conv(conv, outbound=True)
        if return_reason:
            return False, reason
        return False

    # Kota kontrolü
    ok, quota_reason = st.can_send_now()  # → 'DAILY_LIMIT' / 'MONTHLY_LIMIT' / None
    if not ok:
        log_obj = WhatsAppMessageLog.objects.create(
            store=store, user=user, customer=customer, to=str(to),
            template=tpl, language=language, msg_type=msg_type,
            status=WhatsAppMessageLog.Status.BLOCKED_QUOTA, error_message=quota_reason or "QUOTA",
        )
        conv = get_or_create_conversation(store, _e164(to) or to)
        WhatsAppChatMessage.objects.create(
            store=store, conversation=conv, user=user, customer=customer,
            direction="OUT", kind="TEMPLATE", from_number="", to_number=_e164(to) or to,
            template_name=tpl,
            template_params={"header": header_params, "body": body_params, "button": button_params},
            status="BLOCKED", error=quota_reason or "QUOTA"
        )
        _touch_conv(conv, outbound=True)
        _log_activity(store, log_obj, user, "BLOCK", "Quota")
        if return_reason:
            # kota durumunda açıkça günlük/aylık döner
            return False, (quota_reason or "QUOTA")
        return False

    # Numara normalize
    to_norm = _e164(to)
    if not to_norm:
        WhatsAppMessageLog.objects.create(
            store=store, user=user, customer=customer, to=str(to),
            template=tpl, language=language, msg_type=msg_type,
            status=WhatsAppMessageLog.Status.FAILED, error_message="invalid_msisdn",
        )
        if return_reason:
            return False, "INVALID_MSISDN"
        return False

    # --- DİL ÇÖZÜMLEME (HER ZAMAN) ---
    chosen_lang = language
    schema = None
    try:
        schema = _fetch_template_schema(tpl)
        chosen_lang = _resolve_language(language, schema.get("languages", []))
    except Exception:
        if "_" in language:
            chosen_lang = _short_lang(language)

    # --- Placeholder doğrulama (opsiyonel) ---
    if validate and schema:
        hp = _as_list(header_params) or []
        bp = _as_list(body_params) or []
        btnp = _as_list(button_params) or []
        if schema.get("header", 0) != len(hp):
            if return_reason: return False, "ERROR"
            raise WhatsAppTemplateError("HEADER placeholder sayısı uyuşmuyor.")
        if schema.get("body", 0) != len(bp):
            if return_reason: return False, "ERROR"
            raise WhatsAppTemplateError("BODY placeholder sayısı uyuşmuyor.")
        btn_total = sum(schema.get("buttons_by_idx") or [])
        if btn_total and btn_total != len(btnp):
            if return_reason: return False, "ERROR"
            raise WhatsAppTemplateError("BUTTON placeholder sayısı uyuşmuyor.")

    # --- GÖNDER ---
    hp = _as_list(header_params)
    bp = _as_list(body_params)
    btnp = _as_list(button_params)
    try:
        resp = send_meta_template_api(
            to=to_norm,
            template=tpl,
            lang=chosen_lang,
            header_params=hp,
            body_params=bp,
            button_params=btnp,
        )
        msg_id = (resp.get("messages") or [{}])[0].get("id", "")

        try:
            st_refresh = _get_store_settings(store)
            st_refresh.consume(1)

            conv = get_or_create_conversation(store, to_norm)

            WhatsAppChatMessage.objects.create(
                store=store, conversation=conv, user=user, customer=customer,
                direction="OUT", kind="TEMPLATE", wa_message_id=msg_id,
                from_number="", to_number=to_norm,
                template_name=tpl,
                template_params={"header": hp, "body": bp, "button": btnp},
                status="DELIVERED"
            )

            msglog = WhatsAppMessageLog.objects.create(
                store=store, user=user, customer=customer, to=to_norm,
                template=tpl, language=chosen_lang, msg_type=msg_type,
                header_params=hp, body_params=bp, button_params=btnp,
                response_id=msg_id, status=WhatsAppMessageLog.Status.SENT,
            )
            _touch_conv(conv, outbound=True)
            _log_activity(store, msglog, user, "SEND", "Template sent")

        except Exception as db_err:
            log.error(f"WhatsApp DB Error (Message SENT but not saved): {str(db_err)}")

        if return_reason:
            return True, None
        return True

    except requests.HTTPError as http_err:
        # 132001 => negatif cache
        try:
            ej = http_err.response.json()
            code = ej.get("error", {}).get("code")
            details = (ej.get("error", {}).get("error_data") or {}).get("details", "")
            if code == 132001 or "does not exist in" in details:
                note_negative_template_lang(tpl, chosen_lang)
        except Exception:
            pass

        WhatsAppMessageLog.objects.create(
            store=store, user=user, customer=customer, to=to_norm or str(to),
            template=tpl, language=chosen_lang, msg_type=msg_type,
            status=WhatsAppMessageLog.Status.FAILED, error_message=str(http_err),
        )
        if return_reason:
            return False, "HTTP_ERROR"
        return False

    except Exception as e:
        WhatsAppMessageLog.objects.create(
            store=store, user=user, customer=customer, to=to_norm or str(to),
            template=tpl, language=chosen_lang, msg_type=msg_type,
            status=WhatsAppMessageLog.Status.FAILED, error_message=str(e),
        )
        if return_reason:
            return False, "ERROR"
        return False


def send_whatsapp_text_guarded(
        *, store: Stores, user: Users | None, customer, to: str, text: str, return_reason: bool = False
) -> bool | tuple[bool, Optional[str]]:
    """
    Serbest metin gönderimi (support chat).
    - Varsayılan: bool döner.
    - return_reason=True → (ok, code)
      Olası code: 'DISABLED', 'DAILY_LIMIT', 'MONTHLY_LIMIT', 'CONFIG', 'HTTP_ERROR', 'ERROR'
    """
    st = _get_store_settings(store)
    if not st.enabled:
        _append_chat_out(store, user, customer, to, text, status="BLOCKED", error="disabled")
        if return_reason:
            return False, "DISABLED"
        return False

    ok, quota_reason = st.can_send_now()
    if not ok:
        _append_chat_out(store, user, customer, to, text, status="BLOCKED", error=quota_reason or "quota")
        if return_reason:
            return False, (quota_reason or "QUOTA")
        return False

    tok = getattr(settings, "META_ACCESS_TOKEN", None)
    pnid = getattr(settings, "META_PHONE_NUMBER_ID", None)
    if not (tok and pnid):
        _append_chat_out(store, user, customer, to, text, status="FAILED", error="config")
        if return_reason:
            return False, "CONFIG"
        return False

    url = f"https://graph.facebook.com/{GRAPH_VER}/{pnid}/messages"
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": _e164(to) or to, "type": "text", "text": {"body": text}}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code >= 400:
            log.error("send_text failed: %s", r.text)
            r.raise_for_status()
        resp = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        st.consume(1)
        _append_chat_out(store, user, customer, to, text, wa_id=str(resp.get("messages", [{}])[0].get("id", "")))
        if return_reason:
            return True, None
        return True
    except requests.HTTPError:
        _append_chat_out(store, user, customer, to, text, status="FAILED", error="http")
        if return_reason:
            return False, "HTTP_ERROR"
        return False
    except Exception as e:
        _append_chat_out(store, user, customer, to, text, status="FAILED", error=str(e))
        if return_reason:
            return False, "ERROR"
        return False
