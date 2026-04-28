# Uygulama içi modeller ve view'lar
from apps.invoices.models import *
from apps.settings.models import *
from apps.stores.models import *


# --- YARDIMCI VE DÖNÜŞÜM FONKSİYONLARI ---

def _dec(x, q='0.01'):
    """Güvenli Decimal dönüşümü."""
    try:
        return Decimal(str(x)).quantize(Decimal(q), rounding=ROUND_HALF_UP)
    except:
        return Decimal('0.00')


def _get_store_config(user):
    """Mağaza konfigürasyonunu getirir."""
    store = getattr(user, 'store', None)
    if not store:
        return None
    config, _ = StoreConfiguration.objects.get_or_create(store=store)
    return config


# --- PAVO (POS) YARDIMCILARI ---

def _pavo_extract_data(pavo_obj: dict) -> dict:
    if not isinstance(pavo_obj, dict): return {}
    return pavo_obj.get('Data') or pavo_obj.get('CompleteSale') or pavo_obj.get('Sale') or {}


def _pavo_extract_status_id(data: dict):
    if not isinstance(data, dict): return None
    sid = data.get('StatusId')
    if sid is None: sid = data.get('SaleStatusId')
    sale_sub = data.get('Sale')
    if sid is None and isinstance(sale_sub, dict):
        sid = sale_sub.get('StatusId') or sale_sub.get('SaleStatusId')
    try:
        return int(sid)
    except:
        return None


def _normalize_http_url(u: str) -> str:
    s = (u or '').strip()
    if not s: return ''
    if s.startswith('http://') or s.startswith('https://'): return s
    return 'https://' + s.lstrip('/')


def _pavo_pick_inquiry_fields(pavo_obj: dict, pavo_data: dict) -> dict:
    src = pavo_data if isinstance(pavo_data, dict) else {}
    sale = src.get('Sale') if isinstance(src.get('Sale'), dict) else {}

    def _get(k):
        v = src.get(k)
        if v is None and isinstance(sale, dict): v = sale.get(k)
        return v

    out = {
        'SaleUid': _get('SaleUid') or '',
        'IsInFlightSale': bool(_get('IsInFlightSale')) if _get('IsInFlightSale') is not None else False,
        'CancelRequested': bool(_get('CancelRequested')) if _get('CancelRequested') is not None else False,
        'SaleInquieryLink': _normalize_http_url(
            str(_get('SaleInquieryLink') or _get('SaleInquiryLink') or '').strip()),
        'IsSuspendedSale': bool(_get('IsSuspendedSale')) if _get('IsSuspendedSale') is not None else False,
        'RemainingPaymentAmount': None
    }
    try:
        rpa = _get('RemainingPaymentAmount')
        out['RemainingPaymentAmount'] = float(rpa) if rpa is not None else None
    except:
        out['RemainingPaymentAmount'] = None
    return out





