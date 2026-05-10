"""Cari/Emanet Refactor — Özel İstisnalar.

Tüm finansal akış istisnaları HTTP 400 (iş hatası) olarak
döndürülür; 500 (sunucu hatası) DEĞİL. View katmanı bu
istisnaları yakalayıp `error_code` ile yapılandırılmış JSON
yanıt döner.
"""

from decimal import Decimal


class LedgerError(Exception):
    """Tüm Ledger iş istisnalarının base'i."""
    error_code = 'LEDGER_ERROR'
    http_status = 400

    def __init__(self, message: str = '', **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class InsufficientApprovalError(LedgerError):
    """Eşik üstü işlem için yetersiz onay seviyesi."""
    error_code = 'INSUFFICIENT_APPROVAL'

    def __init__(self, required_level: str, actor_level: str, **extra):
        msg = (
            f'Bu işlem için {required_level} yetkisi gerekiyor; '
            f'mevcut yetki: {actor_level}.'
        )
        super().__init__(msg, **extra)
        self.required_level = required_level
        self.actor_level = actor_level


class BalanceMismatchError(LedgerError):
    """Tahsilat + ek fiş toplamı açık borç ile uyuşmuyor."""
    error_code = 'BALANCE_MISMATCH'

    def __init__(self, expected: Decimal, actual: Decimal, unit: str = 'HS', **extra):
        msg = (
            f'Toplam kapama tutarı uyuşmuyor: '
            f'beklenen {expected} {unit}, hesaplanan {actual} {unit}.'
        )
        super().__init__(msg, **extra)
        self.expected = expected
        self.actual = actual
        self.unit = unit


class InsufficientCustodyError(LedgerError):
    """Emanet bakiyesi yetersiz (mahsuplaşma veya çıkışta)."""
    error_code = 'INSUFFICIENT_CUSTODY'

    def __init__(self, available: Decimal, requested: Decimal, **extra):
        msg = (
            f'Mevcut emanet bakiyesi yetersiz: {available} HS, '
            f'talep: {requested} HS.'
        )
        super().__init__(msg, **extra)
        self.available = available
        self.requested = requested


class InvalidLedgerStateError(LedgerError):
    """Geçersiz state geçişi (ör. iptal edilmiş kayıt için REVERSAL)."""
    error_code = 'INVALID_LEDGER_STATE'
