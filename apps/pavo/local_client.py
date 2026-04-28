from __future__ import annotations
import socket
from datetime import datetime
from decimal import Decimal
from django.core.cache import cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

DEFAULT_SECURE_PORT = 4567
DEFAULT_NONSECURE_PORT = 4568


def build_jewellery_sale_payload_from_invoice(invoice) -> dict:
    items = []
    for it in invoice.items.all():
        items.append({
            "Name": it.product_name or "Ürün",
            "Barcode": it.barcode or "",
            "Karat": str(getattr(it, "karat", "")) if hasattr(it, "karat") else "",
            "Unit": it.unit,
            "Quantity": float(it.quantity or 0),
            "UnitPrice": float(it.unit_price or Decimal("0")),
            "DiscountRate": float(it.discount_rate or Decimal("0")),
            "VatRate": float(it.vat_rate or Decimal("0")),
            "LineTotal": float(it.total_incl_vat or Decimal("0")),
        })
    return {
        "SaleInfo": {
            "Currency": invoice.currency,
            "Subtotal": float(invoice.subtotal or Decimal("0")),
            "DiscountTotal": float(invoice.discount_total or Decimal("0")),
            "TaxTotal": float(invoice.tax_total or Decimal("0")),
            "GrandTotal": float(invoice.grand_total or Decimal("0")),
            "InvoiceNo": invoice.invoice_no,
        },
        "Customer": {
            "CustomerId": str(invoice.customer_id or ""),
            "Title": getattr(invoice.customer, "title", "") if invoice.customer_id else "",
            "TaxNo": getattr(invoice.customer, "taxno", "") if invoice.customer_id else "",
            "Tckn": getattr(invoice.customer, "tckn", "") if invoice.customer_id else "",
        },
        "Items": items
    }


def _pick_source_ip(dst_ip: str) -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((dst_ip, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


class _SourceIPAdapter(HTTPAdapter):
    def __init__(self, source_ip: str | None = None, **kwargs):
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        if self._source_ip:
            pool_kwargs["source_address"] = (self._source_ip, 0)
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


class PavoLocalClient:
    """
    - Self-signed için verify=False (cihaz lokal),
    - proxy/PAC devralma kapalı (trust_env=False),
    - çıkış IP bind (source IP),
    - TransactionSequence cache’te tutulur.
    """

    def __init__(
            self, *, ip: str, secure: bool, serial_number: str, fingerprint: str,
            port: int | None = None, timeout: float = 8.0, source_ip: str | None = None
    ):
        self.ip = ip.strip()
        self.secure = bool(secure)
        self.port = port or (DEFAULT_SECURE_PORT if self.secure else DEFAULT_NONSECURE_PORT)
        self.serial_number = serial_number.strip()
        self.fingerprint = fingerprint.strip()
        self.timeout = timeout

        self._seq_key = f"pavo_seq::{self.serial_number}::{self.fingerprint}"
        self.source_ip = source_ip or _pick_source_ip(self.ip)

        self.session = requests.Session()
        self.session.verify = False
        self.session.trust_env = False
        self._proxies = {"http": None, "https": None}

        adapter = _SourceIPAdapter(
            source_ip=self.source_ip,
            max_retries=Retry(total=0, redirect=0, connect=0, read=0, status=0)
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _scheme(self) -> str:
        return "https" if self.secure else "http"

    def _url(self, method: str) -> str:
        return f"{self._scheme()}://{self.ip}:{self.port}/{method}"

    def _next_sequence(self) -> int:
        val = cache.get(self._seq_key, 0)
        try:
            val = int(val)
        except Exception:
            val = 0
        val += 1
        cache.set(self._seq_key, val, timeout=None)
        return val

    def _handle(self) -> dict:
        return {
            "SerialNumber": self.serial_number,
            "Fingerprint": self.fingerprint,
            "TransactionSequence": self._next_sequence(),
            "TransactionDate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _post(self, method: str, payload: dict):
        data = dict(payload or {})
        data["TransactionHandle"] = self._handle()
        r = self.session.post(
            self._url(method),
            json=data,
            timeout=self.timeout,
            allow_redirects=False,
            proxies=self._proxies,
        )
        r.raise_for_status()
        return r.json()

    def pairing(self):
        return self._post("Pairing", {})

    def jewellery_sale(self, sale_payload: dict):
        return self._post("JewellerySale", sale_payload)

    def finalize_sale(self, payload: dict | None = None):
        return self._post("FinalizeSale", payload or {})

    def get_sale_result(self, payload: dict | None = None):
        return self._post("GetSaleResult", payload or {})

    def cancel_sale(self, payload: dict | None = None):
        return self._post("CancelSale", payload or {})

    def completed_sale(self, payload: dict | None = None):
        return self._post("CompletedSale", payload or {})

    def get_turmob_info(self, tckn: str):
        """
        POS Cihazından TÜRMOB sorgusu yapar.
        Payload: { "TurmobInput": { "Tckn": "..." } }
        """
        payload = {
            "TurmobInput": {
                "Tckn": str(tckn)
            }
        }
        return self._post("GetTurmobInfo", payload)

    # YENİ: POS /Sale endpoint’i – Amount/Currency/Reference ile çalışır
    def sale(self, payload: dict | None = None):
        return self._post("Sale", payload or {})
