# ============================================================================
# DOSYA: apps/invoices/esurec_client.py
# KONUM: Kuyum Plus projesi içinde (test.kuyumplus.com)
#
# DÜZELTMEv3 (2026-04-18):
#   - E-Gider Pusulası metotları artık seller_vkn parametresi alır.
#     e-Süreç tarafında tenant izolasyonu zorunlu hale geldi; bu parametre
#     gönderilmezse 400 dönebilir.
#   - send_expense_voucher_to_gib / check_expense_voucher_status /
#     cancel_expense_voucher / get_expense_voucher_pdf metotları query
#     string üzerinden seller_vkn iletiyor.
#
# DÜZELTMEv2:
#   - raise_for_status() KALDIRILDI.
#   - Her HTTP durum kodu (400, 401, 403, 404, 429, 500+) ayrı yakalanır.
#   - Yanıt JSON'undaki error_msg her zaman çıkarılır.
#   - Tüm hata yanıtlarına 'retryable' ve 'http_status' eklendi:
#       retryable=True  → Celery task tekrar deneyebilir (bağlantı, timeout, 429, 500+)
#       retryable=False → Kalıcı hata, retry anlamsız (400, 401, 403, 404)
# ============================================================================

import hashlib
import hmac
import logging
import time

import requests
from django.conf import settings

log = logging.getLogger(__name__)


