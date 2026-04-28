# ============================================================================
# DOSYA: apps/invoices/esurec_expense_views.py
# KONUM: Kuyum Plus projesi içinde
# AÇIKLAMA: e-Gider Pusulası için e-Süreç entegrasyon view'ları.
#
# Bu modül 2026-04-18 itibarıyla esurec_views.py'den ayrıştırıldı.
#
# Sağlanan view'lar:
#   - esurec_send_expense               : Senkron taslak gönderim (tek / toplu)
#   - esurec_send_expense_to_gib        : GİB'e gönderim
#   - esurec_expense_status             : Durum sorgulama
#   - esurec_cancel_expense             : İptal
#   - esurec_reset_expense_to_draft     : ERROR → DRAFT sıfırlama
#   - esurec_expense_async_send         : Celery asenkron kuyruğa alma
#
# Değişiklikler:
#   - Payload nested yapıya geçti (header/supplier/beneficiary/items/totals).
#   - e-Süreç yanıtındaki 'esurec_voucher_id' alanı doğru okunur.
#   - Tenant izolasyonu: tüm client çağrıları seller_vkn parametresiyle yapılır.
#   - Her anahtar işlem InvoiceActivityLog kaydı oluşturur.
#   - Kuyum sektörüne özel alanlar (tevkifat, has altın, işçilik notu) payload'a
#     aktarılır.
# ============================================================================

import json
import logging
import uuid as _uuid
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.invoices.models import (
    Invoice, InvoiceSyncLog, InvoiceActivityLog,
)
from apps.invoices.esurec_client import ESurecClient
from apps.invoices.esurec_serializers import _n  # KP-01: Decimal hassasiyeti

log = logging.getLogger(__name__)


# ======================================================================
# YARDIMCILAR — esurec_views.py ile paralel (bağımsız kopya)
# ======================================================================

def _user_store(request):
    store = getattr(request.user, 'store', None)
    if not store:
        raise ValueError("Kullanıcıya bağlı mağaza bulunamadı.")
    return store


def _check_esurec_activation(store):
    """
    Mağazanın e-Süreç entegrasyonunun aktif olup olmadığını kontrol eder.
    esurec_views.py'deki aynı mantığın yerel kopyası.
    """
    from apps.banking.models import EsurecTenantCredential
    try:
        cred = EsurecTenantCredential.objects.filter(store=store).first()
        if not cred or not getattr(cred, 'is_active', False):
            return {
                'result': False,
                'error_msg': 'e-Süreç entegrasyonu bu mağaza için aktif değil.',
            }
    except Exception:
        pass
    return None


def _get_ids_from_request(request) -> list:
    try:
        body = json.loads(request.body.decode('utf-8'))
        ids = body.get('invoice_ids', [])
        if not ids:
            single = body.get('invoice_id')
            if single:
                ids = [single]
        return [i for i in ids if i]
    except Exception:
        ids = request.POST.getlist('invoice_ids[]') or [request.POST.get('invoice_id')]
        return [i for i in ids if i]


def _sanitize_gib_error(raw_error: str) -> str:
    """
    GİB / entegratör hatalarını kullanıcı dostu mesaja çevirir.
    Ham hatayı döndürmek yerine anlaşılır bir metin üretir.
    """
    if not raw_error:
        return ''
    raw_lower = raw_error.lower()
    _patterns = [
        ('00019', 'Entegratör seri tanımı eksik: e-Süreç panelinde bu firma için '
                  'Gider Pusulası / E-Fatura serisi tanımlayın.'),
        ('uygun alternatif seri', 'Seri tanımı eksik: e-Süreç → Firma Ayarları → Fatura Serileri.'),
        ('belge no üretilemedi', 'Entegratör belge numarası üretemedi. Seri tanımı eksik olabilir.'),
        ('connectionerror', 'e-Süreç sunucusuna bağlanılamadı. Tekrar deneyin.'),
        ('timeout', 'e-Süreç zaman aşımı. Tekrar deneyin.'),
        ('401', 'e-Süreç kimlik doğrulama hatası.'),
        ('429', 'Çok fazla istek. Lütfen bekleyin.'),
        ('500', 'e-Süreç sunucu hatası.'),
        ('tenant', 'Tenant izolasyon hatası: seller_vkn gönderilmemiş olabilir.'),
    ]
    for pattern, friendly in _patterns:
        if pattern in raw_lower:
            return friendly
    if len(raw_error) <= 200:
        return raw_error
    return 'İşlem sırasında hata oluştu. Tekrar deneyin veya yöneticinize başvurun.'


