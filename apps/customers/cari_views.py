"""Cari & Emanet Refactor — View katmanı.

Yeni endpoint'ler (mevcut customers/views.py'deki save_debt_collection
process-bazlı akışı bozulmadan paralel çalışır):

  POST /customers/<uuid>/cari/collect          → tahsilat + kapatma
  POST /customers/<uuid>/cari/custody-offset   → emanet mahsuplaşma
  POST /customers/<uuid>/cari/reverse          → REVERSAL karşı giriş
  POST /customers/<uuid>/cari/approve          → onay bekleyen kayıt onayla
  GET  /customers/<uuid>/cari/balance          → cari + emanet özeti
  GET  /customers/cari/pending-approvals       → tüm onay bekleyenler
  GET  /customers/<uuid>/cari/ledger           → ledger satır listesi
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from django.utils import timezone

from apps.banking.exchange_rate_service import (
    get_current_has_rate, get_current_fx_rate,
)
from apps.banking.models import BankAccount
from apps.customers.models import Customers, CustomerLedger
from apps.custody.models import CustomerCustodyLedger
from apps.customers.services import (
    CollectionService,
    CustodyOffsetService,
    LedgerService,
    ProductPaymentService,
    extract_audit_context,
    approve_entry,
    LedgerError,
)
from apps.activity_logs.views import write_log


# ──────────────────────────────────────────────────────────────────
# YARDIMCILAR
# ──────────────────────────────────────────────────────────────────

def _decimal_or_none(raw, default=None):
    if raw is None or raw == '':
        return default
    try:
        return Decimal(str(raw).replace(',', '.'))
    except (InvalidOperation, ValueError):
        return default


def _get_customer_or_404(request, customer_id):
    """Mağaza izolasyonu ile müşteri çek."""
    store = request.user.store
    return get_object_or_404(
        Customers,
        pk=customer_id,
        store=store,
        is_deleted=False,
    )


def _ledger_error_response(exc: LedgerError):
    return JsonResponse(
        {
            'result': False,
            'error_code': exc.error_code,
            'error_msg': exc.message,
            **(exc.extra or {}),
        },
        status=exc.http_status,
    )


# ──────────────────────────────────────────────────────────────────
# 1) TAHSİLAT + KAPATMA (kur farkı / iskonto destekli)
# ──────────────────────────────────────────────────────────────────

def _truthy(raw) -> bool:
    """Form/JSON'dan gelen bool benzeri değerleri ('true','1','on') True'ya çevir."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ('1', 'true', 'on', 'yes', 'evet')


@login_required
@require_POST
def cari_collect(request, customer_id):
    """Tahsilat ve borç kapatma akışı.

    POST parametreleri:
        bank_account_id     UUID  (zorunlu)
        amount              Decimal (zorunlu, müşterinin verdiği para)
        currency            'TRY'|'USD'|'EUR'|'GBP'|'HS'  (default 'TRY')
        target_close_hs     Decimal (opsiyonel, kapama hedefi HS)
        adjustment_type     'FX_GAIN'|'DISCOUNT'|'WRITEOFF'  (opsiyonel)
        adjustment_reason   string  (adjustment varsa zorunlu)
        allow_overpayment   bool (opsiyonel, FAZ 30) — borçtan fazla
                                tahsilatı kabul et; aşan tutar kasa
                                fazlası olarak yazılır.
    """
    customer = _get_customer_or_404(request, customer_id)
    store = request.user.store

    bank_account_id = request.POST.get('bank_account_id')
    amount = _decimal_or_none(request.POST.get('amount'))
    currency = (request.POST.get('currency') or 'TRY').upper()
    target_close_hs = _decimal_or_none(request.POST.get('target_close_hs'))
    adjustment_type = request.POST.get('adjustment_type') or None
    adjustment_reason = request.POST.get('adjustment_reason') or ''
    allow_overpayment = _truthy(request.POST.get('allow_overpayment'))

    if not bank_account_id:
        return JsonResponse(
            {'result': False, 'error_msg': 'Kasa/banka hesabı seçilmelidir.'},
            status=400,
        )
    if amount is None or amount <= 0:
        return JsonResponse(
            {'result': False, 'error_msg': 'Tutar pozitif olmalıdır.'},
            status=400,
        )

    bank_account = get_object_or_404(
        BankAccount, pk=bank_account_id, store=store,
    )

    audit = extract_audit_context(request)

    try:
        result = CollectionService.collect_and_close(
            customer=customer,
            store=store,
            bank_account=bank_account,
            payment_amount=amount,
            payment_currency=currency,
            target_close_hs=target_close_hs,
            adjustment_type=adjustment_type,
            adjustment_reason=adjustment_reason,
            audit=audit,
            allow_overpayment=allow_overpayment,
        )
    except LedgerError as exc:
        return _ledger_error_response(exc)

    write_log(
        request, 'Cari',
        f'Tahsilat — {customer} — {amount} {currency} '
        f'(adjustment={adjustment_type or "-"}'
        f'{", overpayment=YES" if allow_overpayment else ""})',
    )

    return JsonResponse({
        'result': True,
        'data': result.to_dict(),
    })


# ──────────────────────────────────────────────────────────────────
# 2) EMANET → CARİ MAHSUPLAŞMA
# ──────────────────────────────────────────────────────────────────