class ESurecClient:

    def __init__(self):
        self.base_url = getattr(settings, 'ESUREC_BASE_URL', '').rstrip('/')
        self.api_key = getattr(settings, 'ESUREC_API_KEY', '')
        self.api_secret = getattr(settings, 'ESUREC_API_SECRET', '')
        self.timeout = getattr(settings, 'ESUREC_TIMEOUT', 30)
        self.session = requests.Session()

        if not self.base_url:
            raise ValueError(
                "e-Süreç bağlantısı yapılandırılmamış. "
                "settings.py veya .env dosyasında ESUREC_BASE_URL tanımlayın."
            )
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "e-Süreç API kimlik bilgileri eksik. "
                ".env dosyasında ESUREC_API_KEY ve ESUREC_API_SECRET tanımlayın."
            )

    def _auth_headers(self) -> dict:
        ts = str(int(time.time()))
        sig = hmac.HMAC(
            self.api_secret.encode('utf-8'),
            f"{self.api_key}:{ts}".encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return {
            'Content-Type': 'application/json',
            'X-API-Key': self.api_key,
            'X-Timestamp': ts,
            'X-Signature': sig,
        }

    @staticmethod
    def _extract_json(resp) -> dict | None:
        try:
            return resp.json()
        except Exception:
            return None

    def _request(self, method: str, endpoint: str, data: dict = None,
                 extra_headers: dict = None, params: dict = None) -> dict:
        url = f"{self.base_url}{endpoint}"
        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)

        try:
            if method.upper() == 'GET':
                resp = self.session.get(
                    url, headers=headers,
                    params=params if params is not None else data,
                    timeout=self.timeout,
                )
            else:
                resp = self.session.post(
                    url, headers=headers, json=data, params=params,
                    timeout=self.timeout,
                )

            body = self._extract_json(resp)
            sc = resp.status_code

            # --- 2xx: Başarılı ---
            if 200 <= sc < 300:
                if body is not None:
                    return body
                return {
                    'result': False,
                    'error_msg': f'e-Süreç yanıtı JSON olarak okunamadı (HTTP {sc}).',
                    'http_status': sc,
                    'retryable': False,
                }

            # --- 400 Bad Request (şema/doğrulama hatası) ---
            if sc == 400:
                b = body or {}
                return {
                    'result': False,
                    'error_msg': b.get('error_msg') or b.get('message', '')
                                 or f'e-Süreç geçersiz istek (400): {endpoint}',
                    'http_status': 400,
                    'retryable': False,
                }

            # --- 401 Unauthorized (API Key / imza hatası) ---
            if sc == 401:
                return {
                    'result': False,
                    'error_msg': (body or {}).get('error_msg', '')
                                 or (
                                     'e-Süreç kimlik doğrulama hatası (401). '
                                     'API Key veya Secret yanlış olabilir. '
                                     'Kuyum Plus .env → ESUREC_API_KEY / ESUREC_API_SECRET değerlerini '
                                     'e-Süreç .env → EXTERNAL_API_CLIENTS ile karşılaştırın.'
                                 ),
                    'http_status': 401,
                    'retryable': False,
                }

            # --- 403 Forbidden ---
            if sc == 403:
                return {
                    'result': False,
                    'error_msg': (body or {}).get('error_msg', '')
                                 or 'e-Süreç erişim engeli (403). API anahtarınız bu işlem için yetkili değil.',
                    'http_status': 403,
                    'retryable': False,
                }

            # --- 404 Not Found (Dealer/Fatura bulunamadı) ---
            if sc == 404:
                return {
                    'result': False,
                    'error_msg': (body or {}).get('error_msg', '')
                                 or f'e-Süreç endpoint bulunamadı (404): {endpoint}',
                    'http_status': 404,
                    'retryable': False,
                }

            # --- 429 Too Many Requests (Rate limit) ---
            if sc == 429:
                return {
                    'result': False,
                    'error_msg': (body or {}).get('error_msg', '')
                                 or 'e-Süreç çok fazla istek (429). Lütfen bekleyin.',
                    'http_status': 429,
                    'retryable': True,
                }

            # --- 5xx Sunucu Hatası ---
            if sc >= 500:
                b = body or {}
                # KP-11: resp.text loglanmaz (token/credential sızıntı riski)
                if body is None:
                    log.error(
                        f"e-Süreç 5xx HTML yanıtı ({sc}) [{endpoint}] "
                        f"Content-Type={resp.headers.get('Content-Type', '?')}, "
                        f"Length={len(resp.text or '')}"
                    )
                error_detail = (
                    b.get('error_msg') or b.get('message', '')
                    or f'e-Süreç sunucu hatası ({sc}). Sunucu durumunu kontrol edin.'
                )
                return {
                    'result': False,
                    'error_msg': error_detail,
                    'http_status': sc,
                    'retryable': True,
                }

            # --- Bilinmeyen HTTP kodu ---
            return {
                'result': False,
                'error_msg': (body or {}).get('error_msg', '')
                             or f'e-Süreç beklenmeyen HTTP kodu ({sc})',
                'http_status': sc,
                'retryable': False,
            }

        except requests.exceptions.ConnectionError as e:
            # KP-11: str(e) yerine type(e).__name__ — bağlantı hata detayında
            # sunucu URL'leri, portlar veya internal IP'ler sızabilir
            log.error(f"e-Süreç bağlantı hatası: {type(e).__name__} — {endpoint}")
            return {
                'result': False,
                'error_msg': (
                    f'e-Süreç sunucusuna bağlanılamadı. '
                    f'Sunucu çalışıyor mu kontrol edin.'
                ),
                'retryable': True,
            }

        except requests.exceptions.Timeout:
            return {
                'result': False,
                'error_msg': f'e-Süreç sunucusu {self.timeout} saniye içinde yanıt vermedi (timeout).',
                'retryable': True,
            }

        except Exception as e:
            # KP-11: str(e) yerine type(e).__name__ — exception mesajında
            # API anahtarları veya yanıt içeriği sızabilir
            log.exception(f"e-Süreç beklenmeyen hata: {type(e).__name__} — {endpoint}")
            return {
                'result': False,
                'error_msg': f'e-Süreç ile iletişimde beklenmeyen hata ({type(e).__name__}). Lütfen tekrar deneyin.',
                'retryable': False,
            }

    # ----- API METOTLARI -----

    def send_invoice(self, invoice_payload: dict) -> dict:
        """Faturayı e-Süreç'e taslak olarak gönderir."""
        return self._request('POST', '/api/v1/external/invoice/send/', invoice_payload)

    def send_to_gib(self, esurec_invoice_id: str) -> dict:
        """e-Süreç'teki taslak faturayı GİB/entegratöre gönderir."""
        return self._request(
            'POST',
            f'/api/v1/external/invoice/send-to-gib/{esurec_invoice_id}/',
            {}
        )

    def check_status(self, esurec_invoice_id: str) -> dict:
        """Fatura durumunu sorgular."""
        return self._request(
            'GET',
            f'/api/v1/external/invoice/status/{esurec_invoice_id}/'
        )

    def get_pdf(self, esurec_invoice_id: str) -> dict:
        """PDF alır."""
        return self._request(
            'GET',
            f'/api/v1/external/invoice/pdf/{esurec_invoice_id}/'
        )

    def get_xml(self, esurec_invoice_id: str) -> dict:
        """XML alır."""
        return self._request(
            'GET',
            f'/api/v1/external/invoice/xml/{esurec_invoice_id}/'
        )

    def cancel_invoice(self, esurec_invoice_id: str, reason: str = '') -> dict:
        """İptal eder."""
        return self._request(
            'POST',
            f'/api/v1/external/invoice/cancel/{esurec_invoice_id}/',
            {'reason': reason}
        )

    def check_gib_user(self, vkn: str, seller_vkn: str = None) -> dict:
        """GİB mükellefiyet sorgular. seller_vkn opsiyonel (e-Süreç dealer çözümlemesi için)."""
        params = {'seller_vkn': seller_vkn} if seller_vkn else None
        return self._request('GET', f'/api/v1/external/gib/check/{vkn}/', params)

    # ----- KP-09: E-İRSALİYE METOTLARI -----

    def send_despatch(self, despatch_payload: dict) -> dict:
        """e-İrsaliyeyi e-Süreç'e taslak olarak gönderir."""
        return self._request('POST', '/api/v1/external/despatch/send/', despatch_payload)

    def get_despatch_status(self, esurec_despatch_id: str) -> dict:
        """e-İrsaliye durumunu sorgular."""
        return self._request(
            'GET',
            f'/api/v1/external/despatch/status/{esurec_despatch_id}/'
        )

    def get_inbox_despatches(self, params: dict = None) -> dict:
        """Gelen irsaliyeleri listeler."""
        return self._request('GET', '/api/v1/external/despatch/inbox/', params)

    def send_despatch_to_gib(self, esurec_despatch_id: str) -> dict:
        """e-Süreç'teki taslak irsaliyeyi GİB'e gönderir."""
        return self._request(
            'POST',
            f'/api/v1/external/despatch/send-to-gib/{esurec_despatch_id}/',
            {}
        )

    # ----- E-GİDER PUSULASI METOTLARI (v3: seller_vkn eklendi) -----

    def send_expense_voucher(self, voucher_payload: dict) -> dict:
        """
        E-Gider Pusulasını e-Süreç'e taslak olarak gönderir.
        voucher_payload içinde supplier.tax_number olmalı (düzenleyen VKN).
        """
        return self._request('POST', '/api/v1/external/expense-voucher/send/', voucher_payload)

    def send_expense_voucher_to_gib(self, esurec_voucher_id: str, seller_vkn: str) -> dict:
        """
        e-Süreç'teki taslak e-Gider Pusulasını entegratöre gönderir.

        Args:
            esurec_voucher_id: e-Süreç'teki belge UUID'si
            seller_vkn: Tenant izolasyonu için düzenleyen firmanın VKN/TCKN'si (zorunlu)
        """
        return self._request(
            'POST',
            f'/api/v1/external/expense-voucher/send-to-gib/{esurec_voucher_id}/',
            {'seller_vkn': (seller_vkn or '').strip()}
        )

    def check_expense_voucher_status(self, esurec_voucher_id: str, seller_vkn: str) -> dict:
        """
        E-Gider Pusulası durumunu sorgular.

        Args:
            esurec_voucher_id: e-Süreç'teki belge UUID'si
            seller_vkn: Tenant izolasyonu için düzenleyen firmanın VKN/TCKN'si (zorunlu)
        """
        return self._request(
            'GET',
            f'/api/v1/external/expense-voucher/status/{esurec_voucher_id}/',
            params={'seller_vkn': (seller_vkn or '').strip()},
        )

    def cancel_expense_voucher(self, esurec_voucher_id: str, seller_vkn: str,
                                reason: str = '') -> dict:
        """
        E-Gider Pusulasını iptal eder.

        Args:
            esurec_voucher_id: e-Süreç'teki belge UUID'si
            seller_vkn: Tenant izolasyonu için düzenleyen firmanın VKN/TCKN'si (zorunlu)
            reason: İptal gerekçesi
        """
        return self._request(
            'POST',
            f'/api/v1/external/expense-voucher/cancel/{esurec_voucher_id}/',
            {'seller_vkn': (seller_vkn or '').strip(), 'reason': reason}
        )

    def get_expense_voucher_pdf(self, esurec_voucher_id: str, seller_vkn: str) -> dict:
        """
        E-Gider Pusulası PDF alır.

        Args:
            esurec_voucher_id: e-Süreç'teki belge UUID'si
            seller_vkn: Tenant izolasyonu için düzenleyen firmanın VKN/TCKN'si (zorunlu)
        """
        return self._request(
            'GET',
            f'/api/v1/external/expense-voucher/pdf/{esurec_voucher_id}/',
            params={'seller_vkn': (seller_vkn or '').strip()},
        )
