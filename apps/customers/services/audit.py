"""Audit context çıkarımı.

Her CustomerLedger ve CustomerCustodyLedger satırı:
  - created_by  → kullanıcı
  - ip_address  → istek IP'si
  - user_agent  → tarayıcı/UA bilgisi

ile yazılır. Bu modül `request` nesnesinden bu bilgileri güvenli
şekilde çıkarır. Servis fonksiyonları doğrudan request almak
yerine bu sözlüğü kabul eder (test edilebilirlik için).
"""

from typing import Optional


def _get_client_ip(request) -> Optional[str]:
    if request is None:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _get_user_agent(request) -> str:
    if request is None:
        return ''
    return (request.META.get('HTTP_USER_AGENT') or '')[:255]


def extract_audit_context(request) -> dict:
    """request → {'actor', 'ip_address', 'user_agent'} sözlüğü.

    `actor` Django Users instance'ı veya None.
    """
    actor = None
    if request is not None:
        user = getattr(request, 'user', None)
        if user and getattr(user, 'is_authenticated', False):
            actor = user

    return {
        'actor': actor,
        'ip_address': _get_client_ip(request),
        'user_agent': _get_user_agent(request),
    }