@login_required
@require_POST
def cari_custody_offset(request, customer_id):
    """Emanet altın → cari borç mahsuplaşma.

    POST:
        amount_hs     Decimal (zorunlu, mahsuplaşılacak Has miktarı)
        description   string (opsiyonel)
    """
    customer = _get_customer_or_404(request, customer_id)
    store = request.user.store

    amount_hs = _decimal_or_none(request.POST.get('amount_hs'))
    description = request.POST.get('description') or ''

    if amount_hs is None or amount_hs <= 0:
        return JsonResponse(
            {'result': False, 'error_msg': 'Has miktarı pozitif olmalıdır.'},
            status=400,
        )

    audit = extract_audit_context(request)

    try:
        result = CustodyOffsetService.offset_custody_to_ledger(
            customer=customer,
            store=store,
            amount_hs=amount_hs,
            audit=audit,
            description=description,
        )
    except LedgerError as exc:
        return _ledger_error_response(exc)

    write_log(
        request, 'Cari',
        f'Emanet Mahsuplaşma — {customer} — {amount_hs} HS',
    )

    return JsonResponse({
        'result': True,
        'data': {
            'custody_entry_id': str(result.custody_entry.id),
            'ledger_entry_id': str(result.ledger_entry.id),
            'offset_hs': str(result.offset_hs),
            'new_custody_balance_hs': str(result.new_custody_balance_hs),
            'new_ledger_balance_hs': str(result.new_ledger_balance_hs),
        },
    })


# ──────────────────────────────────────────────────────────────────
# 3) REVERSAL (CustomerLedger satır iptali)
# ──────────────────────────────────────────────────────────────────

@login_required
@require_POST
def cari_reverse(request, customer_id):
    """Bir CustomerLedger satırını APPEND-ONLY iptali (REVERSAL).

    POST:
        entry_id   UUID (zorunlu)
        reason     string (zorunlu)
    """
    customer = _get_customer_or_404(request, customer_id)

    entry_id = request.POST.get('entry_id')
    reason = (request.POST.get('reason') or '').strip()

    if not entry_id:
        return JsonResponse(
            {'result': False, 'error_msg': 'Kayıt ID zorunludur.'},
            status=400,
        )
    if not reason:
        return JsonResponse(
            {'result': False, 'error_msg': 'İptal nedeni zorunludur.'},
            status=400,
        )

    original = get_object_or_404(
        CustomerLedger, pk=entry_id, customer=customer,
    )
    audit = extract_audit_context(request)

    try:
        rev = LedgerService.reverse_entry(
            original=original,
            audit=audit,
            reason=reason,
        )
    except LedgerError as exc:
        return _ledger_error_response(exc)

    write_log(
        request, 'Cari',
        f'REVERSAL — {customer} — {original.transaction_type} '
        f'{original.amount_hs} HS',
    )

    return JsonResponse({
        'result': True,
        'data': {
            'reversal_id': str(rev.id),
            'original_id': str(original.id),
            'is_approved': rev.is_approved,
            'requires_approval': rev.requires_approval,
            'new_balance_hs': str(LedgerService.get_open_balance_hs(customer)),
        },
    })


# ──────────────────────────────────────────────────────────────────
# 4) ONAY BEKLEYEN KAYIT ONAYLA
# ──────────────────────────────────────────────────────────────────

@login_required
@require_POST
def cari_approve(request, customer_id):
    """Onay bekleyen bir CustomerLedger satırını onayla.

    POST:
        entry_id   UUID
        note       string (opsiyonel)
    """
    customer = _get_customer_or_404(request, customer_id)
    entry_id = request.POST.get('entry_id')
    note = request.POST.get('note') or ''

    if not entry_id:
        return JsonResponse(
            {'result': False, 'error_msg': 'Kayıt ID zorunludur.'},
            status=400,
        )

    entry = get_object_or_404(
        CustomerLedger, pk=entry_id, customer=customer,
    )
    audit = extract_audit_context(request)

    try:
        approve_entry(entry, actor=request.user, audit=audit, note=note)
    except LedgerError as exc:
        return _ledger_error_response(exc)

    write_log(
        request, 'Cari',
        f'ONAY — {customer} — {entry.transaction_type} {entry.amount_hs} HS',
    )

    return JsonResponse({
        'result': True,
        'data': {
            'entry_id': str(entry.id),
            'approved_at': entry.approved_at.isoformat() if entry.approved_at else None,
            'new_balance_hs': str(LedgerService.get_open_balance_hs(customer)),
        },
    })