# ── e-Süreç ID eşlemesi için InvoiceSyncLog tabanlı yardımcılar ──

def _extract_esurec_id_from_notes(notes: str) -> str:
    for line in (notes or '').split('\n'):
        line = line.strip()
        if line.startswith('ESUREC_ID:'):
            return line.replace('ESUREC_ID:', '').strip()
    return ''


def _get_esurec_id(invoice) -> str:
    sync_log = InvoiceSyncLog.objects.filter(
        invoice=invoice,
        action=InvoiceSyncLog.Action.SEND_TO_ESUREC,
        status=InvoiceSyncLog.Status.SUCCESS,
        esurec_invoice_id__isnull=False,
    ).exclude(esurec_invoice_id='').order_by('-created_at').first()
    if sync_log:
        return sync_log.esurec_invoice_id
    return _extract_esurec_id_from_notes(getattr(invoice, 'notes', ''))


def _set_esurec_id(invoice, esurec_id: str, store=None):
    if not esurec_id:
        return
    InvoiceSyncLog.objects.update_or_create(
        invoice=invoice,
        action=InvoiceSyncLog.Action.SEND_TO_ESUREC,
        defaults={
            'status': InvoiceSyncLog.Status.SUCCESS,
            'esurec_invoice_id': esurec_id,
            'store': store or invoice.store,
        },
    )


def _get_seller_vkn_for_invoice(invoice) -> str:
    """
    Düzenleyen (mağaza / şirket) VKN'sini çözer.
    e-Süreç tenant izolasyonu için tüm çağrılarda zorunlu.
    """
    store = getattr(invoice, 'store', None)
    if store is None:
        return ''
    company = getattr(store, 'company', None)
    vkn = (getattr(company, 'tax_number', '') or
           getattr(store, 'tax_number', '') or '').strip()
    return vkn


def _log_activity(invoice, store, level, event, user_message, trace_id=None):
    """InvoiceActivityLog kaydı oluşturur. Exception fırlatmaz."""
    try:
        InvoiceActivityLog.objects.create(
            trace_id=trace_id or _uuid.uuid4(),
            invoice=invoice,
            store=store,
            level=level,
            event=event,
            user_message=(user_message or '')[:500],
        )
    except Exception as exc:
        log.warning(f"_log_activity yazılamadı: {exc}")


# ======================================================================
# PAYLOAD SERİALİZER — nested yapı + kuyum sektörü alanları
# ======================================================================

