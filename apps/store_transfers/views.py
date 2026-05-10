# ============================================================================
# DOSYA: apps/store_transfers/views.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v1 — FAZ 46: Şubeler Arası Kasa Transferi View Katmanı
#
# AMAÇ:
#   StoreTransferService'i HTTP istek-yanıtına bağlayan ince katman.
#   Tüm iş mantığı servisin içindedir; bu dosya yetkilendirme + payload
#   doğrulama + JSON serileştirme yapar.
#
# ENDPOINT'LER:
#   GET  /store-transfers/                          → transfer_index
#   GET  /store-transfers/get-all                   → transfer_get_all (DataTables)
#   GET  /store-transfers/source-accounts           → transfer_source_accounts (modal helper)
#   GET  /store-transfers/destination-stores        → transfer_destination_stores (modal helper)
#   POST /store-transfers/create                    → transfer_create_action
#   GET  /store-transfers/<uuid>/detail             → transfer_detail (JSON)
#   POST /store-transfers/<uuid>/dispatch           → transfer_dispatch_action
#   POST /store-transfers/<uuid>/accept             → transfer_accept_action
#   POST /store-transfers/<uuid>/reject             → transfer_reject_action
#   POST /store-transfers/<uuid>/cancel             → transfer_cancel_action
# ============================================================================

import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Count
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.roles.decorators import role_required
from apps.stores.models import Stores
from apps.banking.models import BankAccount
from apps.products.models import Products
from apps.stock_management.models import StockSnapshot

from apps.store_transfers.models import StoreTransfer, StoreTransferItem
from apps.store_transfers.services import (
    StoreTransferService,
    TransferError,
    InvalidTransferStateError,
)

log = logging.getLogger(__name__)


# ============================================================================
# YARDIMCILAR
# ============================================================================

def _err(msg: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'result': False, 'error_msg': msg}, status=status)


def _parse_decimal(value, *, field: str = '') -> Decimal:
    try:
        return Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, ValueError, AttributeError):
        raise TransferError(f"Geçersiz sayı: {field} = {value!r}")


def _serialize_transfer(t: StoreTransfer, *, include_items: bool = False) -> dict:
    """Transfer'ın UI için sözlük gösterimi."""
    payload = {
        'id': str(t.id),
        'transfer_no': t.transfer_no or '',
        'status': t.status,
        'status_label': t.get_status_display(),
        'transfer_type': t.transfer_type,
        'source_store': {
            'id': str(t.source_store_id),
            'title': t.source_store.title or t.source_store.store_id or '-',
            'branch_code': t.source_store.branch_code or '',
        },
        'destination_store': {
            'id': str(t.destination_store_id),
            'title': t.destination_store.title or t.destination_store.store_id or '-',
            'branch_code': t.destination_store.branch_code or '',
        },
        'initiated_by': getattr(t.initiated_by, 'username', None),
        'dispatched_by': getattr(t.dispatched_by, 'username', None),
        'accepted_by': getattr(t.accepted_by, 'username', None),
        'rejected_by': getattr(t.rejected_by, 'username', None),
        'notes_sender': t.notes_sender or '',
        'notes_receiver': t.notes_receiver or '',
        'initiated_at': t.initiated_at.isoformat() if t.initiated_at else None,
        'dispatched_at': t.dispatched_at.isoformat() if t.dispatched_at else None,
        'completed_at': t.completed_at.isoformat() if t.completed_at else None,
        'expected_arrival_date': t.expected_arrival_date.isoformat() if t.expected_arrival_date else None,
        'is_overdue': t.is_overdue,
        'is_pending_action': t.is_pending_action,
        'is_terminal': t.is_terminal,
        'total_cash_tl_equivalent': str(t.total_cash_tl_equivalent or 0),
        'line_count': t.line_count or 0,
    }
    if include_items:
        payload['items'] = [_serialize_item(i) for i in t.items.all()]
    return payload