# ──────────────────────────────────────────────────────────────────
# 5) BAKİYE ÖZETİ (cari + emanet, çift birim)
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def cari_balance(request, customer_id):
    """Cari + emanet özet bakiyeleri.

    Response:
      ledger_balance_hs       — cari Has bakiye (pozitif=borçlu)
      ledger_balance_eur       — STORED amount_eur toplamı (FAZ 33.3)
      custody_balance_hs      — emanet Has bakiye
      custody_balance_eur      — anlık kurla TL (custody HS-only akış)
      pending_approvals_count — onay bekleyen ledger sayısı
      has_rate                — efektif kur (FAZ 33.4): açık borç varsa
                                stored_tl / stored_hs, yoksa piyasa ALIŞ.
                                Tahsilat modali bu kurla TL↔HS dönüştürür.
      has_rate_market         — anlık piyasa ALIŞ kuru (bilgi/fallback)
    """
    from apps.customers.services.custody_offset import get_custody_balance_hs

    customer = _get_customer_or_404(request, customer_id)
    store = request.user.store

    # FAZ 33.4 — has_rate artık efektif kur. Açık borç SATIŞ kuruyla
    # yazılmışsa (FAZ 33.2), tahsilat ekranı da SATIŞ kuruyla TL↔HS
    # dönüştürmeli ki "Hepsini Tahsil Et" doğru TL doldursun ve sahte
    # overpayment çıkmasın. Bakiye yoksa anlık ALIŞ kuruna düşer.
    has_rate_market = get_current_has_rate(store) or Decimal('0')
    effective_rate = LedgerService.get_effective_rate_tl(customer)
    has_rate = effective_rate if (effective_rate and effective_rate > 0) else has_rate_market

    ledger_hs = LedgerService.get_open_balance_hs(customer)
    ledger_tl = LedgerService.get_open_balance_eur(customer)

    custody_hs = get_custody_balance_hs(customer)
    custody_tl = (custody_hs * has_rate_market).quantize(Decimal('0.01'))

    pending_count = CustomerLedger.objects.filter(
        customer=customer,
        is_active=True,
        requires_approval=True,
        is_approved=False,
    ).count()

    # FAZ 38 — Mağaza tercihleri tahsilat modali için response'a eklenir.
    # Frontend `debt_currency_mode`'a göre TL/HS önceliklendirmesi yapar,
    # `allow_overpayment_default` toggle'ın varsayılan durumunu kontrol eder.
    from apps.settings.models import StoreConfiguration
    _config = (
        StoreConfiguration.objects
        .filter(store=store)
        .only('debt_currency_mode', 'allow_overpayment_default')
        .first()
    )
    debt_mode = (_config.debt_currency_mode if _config else 'HS') or 'HS'
    overpay_default = bool(_config.allow_overpayment_default) if _config else False

    return JsonResponse({
        'result': True,
        'data': {
            'customer_id': str(customer.id),
            'customer_name': str(customer),
            'ledger_balance_hs': str(ledger_hs),
            'ledger_balance_eur': str(ledger_tl),
            'custody_balance_hs': str(custody_hs),
            'custody_balance_eur': str(custody_tl),
            'pending_approvals_count': pending_count,
            'has_rate': str(has_rate),
            'has_rate_market': str(has_rate_market),
            'debt_currency_mode': debt_mode,            # FAZ 38: 'HS' | 'TL'
            'allow_overpayment_default': overpay_default,  # FAZ 38
            'as_of': timezone.now().isoformat(),
        },
    })


