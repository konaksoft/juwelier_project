# apps/pavo/views_settings.py
from __future__ import annotations

import json, socket, subprocess
from datetime import datetime

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.stores.models import Stores
from .local_client import PavoLocalClient, _pick_source_ip
from .models import *


def _port_open(host: str, port: int, source_ip: str | None = None, timeout: float = 4.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if source_ip:
            s.bind((source_ip, 0))
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


@staff_member_required
@require_GET
def pavo_debug_connect(request):
    host = request.GET.get("host", "192.168.1.111")
    ports = [4568, 4567]
    out = {"host": host, "tests": []}

    for p in ports:
        try:
            with socket.create_connection((host, p), timeout=3.0):
                ok = True;
                err = None
        except OSError as e:
            ok = False;
            err = str(e)
        out["tests"].append({"type": "socket", "port": p, "ok": ok, "error": err})

    payload = json.dumps({
        "TransactionHandle": {
            "SerialNumber": request.GET.get("sn", "N860W679412"),
            "Fingerprint": request.GET.get("fp", "test"),
            "TransactionSequence": 1,
            "TransactionDate": "2025-01-29T11:20:35"
        }
    })
    for p in ports:
        scheme = "http" if p == 4568 else "https"
        url = f"{scheme}://{host}:{p}/Pairing"
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-m", "5", "-H", "Content-Type: application/json", "-d", payload, url],
                capture_output=True, text=True
            )
            out["tests"].append({
                "type": "curl", "url": url,
                "returncode": proc.returncode, "stdout": proc.stdout[:500], "stderr": proc.stderr[:500],
            })
        except Exception as e:
            out["tests"].append({"type": "curl", "url": url, "error": str(e)})

    return JsonResponse(out)


# apps/pavo/views_settings.py

def _user_store(request: HttpRequest) -> Stores:
    st = getattr(request.user, "store", None)
    if st:
        return st
    sid = getattr(request.user, "store_id", None) or request.session.get("active_store_id")
    if sid:
        try:
            return Stores.objects.get(id=sid)
        except Stores.DoesNotExist:
            pass
    try:
        return Stores.objects.get()
    except Stores.DoesNotExist:
        raise ValueError("Kullanıcıya bağlı mağaza bulunamadı.")


