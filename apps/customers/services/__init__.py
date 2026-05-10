"""Customers services paketi (Cari/Emanet Refactor).

Modüller:
  - exceptions       — özel istisnalar
  - audit            — IP/UA/aktör çıkarımı
  - approval         — eşik bazlı onay kontrolü
  - ledger           — append-only Ledger yazımı + REVERSAL pattern
  - collection       — Tahsilat / Kapatma akışı (kur farkı + iskonto)
  - custody_offset   — Emanet ↔ Cari mahsuplaşma
  - product_payment  — Ürün/Hurda ile tahsilat ve ödeme (FAZ 49)
"""

from apps.customers.services.exceptions import (
    LedgerError,
    InsufficientApprovalError,
    BalanceMismatchError,
    InsufficientCustodyError,
    InvalidLedgerStateError,
)
from apps.customers.services.audit import extract_audit_context
from apps.customers.services.approval import (
    ApprovalLevel,
    determine_approval_requirement,
    approve_entry,
    evaluate_self_approval_capability,
)
from apps.customers.services.ledger import LedgerService
from apps.customers.services.collection import CollectionService
from apps.customers.services.custody_offset import CustodyOffsetService
from apps.customers.services.product_payment import (
    ProductPaymentService,
    ProductPaymentResult,
    ProductPaymentItemResult,
)

__all__ = [
    'LedgerError',
    'InsufficientApprovalError',
    'BalanceMismatchError',
    'InsufficientCustodyError',
    'InvalidLedgerStateError',
    'extract_audit_context',
    'ApprovalLevel',
    'determine_approval_requirement',
    'approve_entry',
    'evaluate_self_approval_capability',
    'LedgerService',
    'CollectionService',
    'CustodyOffsetService',
    'ProductPaymentService',
    'ProductPaymentResult',
    'ProductPaymentItemResult',
]