# ──────────────────────────────────────────────────────────────────
# 6) LEDGER SATIR LİSTESİ
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def cari_ledger_list(request, customer_id):
    """Cari hareket listesi (ledger satırları).

    Query: ?include_inactive=true → eski is_active=False kayıtları da
    dahil eder (denetim/raporlama için).

    FAZ 14 UI — C1 zenginleştirmeleri:
        - signed_amount_hs / signed_amount_eur: bakiye etkisi (+/-) işaretli
        - signed_direction: 'in' (borç azaltıcı, yeşil) / 'out' (borç
                            artırıcı, kırmızı) / 'neutral'
        - bank_account_name / bank_account_type / bank_account_currency:
          Faz 14 ile gelen kasa bilgisi (related_payment → bank_account)
        - cashbox_entry_id: ilgili CashboxLedger satırının id'si
        - payment_type: ödeme tipi (CASH / CREDIT_CARD / TRANSFER vs.)
    """
    customer = _get_customer_or_404(request, customer_id)
    include_inactive = request.GET.get('include_inactive', 'false').lower() == 'true'

    qs = CustomerLedger.objects.filter(customer=customer)
    if not include_inactive:
        qs = qs.filter(is_active=True)

    qs = qs.select_related(
        'parent',
        'created_by',
        'approved_by',
        'related_payment',
        'related_payment__bank_account',
        'related_custody',
    )[:500]

    # CashboxLedger eşleşmelerini tek sorguda toplayıp sözlüğe alıyoruz —
    # her satır için ayrı sorgu yapmamak için.
    from apps.banking.models import CashboxLedger
    payment_ids = [r.related_payment_id for r in qs if r.related_payment_id]
    cashbox_by_payment = {}
    if payment_ids:
        cb_qs = CashboxLedger.objects.filter(
            related_payment_id__in=payment_ids,
        ).values('id', 'related_payment_id', 'cashbox__name', 'cashbox__account_type')
        for cb in cb_qs:
            cashbox_by_payment[cb['related_payment_id']] = cb

    # Borç yönü haritası (statusFor + signed_direction için ortak):
    #   DEBT_INCREASING → bakiyeyi artırır → 'out' (kırmızı, müşteri borçlu)
    #   DEBT_DECREASING → bakiyeyi azaltır → 'in'  (yeşil, tahsilat/iskonto)
    debt_inc = set(Customers.DEBT_INCREASING_TYPES)
    debt_dec = set(Customers.DEBT_DECREASING_TYPES)

    def _signed(r):
        """Satırın bakiye etkisinin işaretli HS değeri."""
        if r.transaction_type == CustomerLedger.CORRECTION:
            return Decimal(r.amount_hs_signed or 0)
        if r.transaction_type == CustomerLedger.REVERSAL:
            tt = r.reversal_target_type
            if tt in debt_inc:
                return -Decimal(r.amount_hs or 0)
            if tt in debt_dec:
                return Decimal(r.amount_hs or 0)
            return Decimal('0')
        if r.transaction_type in debt_inc:
            return Decimal(r.amount_hs or 0)
        if r.transaction_type in debt_dec:
            return -Decimal(r.amount_hs or 0)
        return Decimal('0')

    def _direction(signed_hs):
        if signed_hs > 0:
            return 'out'   # borç artar → mağaza alacaklı, müşteri çıkış borç
        if signed_hs < 0:
            return 'in'    # borç azalır → tahsilat/kapatma
        return 'neutral'

    rows = []
    for r in qs:
        signed_hs = _signed(r)
        # ════════════════════════════════════════════════════════════
        # FAZ 31 / BUG-1 — TL DISPLAY GÜVENLİK FALLBACK (2026-05-01)
        # ════════════════════════════════════════════════════════════
        # Önceki davranış: amount_hs = 0 ise signed_tl = 0 → kullanıcı
        #                  TL borcu olmasına rağmen tabloda TL olarak 0
        #                  görüyordu. "TL borç HAS olarak görünüyor"
        #                  şikayetinin asıl sebebi.
        # Düzeltme: amount_hs > 0 ise oransal hesap (mevcut davranış);
        #           aksi halde stored amount_eur değerinin signed eşdeğeri.
        # ════════════════════════════════════════════════════════════
        try:
            _amt_hs_dec = Decimal(r.amount_hs or 0)
        except (InvalidOperation, ValueError, TypeError):
            _amt_hs_dec = Decimal('0')
        if _amt_hs_dec != Decimal('0'):
            signed_tl = signed_hs / _amt_hs_dec * Decimal(r.amount_eur or 0)
        else:
            # Fallback: stored amount_eur'yi yön bilgisiyle birlikte göster
            try:
                _amt_tl_dec = Decimal(r.amount_eur or 0)
            except (InvalidOperation, ValueError, TypeError):
                _amt_tl_dec = Decimal('0')
            if signed_hs > 0:
                signed_tl = _amt_tl_dec
            elif signed_hs < 0:
                signed_tl = -_amt_tl_dec
            else:
                signed_tl = Decimal('0')

        # Kasa bilgisi: önce related_payment.bank_account, yoksa
        # CashboxLedger üzerinden bul.
        bank_account_name = ''
        bank_account_type = ''
        bank_account_currency = ''
        payment_type = ''
        if r.related_payment_id and r.related_payment:
            ba = r.related_payment.bank_account
            if ba:
                bank_account_name = ba.name
                bank_account_type = ba.account_type
                bank_account_currency = ba.currency
            payment_type = r.related_payment.payment_type or ''

        cb = cashbox_by_payment.get(r.related_payment_id) if r.related_payment_id else None
        if cb and not bank_account_name:
            bank_account_name = cb.get('cashbox__name') or ''
            bank_account_type = cb.get('cashbox__account_type') or ''
        cashbox_entry_id = str(cb['id']) if cb else None

        rows.append({
            'id': str(r.id),
            'created_on': r.created_on.isoformat() if r.created_on else None,
            'transaction_type': r.transaction_type,
            'amount_hs': str(r.amount_hs),
            'amount_eur': str(r.amount_eur),
            'amount_fx': str(r.amount_fx),
            # FAZ 14 UI — C1: işaretli (signed) tutarlar ve yön bilgisi
            'signed_amount_hs': str(signed_hs),
            'signed_amount_eur': str(signed_tl.quantize(Decimal('0.01'))),
            'signed_direction': _direction(signed_hs),
            'currency': r.currency,
            'exchange_rate_eur': str(r.exchange_rate_eur),
            'fx_to_eur_rate': str(r.fx_to_eur_rate),
            'process_no': r.process_no or '',
            'description': r.description or '',
            'parent_id': str(r.parent_id) if r.parent_id else None,
            'reversal_target_type': r.reversal_target_type or '',
            'requires_approval': r.requires_approval,
            'is_approved': r.is_approved,
            'is_active': r.is_active,
            'approved_by': r.approved_by.username if r.approved_by else None,
            'approved_at': r.approved_at.isoformat() if r.approved_at else None,
            'approval_note': r.approval_note or '',
            'created_by': r.created_by.username if r.created_by else None,
            'ip_address': r.ip_address or '',
            'related_payment_id': (
                str(r.related_payment_id) if r.related_payment_id else None
            ),
            'related_custody_id': (
                str(r.related_custody_id) if r.related_custody_id else None
            ),
            # FAZ 14 UI — C1: kasa bilgisi
            'bank_account_name': bank_account_name,
            'bank_account_type': bank_account_type,
            'bank_account_currency': bank_account_currency,
            'payment_type': payment_type,
            'cashbox_entry_id': cashbox_entry_id,
        })

    return JsonResponse({'result': True, 'data': rows, 'count': len(rows)})