def _serialize_item(i: StoreTransferItem) -> dict:
    base = {
        'id': str(i.id),
        'item_type': i.item_type,
        'item_status': i.item_status,
        'item_status_label': i.get_item_status_display(),
        'rejection_reason': i.rejection_reason or '',
    }
    if i.item_type == StoreTransferItem.ItemType.CASH:
        base.update({
            'currency': i.currency or '',
            'amount': str(i.amount or 0),
            'amount_eur_equivalent': str(i.amount_eur_equivalent or 0),
            'exchange_rate_at_dispatch': str(i.exchange_rate_at_dispatch) if i.exchange_rate_at_dispatch else None,
            'accepted_amount': str(i.accepted_amount) if i.accepted_amount is not None else None,
            'source_bank_account': (
                {
                    'id': str(i.source_bank_account_id),
                    'name': i.source_bank_account.name,
                } if i.source_bank_account_id else None
            ),
            'destination_bank_account': (
                {
                    'id': str(i.destination_bank_account_id),
                    'name': i.destination_bank_account.name,
                } if i.destination_bank_account_id else None
            ),
        })
    else:  # STOCK
        base.update({
            'product': (
                {
                    'id': str(i.product_id),
                    'name': i.product.name if i.product_id else '-',
                    'barcode': getattr(i.product, 'barcode', '') if i.product_id else '',
                    'material_type': getattr(i.product, 'material_type', '') if i.product_id else '',
                } if i.product_id else None
            ),
            'quantity_pieces': i.quantity_pieces or 0,
            'quantity_gram': str(i.quantity_gram or 0),
            'unit_cost_hs': str(i.unit_cost_hs or 0),
            'unit_cost_eur': str(i.unit_cost_eur or 0),
            'accepted_pieces': i.accepted_pieces if i.accepted_pieces is not None else None,
            'accepted_gram': str(i.accepted_gram) if i.accepted_gram is not None else None,
            'has_source_ledger': bool(i.source_stock_ledger_id),
            'has_destination_ledger': bool(i.destination_stock_ledger_id),
        })
    return base


# ============================================================================
# 1) ANA SAYFA
# ============================================================================

@login_required
@role_required('STORE_TRANSFERS_INDEX')
def transfer_index(request):
    """Transfer yönetim sayfası: Gönderilen + Gelen sekmeleri."""
    user_store = request.user.store
    pending_inbound = 0
    if user_store is not None:
        pending_inbound = StoreTransfer.objects.filter(
            destination_store=user_store,
            status=StoreTransfer.Status.IN_TRANSIT,
        ).count()

    # FAZ 47 — STOCK transferi için runtime yetki bayrağı (template'e iletilir)
    from apps.roles.models import RoleDetail
    can_stock_transfer = False
    if request.user.is_authenticated and request.user.role_id:
        can_stock_transfer = RoleDetail.objects.filter(
            role=request.user.role,
            permission__code='TRANSFER_STOCK_CREATE',
            status=True,
        ).exists()

    return render(request, 'management/store_transfers/index.html', {
        'store': user_store,
        'pending_inbound_count': pending_inbound,
        'can_stock_transfer': can_stock_transfer,
    })


# ============================================================================
# 2) DATATABLES SERVER-SIDE LİSTESİ
# ============================================================================

@login_required
@role_required('STORE_TRANSFERS_GET_ALL')
@require_GET
def transfer_get_all(request):
    """DataTables için server-side liste.

    Query params:
        direction: 'outbound' | 'inbound' | 'all'   (varsayılan: 'all')
        status:    'DRAFT'|'IN_TRANSIT'|'ACCEPTED'|... | '' (tümü)
    """
    user_store = request.user.store
    if user_store is None:
        return JsonResponse({'draw': 1, 'recordsTotal': 0, 'recordsFiltered': 0, 'data': []})

    direction = (request.GET.get('direction') or 'all').lower()
    status = (request.GET.get('status') or '').strip().upper()

    qs = (
        StoreTransfer.objects
        .select_related('source_store', 'destination_store',
                        'initiated_by', 'accepted_by', 'rejected_by')
        .all()
    )
    if direction == 'outbound':
        qs = qs.filter(source_store=user_store)
    elif direction == 'inbound':
        qs = qs.filter(destination_store=user_store)
    else:
        qs = qs.filter(Q(source_store=user_store) | Q(destination_store=user_store))

    if status:
        qs = qs.filter(status=status)

    qs = qs.order_by('-created_on')

    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 25))
    if length < 0:
        length = 1000  # "Tümü" durumunda makul bir tavan

    total_count = qs.count()
    rows = qs[start:start + length]

    data = [_serialize_transfer(t) for t in rows]

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total_count,
        'recordsFiltered': total_count,
        'data': data,
    })