def _serialize_expense_voucher(invoice) -> dict:
    """
    PURCHASE tipindeki Invoice'u e-Süreç'in beklediği nested
    e-Gider Pusulası formatına çevirir.

    2026-04-18 yeniden yazımı:
      - Yapı: { header, supplier, beneficiary, items, totals }
      - Tevkifat (withholding_code + withholding_rate) aktarılır
      - Kuyum sektörü: has altın milyem bilgisi notes'a eklenir
    """
    store = invoice.store
    company = getattr(store, 'company', None)

    def _s(val):
        return str(val or '').strip()

    # ── Düzenleyen (mağaza / şirket) — supplier ──
    supplier = {
        'tax_number': _s(getattr(company, 'tax_number', '') or getattr(store, 'tax_number', '')),
        'title': _s(getattr(company, 'title', '') or getattr(store, 'title', '')),
    }

    # ── Lehdar (satıcı / müşteri) — beneficiary ──
    beneficiary = {}
    if invoice.supplier:
        beneficiary = {
            'vkn': _s(invoice.supplier.tax_number),
            'title': _s(invoice.supplier.company_name),
            'tax_office': _s(getattr(invoice.supplier, 'tax_office', '')),
            'address': _s(getattr(invoice.supplier, 'address', '')),
            'city': _s(getattr(invoice.supplier, 'city', '')),
            'district': _s(getattr(invoice.supplier, 'district', '')),
            'phone': _s(getattr(invoice.supplier, 'phone', '')),
            'email': _s(getattr(invoice.supplier, 'email', '')),
        }
    elif invoice.customer:
        cust = invoice.customer
        cust_vkn = _s(getattr(cust, 'id_number', '') or getattr(cust, 'tax_number', ''))
        beneficiary = {
            'vkn': cust_vkn,
            'title': _s(f"{cust.first_name} {cust.last_name}"),
            'first_name': _s(cust.first_name),
            'family_name': _s(cust.last_name),
            'nationality': 'Türkiye' if len(cust_vkn) == 11 else '',
            'tax_office': _s(getattr(cust, 'tax_office', '')),
            'address': _s(getattr(cust, 'address', '')),
            'city': _s(getattr(cust, 'city', '')),
            'district': _s(getattr(cust, 'district', '')),
            'phone': _s(getattr(cust, 'phone', '')),
            'email': _s(getattr(cust, 'email', '')),
        }

    # ── Kalemler ──
    items = []
    subtotal = Decimal('0')
    vat_total = Decimal('0')
    for line in invoice.items.all():
        qty = Decimal(str(line.quantity or 0))
        price = Decimal(str(line.unit_price or 0))
        discount = Decimal(str(line.discount_amount or 0))
        net = (qty * price) - discount
        vat_rate = Decimal(str(line.vat_rate or 0))
        vat_amount = (net * vat_rate / Decimal('100')).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )

        subtotal += net
        vat_total += vat_amount

        # ── Kuyum sektörü satır notu: milyem / has altın detayı ──
        line_note = _s(line.notes)
        if getattr(line, 'is_gram_bullion', False) and not line_note:
            line_note = f"Has altın — {getattr(line, 'jewelry_type', '') or 'Külçe'}".strip()

        # ── Tevkifat ──
        withholding_code = ''
        withholding_rate = None
        wh_rate_val = Decimal(str(getattr(line, 'withholding_rate', 0) or 0))
        if wh_rate_val > 0:
            # %9 tevkifat → kod 0009 (GİB standardı: hurda altın / işçilik)
            withholding_rate = float(wh_rate_val)
            withholding_code = f"{int(wh_rate_val):04d}"

        item_payload = {
            'code': _s(getattr(line, 'barcode', '')),
            'name': _s(line.product_name) or 'Ürün',
            'quantity': _n(qty, 3),
            'unit': _map_unit_to_ubl(line.unit),
            'unit_price': _n(price, 3),
            'vat_rate': int(vat_rate),
            'discount_amount': _n(discount, 2),
            'exemption_code': _s(getattr(line, 'exemption_code', '')),
            'exemption_reason': _s(getattr(line, 'exemption_reason', '')),
            'withholding_code': withholding_code,
            'brand_name': _s(getattr(line, 'brand_name', '')),
            'model_name': _s(getattr(line, 'model_name', '')),
            'notes': line_note,
        }
        if withholding_rate is not None:
            item_payload['withholding_rate'] = withholding_rate

        items.append(item_payload)

    # ── Header ──
    header = {
        'external_ref': _s(invoice.invoice_no),
        'voucher_date': invoice.issue_date.strftime('%Y-%m-%d') if invoice.issue_date else '',
        'issue_time': invoice.issue_date.strftime('%H:%M:%S') if invoice.issue_date else None,
        'currency': _s(invoice.currency) or 'TRY',
        'exchange_rate': _n(invoice.exrate_to_try or 1, 4),
        'notes': _s(invoice.notes),
    }

    # ── Toplamlar ──
    grand_total = Decimal(str(invoice.grand_total or 0))
    totals = {
        'subtotal': _n(subtotal, 2),
        'tax_total': _n(vat_total, 2),
        'tax_exclusive_amount': _n(subtotal, 2),
        'tax_inclusive_amount': _n(grand_total, 2),
        'allowance_total_amount': _n(invoice.discount_total or 0, 2),
        'payable_amount': _n(grand_total, 2),
    }

    return {
        'header': header,
        'supplier': supplier,
        'beneficiary': beneficiary,
        'items': items,
        'totals': totals,
    }


def _map_unit_to_ubl(unit_code):
    """
    Kuyum Plus InvoiceItem.Unit değerlerini UBL birim kodlarına çevirir.
      GR (Gram)  → GRM
      AD (Adet)  → C62
      KG         → KGM
      CM         → CMT
    """
    mapping = {
        'GR': 'GRM',
        'AD': 'C62',
        'KG': 'KGM',
        'CM': 'CMT',
    }
    return mapping.get(str(unit_code or '').upper().strip(), 'C62')


