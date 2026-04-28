# apps/whatsapp/meta_api.py
import logging
import requests
from django.conf import settings

log = logging.getLogger(__name__)

GRAPH_VER = getattr(settings, "META_GRAPH_VERSION", "v20.0")  # v20/v21 fark etmez, doğru ID daha kritik
PHONE_NUMBER_ID = getattr(settings, "META_PHONE_NUMBER_ID", None)  # <-- ZORUNLU
ACCESS_TOKEN    = getattr(settings, "META_ACCESS_TOKEN", None)     # <-- ZORUNLU

def send_meta_template_api(
    to: str,
    template: str,
    *,
    lang: str = "tr_TR",
    header_params=None,
    body_params=None,
    button_params=None,
    components=None
):
    """
    ŞABLON MESAJ GÖNDERİMİ
    - Her zaman PHONE_NUMBER_ID üzerinden /messages endpoint’i
    - components parametresi verilirse onu kullanır; yoksa header/body/button’dan kendisi üretir
    """
    if not (PHONE_NUMBER_ID and ACCESS_TOKEN):
        raise RuntimeError("META_PHONE_NUMBER_ID veya META_ACCESS_TOKEN eksik.")

    url = f"https://graph.facebook.com/{GRAPH_VER}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # components yoksa header/body/button’dan oluştur
    if components is None:
        comps = []
        if header_params:
            comps.append({
                "type": "header",
                "parameters": [{"type": "text", "text": str(v) if v is not None else " "} for v in header_params],
            })
        if body_params:
            comps.append({
                "type": "body",
                "parameters": [{"type": "text", "text": str(v) if v is not None else " "} for v in body_params],
            })
        if button_params:
            # Birden fazla URL butonu varsa her biri için ayrı component
            for idx, val in enumerate(button_params):
                if val is None:
                    continue
                comps.append({
                    "type": "button",
                    "sub_type": "url",
                    "index": str(idx),
                    "parameters": [{"type": "text", "text": str(val)}],
                })
    else:
        comps = components

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": lang},
        }
    }
    if comps:
        payload["template"]["components"] = comps

    r = requests.post(url, json=payload, headers=headers, timeout=(3, 6))

    if r.status_code >= 400:
        # Hata detayını log’a bas ki ne olduğunu net görelim
        log.error("Meta send failed (%s): %s | URL=%s", r.status_code, r.text, url)
        r.raise_for_status()

    return r.json()


def send_meta_text_api(to: str, text: str, preview_url: bool=False):
    """
    24s oturum penceresi içinde serbest metin gönderimi.
    """
    if not (PHONE_NUMBER_ID and ACCESS_TOKEN):
        raise RuntimeError("META_PHONE_NUMBER_ID veya META_ACCESS_TOKEN eksik.")

    url = f"https://graph.facebook.com/{GRAPH_VER}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": str(text or ""), "preview_url": bool(preview_url)},
    }
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code >= 400:
        log.error("Meta text send failed (%s): %s | URL=%s", r.status_code, r.text, url)
        r.raise_for_status()
    return r.json()