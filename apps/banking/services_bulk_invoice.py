"""
services_bulk_invoice.py — Stub (invoices app kaldırıldı — Juwelier Plus Alman pazarı)

Bu dosya, invoices uygulaması kaldırıldığından stub olarak bırakılmıştır.
Toplu e-fatura oluşturma işlevselliği bu projede kullanılmamaktadır.
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class BulkInvoiceResult:
    """Stub sonuç sınıfı."""
    success_count: int = 0
    error_count: int = 0
    errors: list = field(default_factory=list)
    safe_txn_ids: list = field(default_factory=list)


class BulkInvoicePreflight:
    """Stub — invoices app bu projede yok."""

    def __init__(self, store, settings=None):
        self.store = store

    def run(self):
        return BulkInvoiceResult()


class BankBulkInvoiceService:
    """Stub — invoices app bu projede yok."""

    def __init__(self, store, settings=None):
        if not store:
            raise ValueError("BankBulkInvoiceService: store zorunlu.")
        self.store = store

    def build_bulk(self, txn_ids, send_to_gib=False):
        """Stub — her zaman boş sonuç döner."""
        return BulkInvoiceResult()
