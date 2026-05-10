"""
invoice_process.py — Stub (invoices app kaldırıldı — Juwelier Plus Alman pazarı)

Bu dosya, invoices uygulaması kaldırıldığından tüm fonksiyonlar boş stub olarak bırakılmıştır.
Gerçek fatura oluşturma işlevselliği bu projede kullanılmamaktadır.
"""
import logging
from decimal import Decimal

log = logging.getLogger(__name__)


def create_invoice_from_process(
        store,
        customer,
        process,
        product,
        operation_type,
        is_pos_flow,
        pavo_invoice_no,
        pavo_inquiry_data,
        pavo_sale_number,
        paid_total,
        pos_reference,
        qty,
        is_gram_bullion,
        unit_price,
        labor_net
):
    """Stub — invoices app kaldırıldı."""
    return None


def create_retail_bulk_invoice(
        store,
        customer,
        processes,
        is_pos_flow=False,
        pavo_data=None,
        payment_total=Decimal('0')
):
    """Stub — invoices app kaldırıldı."""
    return None


def _create_single_doc(store, customer, processes, doc_type, is_pos_flow, pavo_data, paid_amount):
    """Stub — invoices app kaldırıldı."""
    return None
