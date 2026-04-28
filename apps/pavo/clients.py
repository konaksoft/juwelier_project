import os
import hmac
import hashlib
import base64
import json
import requests
from decimal import Decimal
from django.conf import settings


class PavoClient:
    """
    Pavo bulut ödeme API istemcisi.
    """
    def __init__(self):
        self.base_url = getattr(settings, 'PAVO_BASE_URL', 'https://api.pavo.com.tr')
        self.merchant_id = getattr(settings, 'PAVO_MERCHANT_ID', '')
        self.api_key = getattr(settings, 'PAVO_API_KEY', '')
        self.api_secret = getattr(settings, 'PAVO_API_SECRET', '')
        self.success_url = getattr(settings, 'PAVO_SUCCESS_URL', '')
        self.fail_url = getattr(settings, 'PAVO_FAIL_URL', '')
        self.session = requests.Session()

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'X-Api-Key': self.api_key,
            'X-Api-Secret': self.api_secret,
        }

    def create_payment(self, *, external_id: str, amount: Decimal, currency: str, description: str | None = None):
        payload = {
            'merchant_id': self.merchant_id,
            'external_id': external_id,
            'amount': str(amount),
            'currency': currency,
            'description': description or external_id,
            'return_url_success': self.success_url,
            'return_url_fail': self.fail_url,
        }
        url = f'{self.base_url}/payments'
        r = self.session.post(url, headers=self._headers(), data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        data = r.json()
        return {
            'pavo_id': data.get('id') or data.get('payment_id'),
            'payment_url': data.get('payment_url') or data.get('redirect_url'),
            'raw': data,
        }

    def payment_status(self, pavo_id: str):
        url = f'{self.base_url}/payments/{pavo_id}'
        r = self.session.get(url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def verify_webhook(signature_header: str | None, body_bytes: bytes) -> bool:
        """
        Pavo webhook imza doğrulama (HMAC-SHA256 + Base64).
        """
        secret = os.getenv('PAVO_WEBHOOK_SECRET', getattr(settings, 'PAVO_WEBHOOK_SECRET', ''))
        if not secret or not signature_header:
            return False
        expected = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).digest()
        try:
            provided = base64.b64decode(signature_header)
        except Exception:
            return False
        return hmac.compare_digest(expected, provided)