# ============================================================================
# 3) MODAL HELPER ENDPOINT'LER
# ============================================================================

@login_required
@role_required('STORE_TRANSFERS_INDEX')
@require_GET
def transfer_destination_stores(request):
    """Yeni transfer modalında 'hedef şube' dropdown'ı için aktif şubeler.

    Kullanıcının kendi şubesi hariç, gelen-transfer kabul eden tüm aktif
    şubeleri döner. FAZ 48'de UserStoreAccess izole edilebilir.
    """
    user_store = request.user.store
    qs = (
        Stores.objects
        .filter(
            is_active=True,
            is_deleted=False,
            is_branch_active=True,
            allows_inbound_transfer=True,
        )
        .exclude(id=user_store.id if user_store else None)
        .order_by('-branch_type', 'title')
    )
    data = [
        {
            'id': str(s.id),
            'title': s.title or s.store_id or '-',
            'branch_code': s.branch_code or '',
            'branch_type': s.branch_type,
        }
        for s in qs
    ]
    return JsonResponse({'result': True, 'stores': data})


@login_required
@role_required('STORE_TRANSFERS_INDEX')
@require_GET
def transfer_source_accounts(request):
    """Yeni transfer modalında 'kaynak kasa' dropdown'ı için kaynak şubedeki
    aktif (transit olmayan) hesapları döner.

    Query: currency=TRY (opsiyonel filtre)
    """
    user_store = request.user.store
    if user_store is None:
        return JsonResponse({'result': True, 'accounts': []})

    currency = (request.GET.get('currency') or '').upper().strip()
    qs = BankAccount.objects.filter(
        store=user_store,
        is_active=True,
        is_deleted=False,
        is_inter_branch_transit_account=False,
    ).order_by('account_type', 'name')
    if currency:
        qs = qs.filter(currency=currency)

    data = []
    for ba in qs:
        try:
            balance = ba.get_balance(currency=ba.currency)
        except Exception:
            balance = Decimal('0')
        data.append({
            'id': str(ba.id),
            'name': ba.name,
            'currency': ba.currency,
            'account_type': ba.account_type,
            'balance': str(balance),
        })
    return JsonResponse({'result': True, 'accounts': data})


@login_required
@role_required('STORE_TRANSFERS_SOURCE_PRODUCTS')
@require_GET
def transfer_source_products(request):
    """Yeni transfer modalında 'kaynak ürün' arama/seçim widget'ı için
    kaynak şubedeki stoktaki ürünleri WAC ve mevcut miktar bilgisiyle döner.

    Query params:
        q: arama terimi (name veya barcode partial)
        material_type: GOLD/SILVER/WATCH/DIAMOND filtresi
        limit: maksimum sonuç (varsayılan 50)
    """
    user_store = request.user.store
    if user_store is None:
        return JsonResponse({'result': True, 'products': []})

    q = (request.GET.get('q') or '').strip()
    material_type = (request.GET.get('material_type') or '').strip().upper()
    try:
        limit = max(1, min(int(request.GET.get('limit') or 50), 200))
    except ValueError:
        limit = 50

    # Kaynak şubedeki stoğu olan ürünleri al — JOIN üzerinden
    base = (
        StockSnapshot.objects
        .filter(store=user_store, product__is_deleted=False, product__is_currency=False)
        .select_related('product')
    )
    # Stoğu olmayan ürünleri elemiyoruz çünkü kullanıcı görmek isteyebilir;
    # ama varsayılan olarak en az birini olanları öne çıkaralım.
    if q:
        base = base.filter(Q(product__name__icontains=q) | Q(product__barcode__icontains=q))
    if material_type in ('GOLD', 'SILVER', 'WATCH', 'DIAMOND'):
        base = base.filter(product__material_type=material_type)

    base = base.order_by('-stock_gram', '-stock_pieces', 'product__name')[:limit]

    data = []
    for snap in base:
        p = snap.product
        data.append({
            'product_id': str(p.id),
            'name': p.name or '-',
            'barcode': getattr(p, 'barcode', '') or '',
            'material_type': p.material_type,
            'product_mileage': str(getattr(p, 'product_mileage', '') or ''),
            'gram': str(getattr(p, 'gram', '') or '0'),
            'stock_gram': str(snap.stock_gram or 0),
            'stock_pieces': snap.stock_pieces or 0,
            'wac_hs': str(snap.weighted_avg_cost_hs or 0),
            'wac_tl': str(snap.weighted_avg_cost_eur or 0),
        })
    return JsonResponse({'result': True, 'products': data})