@login_required(login_url="login")
@require_http_methods(["POST"])
def pavo_terminal_pairing_test_view(request: HttpRequest) -> JsonResponse:
    try:
        store = _user_store(request)
    except ValueError as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=400)

    term = (
        PavoTerminal.objects
        .filter(store=store, is_active=True)
        .order_by("-updated_at")
        .first()
    )

    ip = (request.POST.get("ip") or (term.ip if term else "")).strip()
    secure_from_form = ("secure" in request.POST) and (request.POST.get("secure") in ("on", "1", "true", "True"))
    secure_saved = (term.secure if term else True)
    secure = secure_from_form if "secure" in request.POST else secure_saved

    port_raw = request.POST.get("port") or (str(term.port) if term and term.port else "")
    port = int(port_raw) if port_raw else None

    serial_number = (request.POST.get("serial_number") or (term.serial_number if term else "")).strip()
    fingerprint = (request.POST.get("fingerprint") or (term.fingerprint if term else "")).strip()

    if not ip or not serial_number or not fingerprint:
        return JsonResponse({"ok": False, "message": "IP, Seri No ve Fingerprint zorunludur."}, status=400)

    def _is_private_ipv4(s: str) -> bool:
        try:
            import ipaddress, socket as _s
            host = s.strip()
            if not host:
                return True
            try:
                ip_obj = ipaddress.ip_address(host)
            except ValueError:
                try:
                    ip_obj = ipaddress.ip_address(_s.gethostbyname(host))
                except Exception:
                    return True
            return ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
        except Exception:
            try:
                a, b, c, d = [int(x) for x in s.split(".")]
                if a == 10: return True
                if a == 192 and b == 168: return True
                if a == 172 and 16 <= b <= 31: return True
                if a == 127: return True
                if a == 169 and b == 254: return True
                return False
            except Exception:
                return True

    source_ip = _pick_source_ip(ip)
    if _is_private_ipv4(ip) and not source_ip:
        return JsonResponse({
            "ok": False,
            "reason": "unreachable_from_cloud",
            "message": "Bu IP yerel veya loopback. Sunucudan erişilemez. Lütfen Local Agent (127.0.0.1:9099) veya VPN kullanın.",
            "ip": ip
        }, status=400)

    try_order = []
    chosen = (secure, port or (4567 if secure else 4568))
    alt = (not secure, 4567 if not secure else 4568)
    try_order.append(chosen)
    if alt != chosen:
        try_order.append(alt)

    def _payload(seq: int = 1):
        from datetime import datetime as _dt
        return {
            "TransactionHandle": {
                "SerialNumber": serial_number,
                "Fingerprint": fingerprint,
                "TransactionSequence": seq,
                "TransactionDate": _dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
        }

    def _http_post_raw(host: str, prt: int, body_json: dict, timeout: float = 3.0) -> dict:
        import json as _json, socket as _s
        body = _json.dumps(body_json).encode("utf-8")
        req = (
                  f"POST /Pairing HTTP/1.1\r\n"
                  f"Host: {host}:{prt}\r\n"
                  f"Content-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\n"
                  f"Connection: close\r\n\r\n"
              ).encode("utf-8") + body

        s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        if source_ip:
            try:
                s.bind((source_ip, 0))
            except OSError:
                pass
        s.settimeout(timeout)
        s.connect((host, prt))
        s.sendall(req)
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
        s.close()
        raw = b"".join(chunks).decode("utf-8", "replace")
        idx = raw.find("\r\n\r\n")
        body_txt = raw[idx + 4:] if idx != -1 else raw
        try:
            return {"ok": True, "raw": raw[:2000], "json": json.loads(body_txt)}
        except Exception as e:
            return {"ok": False, "raw": raw[:2000], "error": f"JSON parse failed: {e}"}

    attempts, last_err = [], None

    try:
        for use_secure, use_port in try_order:
            scheme = "https" if use_secure else "http"
            endpoint = f"{scheme}://{ip}:{use_port}/Pairing"
            port_ok = _port_open(ip, use_port, source_ip=source_ip, timeout=1.2)

            try:
                client = PavoLocalClient(
                    ip=ip, secure=use_secure,
                    serial_number=serial_number, fingerprint=fingerprint,
                    port=use_port, timeout=3.0, source_ip=source_ip
                )
                resp = client.pairing()
                if term:
                    term.last_paired_at = timezone.now()
                    term.save(update_fields=["last_paired_at", "updated_at"])
                attempts.append(
                    {"method": "requests_bound", "endpoint": endpoint, "precheck_port_open": port_ok, "ok": True})
                return JsonResponse({"ok": True, "attempts": attempts, "response": resp, "source_ip": source_ip})
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                attempts.append(
                    {"method": "requests_bound", "endpoint": endpoint, "precheck_port_open": port_ok, "ok": False,
                     "error": last_err, "source_ip": source_ip})

            try:
                import requests as _r
                r2 = _r.post(
                    endpoint,
                    json=_payload(2),
                    timeout=3.0,
                    verify=False,
                    allow_redirects=False,
                    proxies={"http": None, "https": None}
                )
                r2.raise_for_status()
                attempts.append({"method": "requests_plain", "endpoint": endpoint, "ok": True})
                return JsonResponse({"ok": True, "attempts": attempts, "response": r2.json(), "source_ip": source_ip})
            except Exception as e:
                attempts.append({"method": "requests_plain", "endpoint": endpoint, "ok": False,
                                 "error": f"{type(e).__name__}: {e}"})

            if not use_secure:
                try:
                    raw_out = _http_post_raw(ip, use_port, _payload(3), timeout=3.0)
                    attempts.append({"method": "raw_socket_http", "endpoint": endpoint, "ok": raw_out.get("ok", False)})
                    if raw_out.get("ok"):
                        if term:
                            term.last_paired_at = timezone.now()
                            term.save(update_fields=["last_paired_at", "updated_at"])
                        return JsonResponse({"ok": True, "attempts": attempts, "response": raw_out.get("json", raw_out),
                                             "source_ip": source_ip})
                except Exception as e:
                    attempts.append({"method": "raw_socket_http", "endpoint": endpoint, "ok": False,
                                     "error": f"{type(e).__name__}: {e}"})
    except Exception as fatal:
        import traceback
        return JsonResponse({
            "ok": False,
            "message": "Beklenmeyen hata",
            "error": str(fatal),
            "trace": traceback.format_exc()[-2000:],
        }, status=500)

    def _curl_try(url: str, payload: dict):
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-m", "6", "-H", "Content-Type: application/json", "-d", json.dumps(payload), url],
                capture_output=True, text=True
            )
            return {"url": url, "returncode": proc.returncode, "stdout": proc.stdout[:1200],
                    "stderr": proc.stderr[:600]}
        except Exception as e:
            return {"url": url, "error": str(e)}

    curl_http = _curl_try(f"http://{ip}:{4568}/Pairing", _payload(4))
    curl_https = _curl_try(f"https://{ip}:{4567}/Pairing", _payload(5))

    return JsonResponse({
        "ok": False,
        "attempts": attempts,
        "curl": {"http4568": curl_http, "https4567": curl_https},
        "source_ip": source_ip,
        "message": last_err or "Bağlantı kurulamadı",
        "hint": "Özel/loopback IP’lere buluttan erişim yoksa Local Agent veya VPN kullanın."
    }, status=502)


@login_required(login_url="login")
@require_http_methods(["GET", "POST"])
def pavo_settings_view(request: HttpRequest) -> HttpResponse:
    store = _user_store(request)
    term = (PavoTerminal.objects
            .filter(store=store, is_active=True)
            .order_by("-updated_at")
            .first())

    if request.method == "POST":
        title = (request.POST.get("title") or "Terminal").strip()
        ip = (request.POST.get("ip") or "").strip()
        secure = bool(request.POST.get("secure"))
        port_raw = (request.POST.get("port") or "").strip()
        port = int(port_raw) if port_raw else None
        serial_number = (request.POST.get("serial_number") or "").strip()
        fingerprint = (request.POST.get("fingerprint") or "").strip()

        if not ip or not serial_number or not fingerprint:
            messages.error(request, "IP, Seri No ve Fingerprint zorunlu alanlardır.")
        else:
            if term is None:
                term = PavoTerminal(store=store)
            term.title = title or term.title
            term.ip = ip
            term.secure = secure
            term.port = port
            term.serial_number = serial_number
            term.fingerprint = fingerprint
            term.is_active = True
            term.save()
            messages.success(request, "Pavo terminal ayarları kaydedildi.")
            return redirect("pavo:settings")

    return render(request, "management/pavo/settings.html", {
        "title": "Pavo Ayarları",
        "term": term,
    })