# ──────────────────────────────────────────────────────────────────
# 7) ONAY BEKLEYENLER LİSTESİ (mağaza geneli)
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def cari_pending_approvals(request):
    """Mağaza geneli onay bekleyen CustomerLedger kayıtları."""
    store = request.user.store
    qs = (
        CustomerLedger.objects
        .filter(
            store=store,
            is_active=True,
            requires_approval=True,
            is_approved=False,
        )
        .select_related('customer', 'created_by', 'parent')
        .order_by('-created_on')[:200]
    )

    rows = []
    for r in qs:
        rows.append({
            'id': str(r.id),
            'customer_id': str(r.customer_id),
            'customer_name': str(r.customer),
            'transaction_type': r.transaction_type,
            'amount_hs': str(r.amount_hs),
            'amount_eur': str(r.amount_eur),
            'description': r.description or '',
            'created_on': r.created_on.isoformat() if r.created_on else None,
            'created_by': r.created_by.username if r.created_by else None,
        })
    return JsonResponse({'result': True, 'data': rows, 'count': len(rows)})


# ──────────────────────────────────────────────────────────────────
# 8) BANKA HESABI LİSTESİ (tahsilat modalı için)
# ──────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────
# 8a) STANDALONE CARİ HESAP SAYFASI (HTML render)
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def cari_detail_page(request, customer_id):
    """Standalone cari hesap (yeni) sayfası — tüm cari işlemler merkezi.

    URL: /customers/<uuid>/cari/
    """
    customer = _get_customer_or_404(request, customer_id)
    store = request.user.store
    has_rate = get_current_has_rate(store) or Decimal('0')

    context = {
        'title': f'Cari Hesap — {customer}',
        'customer': customer,
        'has_rate': str(has_rate),
    }
    return render(request, 'management/customers/cari_detail.html', context)


@login_required
@require_GET
def cari_pending_approvals_page(request):
    """Mağaza geneli onay bekleyen ledger satırları sayfası."""
    context = {
        'title': 'Cari Onay Bekleyenler',
    }
    return render(request, 'management/customers/cari_pending_approvals.html', context)


@login_required
@require_GET
def cari_collect_bank_accounts(request):
    """Tahsilat modalında kullanılacak aktif banka hesapları.

    FAZ 37 — Soft-delete edilmiş kasaları (is_deleted=True) dropdown'a
    döndürmemek için ek filtre. Eski filtre yalnızca is_active=True
    kullanıyordu; silinmiş ama is_active=True kalmış kasalar görünüyordu.
    """
    store = request.user.store
    qs = (
        BankAccount.objects
        .filter(store=store, is_active=True, is_deleted=False)
        .order_by('account_type', 'name')
        .values('id', 'name', 'account_type', 'currency', 'bank_name')
    )
    return JsonResponse({
        'result': True,
        'data': [
            {
                'id': str(b['id']),
                'name': b['name'],
                'account_type': b['account_type'],
                'currency': b['currency'],
                'bank_name': b['bank_name'] or '',
            }
            for b in qs
        ],
    })


# ──────────────────────────────────────────────────────────────────
# 8b) ANLIK KUR SERVİSİ (FAZ 22.3 — modal kur kaynağı)
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def cari_fx_rates(request):
    """Modal kur kaynağı — tek seferde tüm anlık kurları döner.

    FAZ 22.3 düzeltmesi: detail.html JS önceden döviz kurlarını almak
    için `cari/preview-close` önizleme endpoint'ini kullanıyordu — bu
    kırılgandı (önizleme servisi kur servisi değil). Bu endpoint kuru
    doğrudan exchange_rate_service'ten okur.

    Response:
      has_rate                — 1 gr Has = X TL
      fx_rates {USD, EUR, GBP} — 1 birim döviz = X TL
    """
    store = request.user.store
    has_rate = get_current_has_rate(store) or Decimal('0')

    fx = {}
    for code in ('USD', 'EUR', 'GBP'):
        rate = get_current_fx_rate(code, store) or Decimal('0')
        fx[code] = str(rate)

    return JsonResponse({
        'result': True,
        'data': {
            'has_rate': str(has_rate),
            'fx_rates': fx,
        },
    })