# ============================================================================
# 4) CRUD AKSİYONLARI
# ============================================================================

@login_required
@role_required('TRANSFER_CREATE')
@require_POST
def transfer_create_action(request):
    """Yeni DRAFT transfer oluşturur ve isteğe bağlı olarak hemen yola çıkarır.

    JSON Body:
        {
            "destination_store_id": "<uuid>",
            "notes_sender": "...",
            "expected_arrival_date": "2026-05-10",  // opsiyonel
            "dispatch_now": true,                    // true ise hemen IN_TRANSIT
            "items": [
                {
                    "currency": "TRY",
                    "amount": "5000.00",
                    "amount_eur": "5000.00",
                    "rate": null,
                    "source_bank_account_id": "<uuid>"
                },
                {
                    "currency": "USD",
                    "amount": "200.00",
                    "amount_eur": "6800.00",
                    "rate": "34.00",
                    "source_bank_account_id": "<uuid>"
                }
            ]
        }
    """
    user_store = request.user.store
    if user_store is None:
        return _err("Aktif şube bulunamadı.")

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return _err("Geçersiz istek gövdesi (JSON beklenir).")

    dest_id = body.get('destination_store_id')
    if not dest_id:
        return _err("Hedef şube zorunludur.")
    try:
        dest_store = Stores.objects.get(id=dest_id, is_active=True, is_deleted=False)
    except Stores.DoesNotExist:
        return _err("Hedef şube bulunamadı.")

    items = body.get('items') or []
    if not items:
        return _err("En az bir kalem girilmeli.")

    # Stok kalem varsa ek yetki kontrolü (TRANSFER_STOCK_CREATE)
    # RoleDetail üzerinden doğrudan sorgulanır (apps/roles/utils benzeri yardımcıya
    # bağlı kalmamak için defansif). is_staff veya superuser by-pass'i YOKTUR.
    if any((it.get('item_type') or 'CASH').upper() == 'STOCK' for it in items):
        from apps.roles.models import RoleDetail
        has_stock_perm = (
            RoleDetail.objects
            .filter(
                role=request.user.role,
                permission__code='TRANSFER_STOCK_CREATE',
                status=True,
            )
            .exists()
        )
        if not has_stock_perm:
            return _err(
                "Stok transferi yetkiniz yok. Yöneticinizle iletişime geçin.",
                status=403,
            )

    expected = body.get('expected_arrival_date')
    if expected:
        try:
            expected_date = timezone.datetime.fromisoformat(expected).date()
        except (TypeError, ValueError):
            return _err("expected_arrival_date geçersiz (YYYY-MM-DD bekleniyor).")
    else:
        expected_date = None

    dispatch_now = bool(body.get('dispatch_now'))

    try:
        with transaction.atomic():
            transfer = StoreTransferService.create_draft(
                source_store=user_store,
                destination_store=dest_store,
                items_payload=items,
                initiated_by=request.user,
                notes_sender=body.get('notes_sender') or '',
                expected_arrival_date=expected_date,
            )
            if dispatch_now:
                StoreTransferService.dispatch_transfer(transfer, dispatched_by=request.user)
            transfer.refresh_from_db()
    except TransferError as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("Transfer create hatası")
        return _err(f"Beklenmeyen hata: {exc}")

    return JsonResponse({
        'result': True,
        'transfer': _serialize_transfer(transfer, include_items=True),
    })