# ======================================================================
# 1. GİDER PUSULASINI e-SÜREÇ'E TASLAK GÖNDER
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_send_expense(request):
    """
    Gider Pusulalarını e-Süreç'e taslak olarak gönderir.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice_ids = _get_ids_from_request(request)
        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gider pusulası seçilmedi.'})

        client = ESurecClient()
        sent = 0
        errors = []

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.select_related(
                    'customer', 'supplier', 'store', 'store__company',
                ).prefetch_related('items').get(
                    id=inv_id, store=store, is_deleted=False,
                    invoice_type=Invoice.Type.PURCHASE,
                )

                # Zaten e-Süreç'e gönderilmişse atla
                esurec_id = _get_esurec_id(invoice)
                if esurec_id:
                    errors.append(f'{invoice.invoice_no}: Zaten e-Süreç\'te.')
                    continue

                payload = _serialize_expense_voucher(invoice)
                resp = client.send_expense_voucher(payload)

                if resp.get('result'):
                    # 2026-04-18 düzeltmesi: e-Süreç 'esurec_voucher_id' döndürür
                    new_esurec_id = (
                        resp.get('esurec_voucher_id', '') or
                        resp.get('voucher_id', '') or
                        resp.get('id', '')
                    )
                    if new_esurec_id:
                        _set_esurec_id(invoice, new_esurec_id, store=store)
                    invoice.gib_status_code = '10'
                    invoice.gib_status_desc = 'e-Süreç Taslağı'
                    invoice.save(update_fields=['gib_status_code', 'gib_status_desc', 'updated_at'])
                    sent += 1

                    _log_activity(
                        invoice, store,
                        InvoiceActivityLog.Level.INFO,
                        InvoiceActivityLog.Event.SEND_ATTEMPT,
                        f'Gider pusulası "{invoice.invoice_no}" e-Süreç taslağı oluşturuldu.',
                    )
                else:
                    err_msg = resp.get('error_msg') or resp.get('msg', 'Gönderim hatası')
                    errors.append(f'{invoice.invoice_no}: {err_msg[:120]}')

                    _log_activity(
                        invoice, store,
                        InvoiceActivityLog.Level.ERROR,
                        InvoiceActivityLog.Event.VALIDATION_ERROR,
                        _sanitize_gib_error(err_msg),
                    )

            except Invoice.DoesNotExist:
                errors.append(f'{inv_id}: Gider pusulası bulunamadı.')
            except Exception as e:
                log.exception(f"esurec_send_expense hata: invoice_id={inv_id}, error={type(e).__name__}")
                errors.append(f'{inv_id}: {_sanitize_gib_error(str(e))}')

        msg = f'{sent} gider pusulası e-Süreç\'e gönderildi.'
        if errors:
            msg += f' ({len(errors)} hata: {"; ".join(errors[:3])})'

        return JsonResponse({'result': sent > 0, 'msg': msg})

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_send_expense hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 2. GİDER PUSULASINI GİB'E GÖNDER
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_send_expense_to_gib(request):
    """
    e-Süreç'teki taslak gider pusulalarını GİB'e (MySoft entegratörüne) gönderir.
    Tenant izolasyonu için seller_vkn gönderilir.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice_ids = _get_ids_from_request(request)
        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gider pusulası seçilmedi.'})

        client = ESurecClient()
        sent = 0
        errors = []

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.select_related('store', 'store__company').get(
                    id=inv_id, store=store, is_deleted=False,
                    invoice_type=Invoice.Type.PURCHASE,
                )

                seller_vkn = _get_seller_vkn_for_invoice(invoice)
                if not seller_vkn:
                    errors.append(f'{invoice.invoice_no}: Mağaza/firma VKN tanımlı değil.')
                    continue

                esurec_id = _get_esurec_id(invoice)
                if not esurec_id:
                    errors.append(f'{invoice.invoice_no}: Önce e-Süreç\'e taslak gönderilmeli.')
                    continue

                # Zaten GİB sürecinde mi?
                if invoice.gib_status_code and str(invoice.gib_status_code) in [
                    '100', '1000', '1100', '1200', '1300'
                ]:
                    errors.append(f'{invoice.invoice_no}: Zaten GİB sürecinde.')
                    continue

                resp = client.send_expense_voucher_to_gib(esurec_id, seller_vkn=seller_vkn)

                if resp.get('result'):
                    invoice.gib_status_code = str(resp.get('gib_status_code', '100'))
                    invoice.gib_status_desc = resp.get('gib_status_description', '') or "GİB'e Gönderildi"
                    invoice.status = Invoice.Status.SENT
                    invoice.gib_error = ''
                    invoice.save(update_fields=[
                        'gib_status_code', 'gib_status_desc', 'status',
                        'gib_error', 'updated_at',
                    ])
                    sent += 1

                    _log_activity(
                        invoice, store,
                        InvoiceActivityLog.Level.INFO,
                        InvoiceActivityLog.Event.GIB_SUCCESS,
                        f'Gider pusulası "{invoice.invoice_no}" GİB\'e gönderildi.',
                    )
                else:
                    err_msg = resp.get('error_msg') or resp.get('msg', 'Gönderim hatası')
                    friendly = _sanitize_gib_error(err_msg)
                    invoice.gib_status_code = str(resp.get('gib_status_code', '1400'))
                    invoice.gib_error = friendly[:500]
                    invoice.status = Invoice.Status.ERROR
                    invoice.save(update_fields=[
                        'gib_status_code', 'gib_error', 'status', 'updated_at',
                    ])
                    errors.append(f'{invoice.invoice_no}: {friendly[:100]}')

                    _log_activity(
                        invoice, store,
                        InvoiceActivityLog.Level.ERROR,
                        InvoiceActivityLog.Event.GIB_ERROR,
                        friendly,
                    )

            except Invoice.DoesNotExist:
                errors.append(f'{inv_id}: Gider pusulası bulunamadı.')
            except Exception as e:
                log.exception(f"esurec_send_expense_to_gib hata: invoice_id={inv_id}, error={type(e).__name__}")
                errors.append(f'{inv_id}: {_sanitize_gib_error(str(e))}')

        msg = f'{sent} gider pusulası GİB\'e gönderildi.'
        if errors:
            msg += f' ({len(errors)} hata: {"; ".join(errors[:3])})'

        return JsonResponse({'result': sent > 0, 'msg': msg})

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_send_expense_to_gib hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 3. GİDER PUSULASI DURUM SORGULAMA
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_expense_status(request):
    """
    Gider pusulalarının GİB durumunu e-Süreç üzerinden sorgular ve günceller.
    Tenant izolasyonu için seller_vkn gönderilir.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice_ids = _get_ids_from_request(request)
        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gider pusulası seçilmedi.'})

        client = ESurecClient()
        updated = 0
        errors = []

        _STATUS_MAP = {
            '1300': (Invoice.Status.ISSUED, 'GİB Onayladı'),
            '1230': (Invoice.Status.CANCELED, 'İptal Edildi'),
            '1220': (Invoice.Status.SENT, 'Entegratöre İletildi (Cevap Bekliyor)'),
            '1210': (Invoice.Status.SENT, 'Zarflandı-İmzalandı'),
            '1200': (Invoice.Status.SENT, 'Zarflandı'),
            '1100': (Invoice.Status.SENT, 'İşleniyor'),
            '1000': (Invoice.Status.QUEUED, 'Kuyrukta'),
            '100': (Invoice.Status.SENT, 'e-Süreç Kabul Etti'),
            '1400': (Invoice.Status.ERROR, 'GİB Hatası'),
            '1500': (Invoice.Status.REJECTED, 'Reddedildi'),
            'CANCELLED': (Invoice.Status.CANCELED, 'İptal'),
        }

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.select_related('store', 'store__company').get(
                    id=inv_id, store=store, is_deleted=False,
                    invoice_type=Invoice.Type.PURCHASE,
                )

                seller_vkn = _get_seller_vkn_for_invoice(invoice)
                if not seller_vkn:
                    continue

                esurec_id = _get_esurec_id(invoice)
                if not esurec_id:
                    continue

                resp = client.check_expense_voucher_status(esurec_id, seller_vkn=seller_vkn)

                if resp.get('result'):
                    new_code = str(
                        resp.get('gib_status_code', '') or
                        resp.get('status_code', '') or ''
                    ).strip()
                    new_desc = str(
                        resp.get('gib_status_description', '') or
                        resp.get('status_description', '') or
                        resp.get('gib_status_desc', '') or ''
                    ).strip()

                    if new_code and new_code != str(invoice.gib_status_code or ''):
                        mapped = _STATUS_MAP.get(new_code)
                        update_fields = ['gib_status_code', 'gib_status_desc', 'updated_at']
                        if mapped:
                            invoice.status = mapped[0]
                            update_fields.append('status')
                            if not new_desc:
                                new_desc = mapped[1]

                        invoice.gib_status_code = new_code
                        invoice.gib_status_desc = new_desc

                        if new_code in ('1400', '1500'):
                            invoice.gib_error = _sanitize_gib_error(new_desc)[:500]
                            update_fields.append('gib_error')

                            _log_activity(
                                invoice, store,
                                InvoiceActivityLog.Level.ERROR,
                                InvoiceActivityLog.Event.GIB_ERROR,
                                invoice.gib_error,
                            )
                        elif new_code == '1300':
                            invoice.gib_error = ''
                            update_fields.append('gib_error')

                            _log_activity(
                                invoice, store,
                                InvoiceActivityLog.Level.INFO,
                                InvoiceActivityLog.Event.GIB_SUCCESS,
                                f'Gider pusulası "{invoice.invoice_no}" GİB\'de onaylandı.',
                            )
                        else:
                            _log_activity(
                                invoice, store,
                                InvoiceActivityLog.Level.INFO,
                                InvoiceActivityLog.Event.STATUS_CHANGE,
                                f'Gider pusulası "{invoice.invoice_no}" durumu: {new_desc}',
                            )

                        invoice.save(update_fields=update_fields)
                        updated += 1

            except Invoice.DoesNotExist:
                errors.append(f'{inv_id}: Gider pusulası bulunamadı.')
            except Exception as e:
                log.exception(f"esurec_expense_status hata: invoice_id={inv_id}, error={type(e).__name__}")
                errors.append(f'{inv_id}: {_sanitize_gib_error(str(e))}')

        msg = f'{updated} gider pusulası durumu güncellendi.'
        if errors:
            msg += f' ({len(errors)} hata)'

        return JsonResponse({'result': True, 'msg': msg})

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_expense_status hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 4. GİDER PUSULASI İPTAL
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_cancel_expense(request):
    """
    e-Süreç üzerinden gider pusulasını iptal eder.
    GİB onaylı (1300) belgeler iptal edilemez.
    """
    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        invoice_ids = _get_ids_from_request(request)
        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gider pusulası seçilmedi.'})

        try:
            body = json.loads(request.body.decode('utf-8'))
        except Exception:
            body = {}
        reason = (body.get('reason') or '').strip()

        client = ESurecClient()
        cancelled = 0
        errors = []

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.select_related('store', 'store__company').get(
                    id=inv_id, store=store, is_deleted=False,
                    invoice_type=Invoice.Type.PURCHASE,
                )

                if str(invoice.gib_status_code or '') == '1300':
                    errors.append(f'{invoice.invoice_no}: GİB onaylı belge iptal edilemez.')
                    continue

                seller_vkn = _get_seller_vkn_for_invoice(invoice)
                esurec_id = _get_esurec_id(invoice)

                cancelled_remote = False
                if esurec_id and seller_vkn:
                    resp = client.cancel_expense_voucher(
                        esurec_id, seller_vkn=seller_vkn, reason=reason,
                    )
                    cancelled_remote = bool(resp.get('result'))

                invoice.status = Invoice.Status.CANCELED
                invoice.gib_status_code = 'CANCELLED'
                invoice.gib_status_desc = 'İptal edildi'
                invoice.save(update_fields=[
                    'status', 'gib_status_code', 'gib_status_desc', 'updated_at',
                ])

                _log_activity(
                    invoice, store,
                    InvoiceActivityLog.Level.INFO,
                    InvoiceActivityLog.Event.CANCEL,
                    (
                        f'Gider pusulası "{invoice.invoice_no}" iptal edildi. '
                        f'(Entegratör iptali: {"Başarılı" if cancelled_remote else "Atlandı"})'
                    ),
                )
                cancelled += 1

            except Invoice.DoesNotExist:
                errors.append(f'{inv_id}: Gider pusulası bulunamadı.')
            except Exception as e:
                log.exception(f"esurec_cancel_expense hata: invoice_id={inv_id}, error={type(e).__name__}")
                errors.append(f'{inv_id}: {_sanitize_gib_error(str(e))}')

        msg = f'{cancelled} gider pusulası iptal edildi.'
        if errors:
            msg += f' ({len(errors)} hata: {"; ".join(errors[:3])})'

        return JsonResponse({'result': cancelled > 0, 'msg': msg})

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_cancel_expense hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})


# ======================================================================
# 5. ERROR → DRAFT SIFIRLAMA (Düzenlemeye Al)
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_reset_expense_to_draft(request):
    """
    ERROR statüsündeki gider pusulasını DRAFT'a döndürür.

    Güvenlik Kuralları:
      - Sadece ERROR statüsündekiler reset edilebilir.
      - GİB onaylı (1300) belgeler reset edilemez.
      - e-Süreç'teki taslak varsa cancel edilir (best-effort).
      - İlgili InvoiceSyncLog kayıtları SKIPPED'a çekilir.
      - InvoiceActivityLog DRAFT_RESET kaydı oluşturulur.
    """
    try:
        store = _user_store(request)
        invoice_ids = _get_ids_from_request(request)

        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gider pusulası seçilmedi.'})

        results = []
        success_count = 0

        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.select_related('store', 'store__company').get(
                    id=inv_id, store=store, is_deleted=False,
                    invoice_type=Invoice.Type.PURCHASE,
                )

                # ── Güvenlik: Sadece ERROR ──
                if invoice.status != Invoice.Status.ERROR:
                    results.append({
                        'invoice_no': invoice.invoice_no, 'result': False,
                        'error_msg': (
                            f'Gider pusulası "{invoice.invoice_no}" şu an '
                            f'"{invoice.get_status_display()}" durumunda. '
                            f'Yalnızca "Hata Aldı" durumundaki belgeler düzenlemeye alınabilir.'
                        ),
                    })
                    continue

                # ── Güvenlik: GİB onaylı reset edilemez ──
                if str(invoice.gib_status_code or '') == '1300':
                    results.append({
                        'invoice_no': invoice.invoice_no, 'result': False,
                        'error_msg': (
                            f'Gider pusulası "{invoice.invoice_no}" GİB onaylı '
                            f'(1300). GİB\'de onaylanmış belgeler düzenlemeye alınamaz.'
                        ),
                    })
                    continue

                # ── ADIM 1: e-Süreç taslağını cancel et ──
                esurec_id = _get_esurec_id(invoice)
                seller_vkn = _get_seller_vkn_for_invoice(invoice)
                cancel_note = ''
                if esurec_id and seller_vkn:
                    try:
                        client = ESurecClient()
                        cancel_resp = client.cancel_expense_voucher(
                            esurec_id, seller_vkn=seller_vkn, reason='Reset to draft',
                        )
                        if cancel_resp.get('result'):
                            cancel_note = ' e-Süreç taslağı iptal edildi.'
                        else:
                            cancel_note = ' e-Süreç taslak iptali atlandı (önemsiz).'
                    except Exception as cancel_exc:
                        cancel_note = ' e-Süreç taslak iptali atlandı (bağlantı hatası).'
                        log.warning(
                            "esurec_reset_expense_to_draft cancel exception: "
                            "invoice=%s error=%s",
                            invoice.invoice_no, type(cancel_exc).__name__,
                        )

                # ── ADIM 2: SyncLog temizliği ──
                InvoiceSyncLog.objects.filter(
                    invoice=invoice,
                    status__in=['FAILED', 'QUEUED', 'PROCESSING', 'RETRYING', 'SUCCESS'],
                ).update(status='SKIPPED')

                # ── ADIM 3: Belge sıfırlama ──
                trace_id = _uuid.uuid4()
                with transaction.atomic():
                    invoice.status = Invoice.Status.DRAFT
                    invoice.gib_status_code = ''
                    invoice.gib_status_desc = ''
                    invoice.gib_error = ''
                    invoice.save(update_fields=[
                        'status', 'gib_status_code', 'gib_status_desc',
                        'gib_error', 'updated_at',
                    ])

                # ── ADIM 4: ActivityLog ──
                user_msg = (
                    f'Gider pusulası "{invoice.invoice_no}" düzenleme moduna alındı.'
                    f'{cancel_note} '
                    f'Bilgileri güncelleyerek tekrar GİB\'e gönderebilirsiniz.'
                )
                _log_activity(
                    invoice, store,
                    InvoiceActivityLog.Level.INFO,
                    InvoiceActivityLog.Event.DRAFT_RESET,
                    user_msg,
                    trace_id=trace_id,
                )

                log.info(
                    "[RESET-EV] Gider pusulası %s → DRAFT'a döndürüldü. trace_id=%s",
                    invoice.invoice_no, trace_id,
                )

                results.append({
                    'invoice_no': invoice.invoice_no,
                    'result': True,
                    'msg': user_msg,
                })
                success_count += 1

            except Invoice.DoesNotExist:
                results.append({
                    'invoice_id': inv_id, 'result': False,
                    'error_msg': 'Gider pusulası bulunamadı.',
                })

        if len(invoice_ids) == 1:
            return JsonResponse(results[0] if results else {
                'result': False, 'error_msg': 'İşlem yapılamadı.',
            })

        return JsonResponse({
            'result': success_count > 0,
            'msg': f'{success_count}/{len(invoice_ids)} gider pusulası düzenlemeye alındı.',
            'details': results,
        })

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_reset_expense_to_draft hatası: {type(e).__name__}")
        return JsonResponse({
            'result': False,
            'error_msg': 'Gider pusulası düzenlemeye alınırken hata oluştu.',
        })


# ======================================================================
# 6. ASENKRON GÖNDERİM (Celery)
# ======================================================================

@login_required(login_url='login')
@require_POST
def esurec_expense_async_send(request):
    """
    Gider pusulalarını Celery kuyruğuna atar.
    UI bloklama yaşanmaması için tercih edilir.

    Body: { invoice_ids: [..], mode: 'esurec' | 'gib' }
      mode='esurec' → send_expense_voucher_to_esurec_task
      mode='gib'    → send_expense_voucher_to_gib_task
    """
    from apps.invoices.tasks import (
        send_expense_voucher_to_esurec_task,
        send_expense_voucher_to_gib_task,
    )

    try:
        store = _user_store(request)

        activation_error = _check_esurec_activation(store)
        if activation_error:
            return JsonResponse(activation_error)

        try:
            body = json.loads(request.body.decode('utf-8'))
        except Exception:
            body = {}

        invoice_ids = body.get('invoice_ids', []) or []
        if not invoice_ids and body.get('invoice_id'):
            invoice_ids = [body.get('invoice_id')]
        invoice_ids = [i for i in invoice_ids if i]

        mode = (body.get('mode') or 'esurec').lower()
        if mode not in ('esurec', 'gib'):
            return JsonResponse({'result': False, 'error_msg': 'Geçersiz mode.'})

        if not invoice_ids:
            return JsonResponse({'result': False, 'error_msg': 'Gider pusulası seçilmedi.'})

        enqueued = 0
        for inv_id in invoice_ids:
            try:
                invoice = Invoice.objects.get(
                    id=inv_id, store=store, is_deleted=False,
                    invoice_type=Invoice.Type.PURCHASE,
                )

                # QUEUED işaretle
                invoice.status = Invoice.Status.QUEUED
                invoice.save(update_fields=['status', 'updated_at'])

                # Kuyruğa QUEUED sync log yaz
                InvoiceSyncLog.objects.create(
                    invoice=invoice,
                    store=store,
                    action=(
                        InvoiceSyncLog.Action.SEND_TO_ESUREC
                        if mode == 'esurec'
                        else InvoiceSyncLog.Action.SEND_TO_GIB
                    ),
                    status=InvoiceSyncLog.Status.QUEUED,
                )

                if mode == 'esurec':
                    send_expense_voucher_to_esurec_task.delay(str(invoice.id), str(store.id))
                else:
                    send_expense_voucher_to_gib_task.delay(str(invoice.id), str(store.id))
                enqueued += 1

            except Invoice.DoesNotExist:
                continue
            except Exception as e:
                log.warning(f"Kuyruğa atma hatası: {type(e).__name__}")

        return JsonResponse({
            'result': enqueued > 0,
            'msg': f'{enqueued} gider pusulası kuyruğa alındı ({mode}).',
        })

    except ValueError as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})
    except Exception as e:
        log.exception(f"esurec_expense_async_send hatası: {type(e).__name__}")
        return JsonResponse({'result': False, 'error_msg': _sanitize_gib_error(str(e))})