# ──────────────────────────────────────────────────────────────────
# 9) KUR ÖNİZLEME (tahsilat modalında "fark hesabı" preview)
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def cari_preview_close(request, customer_id):
    """Tahsilat öncesi fark/kapama önizlemesi (yazma yapmadan).

    Query:
      amount        Decimal
      currency      'TRY'|'USD'|'EUR'|'GBP'|'HS'
      target_close_hs (opsiyonel)

    Response:
      collection_hs, collection_tl  — tahsilatın HS/TL karşılığı
      target_close_hs               — kapama hedefi
      adjustment_hs, adjustment_tl  — fark
      open_balance_hs               — mevcut açık borç
      has_rate, fx_rate             — anlık kur
    """
    customer = _get_customer_or_404(request, customer_id)
    store = request.user.store

    amount = _decimal_or_none(request.GET.get('amount'), Decimal('0'))
    currency = (request.GET.get('currency') or 'TRY').upper()
    target_close_hs = _decimal_or_none(request.GET.get('target_close_hs'))

    # ──────────────────────────────────────────────────────────────
    # FAZ 33.3 — Tahsilat preview STORED kur ile çalışır
    # ──────────────────────────────────────────────────────────────
    # has_rate (anlık ALIŞ kuru) yalnız fallback. Açık borç varsa
    # `effective_rate = balance_eur / balance_hs` (borçların yazıldığı
    # andaki ortalama kur) kullanılır → kullanıcı açık borç TL'sini
    # tam girdiğinde sahte overpayment çıkmaz, ledger ile bire bir
    # kapama olur.
    # FX (USD/EUR/GBP) tahsilatlarda fx_rate ayrıca anlık piyasadan
    # okunur (FX → TL); TL → HS dönüşümü efektif kurla yapılır.
    # ──────────────────────────────────────────────────────────────
    has_rate_market = get_current_has_rate(store) or Decimal('0')
    effective_rate = LedgerService.get_effective_rate_tl(customer)
    has_rate = effective_rate if (effective_rate and effective_rate > 0) else has_rate_market
    if has_rate <= 0:
        return JsonResponse(
            {'result': False, 'error_msg': 'Has kuru servisi yanıt vermedi.'},
            status=503,
        )

    fx_rate = Decimal('0')
    if currency == 'TRY':
        collection_tl = amount.quantize(Decimal('0.01'))
        collection_hs = (collection_tl / has_rate).quantize(Decimal('0.001'))
    elif currency == 'HS':
        collection_hs = amount.quantize(Decimal('0.001'))
        collection_tl = (collection_hs * has_rate).quantize(Decimal('0.01'))
    elif currency in ('USD', 'EUR', 'GBP'):
        fx_rate = get_current_fx_rate(currency, store) or Decimal('0')
        if fx_rate <= 0:
            return JsonResponse(
                {'result': False, 'error_msg': f'{currency} kuru bulunamadı.'},
                status=503,
            )
        collection_tl = (amount * fx_rate).quantize(Decimal('0.01'))
        collection_hs = (collection_tl / has_rate).quantize(Decimal('0.001'))
    else:
        return JsonResponse(
            {'result': False, 'error_msg': f'Desteklenmeyen para birimi: {currency}'},
            status=400,
        )

    open_balance_hs = LedgerService.get_open_balance_hs(customer)
    open_balance_eur = LedgerService.get_open_balance_eur(customer)
    if target_close_hs is None:
        target_close_hs = collection_hs
    target_close_hs = target_close_hs.quantize(Decimal('0.001'))

    adjustment_hs = (target_close_hs - collection_hs).quantize(Decimal('0.001'))
    adjustment_tl = (adjustment_hs * has_rate).quantize(Decimal('0.01'))

    # FAZ 30 — Overpayment önizlemesi:
    # Açık borcun tamamı kapanıp üzerine TL kalıyorsa kasa fazlası
    # oluşacak. FAZ 33.3 ile closed_debt_eur artık STORED open_balance_eur
    # → fazlanın yüzdesi gerçek borç TL'si üzerinden hesaplanır.
    overpayment_hs = Decimal('0.000')
    overpayment_tl = Decimal('0.00')
    pct_of_debt = Decimal('0')
    if open_balance_hs > 0 and collection_hs > open_balance_hs:
        overpayment_hs = (collection_hs - open_balance_hs).quantize(Decimal('0.001'))
        # Fazla TL = toplam tahsilat TL − stored open balance TL
        overpayment_tl = (collection_tl - open_balance_eur).quantize(Decimal('0.01'))
        if overpayment_tl < 0:
            overpayment_tl = Decimal('0.00')
        if open_balance_eur > 0:
            pct_of_debt = (
                (overpayment_tl / open_balance_eur) * Decimal('100')
            ).quantize(Decimal('0.01'))

    return JsonResponse({
        'result': True,
        'data': {
            'collection_hs': str(collection_hs),
            'collection_tl': str(collection_tl),
            'target_close_hs': str(target_close_hs),
            'adjustment_hs': str(adjustment_hs),
            'adjustment_tl': str(adjustment_tl),
            'open_balance_hs': str(open_balance_hs),
            'open_balance_eur': str(open_balance_eur),
            'has_rate': str(has_rate),  # efektif kur (varsa) ya da anlık ALIŞ
            'has_rate_market': str(has_rate_market),  # bilgi için anlık ALIŞ
            'fx_rate': str(fx_rate),
            'currency': currency,
            # FAZ 30 — Hızlı Onay Modalı önizleme alanları
            'overpayment_hs': str(overpayment_hs),
            'overpayment_tl': str(overpayment_tl),
            'overpayment_pct_of_debt': str(pct_of_debt),
            'has_overpayment': bool(overpayment_tl > 0),
        },
    })


# ════════════════════════════════════════════════════════════════════
# FAZ 49 — ÜRÜN/HURDA İLE TAHSİLAT VE ÖDEME
# ════════════════════════════════════════════════════════════════════