@login_required
@role_required('STORE_TRANSFERS_INDEX')
@require_GET
def transfer_detail(request, transfer_id):
    """Transfer detay JSON: kalemler dahil."""
    user_store = request.user.store
    transfer = get_object_or_404(
        StoreTransfer.objects.select_related('source_store', 'destination_store')
                              .prefetch_related('items'),
        id=transfer_id,
    )
    # Yetki: sadece kaynak veya hedef şube personeli görebilir
    if user_store and transfer.source_store_id != user_store.id and transfer.destination_store_id != user_store.id:
        return _err("Bu transferi görme yetkiniz yok.", status=403)

    return JsonResponse({
        'result': True,
        'transfer': _serialize_transfer(transfer, include_items=True),
    })


@login_required
@role_required('TRANSFER_DISPATCH')
@require_POST
def transfer_dispatch_action(request, transfer_id):
    """DRAFT → IN_TRANSIT geçişi (taslak yola çıkar)."""
    transfer = get_object_or_404(StoreTransfer, id=transfer_id)
    if request.user.store_id != transfer.source_store_id:
        return _err("Sadece kaynak şube transferi yola çıkarabilir.", status=403)
    try:
        StoreTransferService.dispatch_transfer(transfer, dispatched_by=request.user)
    except (TransferError, InvalidTransferStateError) as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("Transfer dispatch hatası")
        return _err(f"Beklenmeyen hata: {exc}")

    transfer.refresh_from_db()
    return JsonResponse({
        'result': True,
        'transfer': _serialize_transfer(transfer, include_items=True),
    })


@login_required
@role_required('TRANSFER_ACCEPT')
@require_POST
def transfer_accept_action(request, transfer_id):
    """IN_TRANSIT → ACCEPTED veya PARTIALLY_ACCEPTED.

    JSON Body (tüm alanlar opsiyonel):
        {
            "notes_receiver": "...",
            "partial_decisions": {
                "<item_id>": {"accept": true},
                "<item_id>": {"accept": false, "reason": "..."}
            }
        }
    """
    transfer = get_object_or_404(StoreTransfer, id=transfer_id)
    if request.user.store_id != transfer.destination_store_id:
        return _err("Sadece hedef şube transferi kabul edebilir.", status=403)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        body = {}

    try:
        StoreTransferService.accept_transfer(
            transfer,
            accepted_by=request.user,
            notes_receiver=body.get('notes_receiver') or '',
            partial_decisions=body.get('partial_decisions') or None,
        )
    except (TransferError, InvalidTransferStateError) as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("Transfer accept hatası")
        return _err(f"Beklenmeyen hata: {exc}")

    transfer.refresh_from_db()
    return JsonResponse({
        'result': True,
        'transfer': _serialize_transfer(transfer, include_items=True),
    })


@login_required
@role_required('TRANSFER_REJECT')
@require_POST
def transfer_reject_action(request, transfer_id):
    """IN_TRANSIT → REJECTED (tüm transfer reddedilir, kaynağa iade)."""
    transfer = get_object_or_404(StoreTransfer, id=transfer_id)
    if request.user.store_id != transfer.destination_store_id:
        return _err("Sadece hedef şube transferi reddedebilir.", status=403)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        body = {}

    reason = (body.get('reason') or '').strip()
    if not reason:
        return _err("Red sebebi zorunludur.")

    try:
        StoreTransferService.reject_transfer(
            transfer, rejected_by=request.user, reason=reason,
        )
    except (TransferError, InvalidTransferStateError) as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("Transfer reject hatası")
        return _err(f"Beklenmeyen hata: {exc}")

    transfer.refresh_from_db()
    return JsonResponse({
        'result': True,
        'transfer': _serialize_transfer(transfer, include_items=True),
    })


@login_required
@role_required('TRANSFER_CANCEL')
@require_POST
def transfer_cancel_action(request, transfer_id):
    """DRAFT → CANCELLED (sadece henüz yola çıkmamış taslaklar)."""
    transfer = get_object_or_404(StoreTransfer, id=transfer_id)
    if request.user.store_id != transfer.source_store_id:
        return _err("Sadece kaynak şube taslağı iptal edebilir.", status=403)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        body = {}

    try:
        StoreTransferService.cancel_draft(
            transfer, cancelled_by=request.user, reason=body.get('reason') or '',
        )
    except (TransferError, InvalidTransferStateError) as exc:
        return _err(str(exc))

    transfer.refresh_from_db()
    return JsonResponse({
        'result': True,
        'transfer': _serialize_transfer(transfer, include_items=True),
    })