@login_required
@require_POST
def cari_collect_with_products(request, customer_id):
    """Ürün/Hurda + (opsiyonel) nakit ile tahsilat veya ödeme.

    POST (JSON body veya form):
        direction      'collect' | 'pay'   (zorunlu)
        items          JSON array — kalemler:
                       [{kind:'product'|'scrap'|'bracelet',
                         product_id?, name?, mileage?,
                         gram, pieces, hs_value, tl_value,
                         description?}, ...]
        cash_amount    Decimal (opsiyonel, sadece collect yönünde)
        cash_currency  'TRY'|'USD'|'EUR'|'GBP'|'HS' (cash_amount > 0 ise)
        bank_account_id UUID (cash_amount > 0 ise zorunlu)
        description    string (opsiyonel)
    """
    customer = _get_customer_or_404(request, customer_id)
    store = request.user.store

    # Items: hem JSON body hem multipart form'dan al
    items_raw = request.POST.get('items')
    if not items_raw and request.body:
        try:
            body_data = json.loads(request.body.decode('utf-8') or '{}')
            items_raw = body_data.get('items')
            # body'den tüm parametreleri tek seferde çek (JSON akışı)
            direction = (body_data.get('direction') or 'collect').lower()
            cash_amount = _decimal_or_none(body_data.get('cash_amount'), Decimal('0'))
            cash_currency = (body_data.get('cash_currency') or 'TRY').upper()
            bank_account_id = body_data.get('bank_account_id')
            description = (body_data.get('description') or '').strip()
        except (ValueError, TypeError, json.JSONDecodeError):
            return JsonResponse(
                {'result': False, 'error_msg': 'Geçersiz JSON gövdesi.'},
                status=400,
            )
    else:
        direction = (request.POST.get('direction') or 'collect').lower()
        cash_amount = _decimal_or_none(request.POST.get('cash_amount'), Decimal('0'))
        cash_currency = (request.POST.get('cash_currency') or 'TRY').upper()
        bank_account_id = request.POST.get('bank_account_id')
        description = (request.POST.get('description') or '').strip()

    if direction not in ('collect', 'pay'):
        return JsonResponse(
            {'result': False, 'error_msg': "direction 'collect' veya 'pay' olmalı."},
            status=400,
        )

    # items normalize
    if isinstance(items_raw, str):
        try:
            items = json.loads(items_raw)
        except (ValueError, TypeError):
            return JsonResponse(
                {'result': False, 'error_msg': 'items JSON parse hatası.'},
                status=400,
            )
    else:
        items = items_raw

    if not items or not isinstance(items, list):
        return JsonResponse(
            {'result': False, 'error_msg': 'En az bir kalem gönderin.'},
            status=400,
        )

    bank_account = None
    if direction == 'collect' and cash_amount and cash_amount > 0:
        if not bank_account_id:
            return JsonResponse(
                {'result': False, 'error_msg': 'Nakit kısım için kasa zorunlu.'},
                status=400,
            )
        bank_account = get_object_or_404(
            BankAccount, pk=bank_account_id, store=store, is_deleted=False,
        )

    audit = extract_audit_context(request)

    try:
        if direction == 'collect':
            result = ProductPaymentService.collect_with_products(
                customer=customer, store=store, items=items,
                cash_amount=cash_amount or Decimal('0'),
                cash_currency=cash_currency,
                bank_account=bank_account,
                audit=audit, description=description,
            )
        else:  # 'pay'
            result = ProductPaymentService.pay_with_products(
                customer=customer, store=store, items=items,
                audit=audit, description=description,
            )
    except LedgerError as exc:
        return _ledger_error_response(exc)
    except Exception as ex:
        return JsonResponse(
            {'result': False, 'error_msg': f'İşlem hatası: {ex}'},
            status=400,
        )

    write_log(
        request, 'Cari',
        f"Ürün ile {'Tahsilat' if direction == 'collect' else 'Ödeme'} — "
        f"{customer} — {len(result.items)} kalem, "
        f"{result.total_items_hs} HS"
        + (f" + {result.cash_hs} HS nakit" if result.cash_hs > 0 else ""),
    )

    return JsonResponse({
        'result': True,
        'data': {
            'process_no': result.process_no,
            'direction': result.direction,
            'items': [
                {
                    'kind': it.kind,
                    'product_id': it.product_id,
                    'product_name': it.product_name,
                    'gram': str(it.gram),
                    'pieces': it.pieces,
                    'hs_value': str(it.hs_value),
                    'tl_value': str(it.tl_value),
                    'stock_ledger_id': it.stock_ledger_id,
                }
                for it in result.items
            ],
            'total_items_hs': str(result.total_items_hs),
            'total_items_tl': str(result.total_items_tl),
            'cash_hs': str(result.cash_hs),
            'cash_tl': str(result.cash_tl),
            'customer_ledger_ids': result.customer_ledger_ids,
            'payment_id': result.payment_id,
            'cashbox_ledger_id': result.cashbox_ledger_id,
            'new_balance_hs': str(result.new_balance_hs),
        },
    })


# ──────────────────────────────────────────────────────────────────
# FAZ 51 (R-05) — ÜRÜN/HURDA TAHSİLAT/ÖDEME GERİ ALMA
# ──────────────────────────────────────────────────────────────────

@login_required
@require_POST
def cari_reverse_product_collection(request, customer_id):
    """FAZ 51 (R-05) — Ürün/Hurda ile yapılmış tahsilat veya ödemeyi
    atomik biçimde geri al.

    POST:
        process_no  str — 'PRC-' veya 'PAY-' prefix'li işlem numarası
        reason      str — geri alma nedeni (zorunlu)

    Servis (ProductPaymentService.reverse_collection):
      1) StockLedger PAYMENT_IN/OUT satırlarını cancel_stock_entry
         ile geri sarar (REVERSAL_REASON_MAP eşlemesi otomatik yön
         çevirir).
      2) Bu process_no'ya yazılmış aktif CustomerLedger satırlarını
         (COLLECTION_HS, COLLECTION_TL, DEBT) LedgerService.reverse_entry
         ile reverse eder. Onaylı REVERSAL ise propagate hook:
           - CashboxLedger.REVERSAL yazar (R-01)
           - Payment.is_cancelled=True yapar (R-08)
           - IncomeExpenseLedger.is_reversed flag düşer
         Onay yetersiz aktör için PENDING REVERSAL kalır → manager
         onaylayınca yan etkiler tetiklenir.
      3) Atomik blok — herhangi adım fail → tümü rollback.
    """
    customer = _get_customer_or_404(request, customer_id)

    process_no = (request.POST.get('process_no') or '').strip()
    reason = (request.POST.get('reason') or '').strip()

    if not process_no:
        return JsonResponse(
            {'success': False, 'error': 'process_no zorunludur.'},
            status=400,
        )
    if not reason:
        return JsonResponse(
            {'success': False, 'error': 'Geri alma nedeni zorunludur.'},
            status=400,
        )

    # process_no prefix sınırlaması — bu endpoint yalnız Ürün ile
    # tahsilat/ödeme akışındaki PRC-/PAY- işlemleri için.
    if not (process_no.startswith('PRC-') or process_no.startswith('PAY-')):
        return JsonResponse({
            'success': False,
            'error': (
                'Bu endpoint yalnız PRC- veya PAY- prefix\'li ürün ile '
                'tahsilat/ödeme işlemlerini geri alır. Diğer iptal '
                'akışları için ilgili ekranı kullanın.'
            ),
        }, status=400)

    audit = extract_audit_context(request)
    try:
        result = ProductPaymentService.reverse_collection(
            customer=customer,
            store=request.user.store,
            process_no=process_no,
            audit=audit,
            reason=reason,
        )
    except LedgerError as le:
        return _ledger_error_response(le)
    except Exception as ex:
        return JsonResponse({'success': False, 'error': str(ex)}, status=400)

    write_log(
        request, 'CARI_REVERSE_PRODUCT_COLLECTION',
        f'Müşteri {customer} — process_no={process_no} — '
        f'stok {result.get("stock_reversed")} reverse, '
        f'ledger {result.get("customer_ledger_reversed")} reverse '
        f'({result.get("customer_ledger_skipped")} skip). Neden: {reason}',
    )

    return JsonResponse({
        'success': True,
        'process_no': process_no,
        'data': result,
    })


@login_required
@require_GET
def cari_product_search(request):
    """FAZ 49 — Ürün/Hurda tahsilat modalı için ürün arama endpoint'i.

    Yalnız gramajlı ve döviz olmayan ürünler döner. Saat/Pırlanta hariç
    (kullanıcı kararı: barkodlu kapsam dışı).

    Query:
        q              string — ad arama (icontains)
        category       'all' | 'ziynet' | 'hurda' | 'bilezik' (filtre)
        only_in_stock  'true' | 'false' — sadece stoğu olanlar (default true)
        limit          int — max sonuç (default 30, max 100)
    """
    from apps.products.models import Products
    from apps.stock_management.models import StockSnapshot
    from django.db.models import Q, OuterRef, Subquery, DecimalField as _DF, IntegerField as _IF
    from django.db.models.functions import Coalesce

    store = request.user.store
    q = (request.GET.get('q') or '').strip()
    category = (request.GET.get('category') or 'all').lower()
    only_in_stock = (request.GET.get('only_in_stock', 'true').lower() != 'false')
    try:
        limit = min(int(request.GET.get('limit') or 30), 100)
    except (ValueError, TypeError):
        limit = 30

    base = Products.objects.filter(
        Q(store=store) | Q(store__isnull=True),
        is_active=True, is_deleted=False, is_currency=False,
    ).exclude(
        material_type__in=['WATCH', 'DIAMOND'],
    )

    if category == 'ziynet':
        base = base.exclude(
            Q(category__name__iexact='Hurda') |
            Q(category__name__iexact='Bilezik')
        )
    elif category == 'hurda':
        base = base.filter(category__name__iexact='Hurda')
    elif category == 'bilezik':
        base = base.filter(category__name__iexact='Bilezik')

    if q:
        base = base.filter(name__icontains=q)

    # Stok bilgisi (StockSnapshot OuterRef)
    snap_qs = StockSnapshot.objects.filter(
        product=OuterRef('pk'), store=store,
    )
    base = base.annotate(
        snap_gram=Coalesce(
            Subquery(snap_qs.values('stock_gram')[:1]),
            Decimal('0'), output_field=_DF(max_digits=14, decimal_places=4),
        ),
        snap_pieces=Coalesce(
            Subquery(snap_qs.values('stock_pieces')[:1]),
            0, output_field=_IF(),
        ),
        snap_wac_hs=Coalesce(
            Subquery(snap_qs.values('weighted_avg_cost_hs')[:1]),
            Decimal('0'), output_field=_DF(max_digits=12, decimal_places=4),
        ),
    )

    if only_in_stock:
        base = base.filter(Q(snap_gram__gt=0) | Q(snap_pieces__gt=0))

    base = base.select_related('category').order_by('name')[:limit]

    items = []
    for p in base:
        items.append({
            'id': str(p.id),
            'name': p.name,
            'category': p.category.name if p.category else '-',
            'mileage': int(p.product_mileage or 0),
            'is_gram_bullion': bool(p.is_gram_bullion),
            'gram_unit': str(p.gram or 0),  # adet bazlı için 1 adetin gramı
            'buy_price_hs': str(p.buy_price_hs or 0),
            'sale_price_hs': str(p.sale_price_hs or 0),
            'stock_gram': str(p.snap_gram or 0),
            'stock_pieces': int(p.snap_pieces or 0),
            'wac_hs': str(p.snap_wac_hs or 0),
            'is_scrap': bool(p.is_scrap),
        })

    return JsonResponse({'result': True, 'count': len(items), 'items': items})
