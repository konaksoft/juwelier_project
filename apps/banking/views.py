import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from apps.banking.models import BankTransaction, BankAccount, EsurecTenantCredential
from apps.banking.services import (
    ESurecBankingClient,
    CariMatchingService,
    InvoiceAutoService,
    PaymentStatusService,
    EsurecHealthCheckService,
    POSCommissionService,
    ReconciliationService,
)
from apps.banking.models import POSCommissionRate
from apps.customers.models import Customers
from apps.invoices.models import Invoice
from apps.process.models import Payment

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
# Yardımcılar
# ──────────────────────────────────────────────────────

def _get_store(request):
    return getattr(request.user, 'store', None)


def _require_store(request):
    store = _get_store(request)
    if not store:
        return JsonResponse({'result': False, 'msg': 'Kullanıcıya bağlı mağaza bulunamadı.'})
    return None


def _parse_body(request):
    try:
        return json.loads(request.body.decode('utf-8'))
    except Exception:
        return {}


# ──────────────────────────────────────────────────────
# 1. ANA SAYFA
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def banking_index(request):
    return render(request, 'management/banking/index.html', {
        'title': 'Banka Hareketleri',
    })


# ──────────────────────────────────────────────────────
# 2. DATATABLE — TÜM HAREKETLERİ LİSTELE
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def get_all_transactions(request):
    err = _require_store(request)
    if err:
        return JsonResponse({'draw': 0, 'recordsTotal': 0, 'recordsFiltered': 0, 'data': []})

    store = _get_store(request)

    # 1. Parametreleri al
    draw = int(request.GET.get('draw', 0))
    length = int(request.GET.get('length', 25))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    order_col_idx = int(request.GET.get('order[0][column]', 2))
    order_dir = request.GET.get('order[0][dir]', 'desc')

    match_filter = request.GET.get('match_status', '')
    date_filter = request.GET.get('date_filter', 'all')

    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')

    type_filter = request.GET.get('type_filter', 'ALL')
    invoice_filter = request.GET.get('invoice_filter', 'ALL')

    # Sıralama Ayarları
    column_map = {
        0: 'match_status', 1: 'doc_date', 2: 'bank_name',
        3: 'other_name', 4: 'note', 5: 'amount',
    }
    order_col = column_map.get(order_col_idx, 'doc_date')
    if order_dir == 'desc' and not order_col.startswith('-'):
        order_col = f'-{order_col}'

    # Temel Sorgu
    qs = BankTransaction.objects.filter(store=store)
    total_records = qs.count()

    # 2. Filtreleri Uygula
    if match_filter:
        match_values = [v.strip() for v in match_filter.split(',') if v.strip()]
        qs = qs.filter(match_status__in=match_values)

    if type_filter == 'INCOMING':
        qs = qs.filter(plus_minus=BankTransaction.PlusMinus.DEBIT)
    elif type_filter == 'OUTGOING':
        qs = qs.filter(plus_minus=BankTransaction.PlusMinus.CREDIT)

    if invoice_filter == 'INVOICED':
        qs = qs.filter(invoice__isnull=False)
    elif invoice_filter == 'NOT_INVOICED':
        qs = qs.filter(invoice__isnull=True)

    # --- TARİH FİLTRESİ GÜNCELLEME ---
    if start_date and end_date:
        # Manuel tarih seçilmişse öncelik ondadır
        qs = qs.filter(doc_date__date__range=[start_date, end_date])
    elif date_filter != 'all':
        # Manuel tarih yoksa butonlardaki (bugün/bu ay) filtreyi uygula
        today = timezone.now().date()
        if date_filter == 'today':
            qs = qs.filter(doc_date__date=today)
        elif date_filter == 'week':
            qs = qs.filter(doc_date__date__gte=today - timedelta(days=today.weekday()))
        elif date_filter == 'month':
            qs = qs.filter(doc_date__year=today.year, doc_date__month=today.month)
    # --- TARİH FİLTRESİ GÜNCELLEME BİTİŞ ---

    # Arama
    if search_value:
        qs = qs.filter(
            Q(bank_name__icontains=search_value) |
            Q(other_name__icontains=search_value) |
            Q(other_vkn_tckn__icontains=search_value) |
            Q(note__icontains=search_value) |
            Q(doc_no__icontains=search_value) |
            Q(iban__icontains=search_value)
        )

    filtered_count = qs.count()

    qs = qs.select_related('customer', 'invoice').order_by(order_col)
    if length != -1:
        qs = qs[start:start + length]

    data = []
    for txn in qs:
        customer_info = None
        if txn.customer:
            customer_info = {
                'id': str(txn.customer.id),
                'name': f"{txn.customer.first_name} {txn.customer.last_name}",
            }
        invoice_info = None
        if txn.invoice:
            invoice_info = {
                'id': str(txn.invoice.id),
                'invoice_no': txn.invoice.invoice_no,
                'status': txn.invoice.status,
            }
        data.append({
            'id': str(txn.id),
            'api_transaction_id': txn.api_transaction_id,
            'match_status': txn.match_status,
            'match_score': txn.match_score,
            'payment_status': txn.payment_status,
            'doc_date': txn.doc_date.strftime('%d.%m.%Y %H:%M') if txn.doc_date else '-',
            'bank_name': txn.bank_name or '-',
            'iban': txn.iban or '-',
            'other_name': txn.other_name or '-',
            'other_vkn_tckn': txn.other_vkn_tckn or '-',
            'note': txn.note or '-',
            'amount': str(txn.amount),
            'currency_code': txn.currency_code,
            'plus_minus': txn.plus_minus,
            'customer': customer_info,
            'invoice': invoice_info,
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': filtered_count,
        'data': data,
    })


# ──────────────────────────────────────────────────────
# 3. MYSOFT'TAN HAREKETLERİ ÇEK (SYNC)
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def sync_transactions(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)

    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    iban = body.get('iban', '')
    only_new = bool(body.get('only_new', False))

    try:
        client = ESurecBankingClient()
        sync_result = client.fetch_transactions(
            store=store,
            start_date=start_date,
            end_date=end_date,
            iban=iban,
            only_new=only_new,
        )

        auto_result = {'matched': 0, 'skipped': 0}
        if sync_result.get('result') and sync_result.get('count', 0) > 0:
            matcher = CariMatchingService(store=store)
            auto_result = matcher.auto_match_pending()

        return JsonResponse({
            'result': sync_result.get('result', False),
            'msg': sync_result.get('msg', ''),
            'sync_count': sync_result.get('count', 0),
            'auto_matched': auto_result.get('matched', 0),
            'auto_skipped': auto_result.get('skipped', 0),
            'error_type': sync_result.get('error_type', ''),
        })

    except Exception as e:
        log.exception(f"[Banking] sync_transactions hatası: {e}")
        return JsonResponse({
            'result': False,
            'msg': 'Banka hareketleri alınırken beklenmeyen bir hata oluştu. Lütfen daha sonra tekrar deneyin.',
            'error_type': 'connection_error',
        })


# ──────────────────────────────────────────────────────
# 4. OTOMATİK CARİ EŞLEŞTİRME
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def auto_match_transactions(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)

    try:
        matcher = CariMatchingService(store=store)
        result = matcher.auto_match_pending()
        return JsonResponse({
            'result': True,
            'msg': f"{result['matched']} hareket otomatik eşleştirildi, {result['skipped']} atlandı.",
            'matched': result['matched'],
            'skipped': result['skipped'],
        })
    except Exception as e:
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


# ──────────────────────────────────────────────────────
# 5. MANUEL EŞLEŞTİRME
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def match_transaction(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)

    txn_id = body.get('transaction_id')
    customer_id = body.get('customer_id')
    invoice_id = body.get('invoice_id')

    if not customer_id and not invoice_id:
        return JsonResponse({'result': False, 'msg': 'Müşteri veya fatura seçilmedi.'})

    try:
        bank_txn = BankTransaction.objects.get(id=txn_id, store=store)
        messages = []

        with db_transaction.atomic():
            if customer_id:
                customer = Customers.objects.get(id=customer_id, store=store)
                bank_txn.customer = customer
                bank_txn.match_score = 100
                messages.append(f"Cari atandı: {customer.first_name} {customer.last_name}")

            if invoice_id:
                invoice = Invoice.objects.select_for_update().get(id=invoice_id, store=store)
                bank_txn.invoice = invoice

                if not bank_txn.customer and invoice.customer:
                    bank_txn.customer = invoice.customer
                    messages.append("Faturanın müşterisi otomatik atandı.")

                pay_status = PaymentStatusService.compute_for_invoice(invoice)
                bank_txn.payment_status = pay_status

                PaymentStatusService.update_invoice(invoice)
                messages.append(f"Faturaya bağlandı (Ödeme: {pay_status}).")

            bank_txn.match_status = BankTransaction.MatchStatus.MANUAL_MATCHED
            bank_txn.save()

        return JsonResponse({'result': True, 'msg': ' '.join(messages)})

    except BankTransaction.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi bulunamadı.'})
    except (Customers.DoesNotExist, Invoice.DoesNotExist):
        return JsonResponse({'result': False, 'msg': 'Hedef kayıt bulunamadı.'})
    except Exception as e:
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


# ──────────────────────────────────────────────────────
# 6. EŞLEŞMEYİ KALDIR
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def unmatch_transaction(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)
    txn_id = body.get('transaction_id')

    try:
        bank_txn = BankTransaction.objects.get(id=txn_id, store=store)

        with db_transaction.atomic():
            invoice = bank_txn.invoice

            bank_txn.customer = None
            bank_txn.invoice = None
            bank_txn.match_status = BankTransaction.MatchStatus.UNMATCHED
            bank_txn.match_score = 0
            bank_txn.payment_status = BankTransaction.PaymentStatus.UNPAID
            bank_txn.save()

            if invoice:
                PaymentStatusService.update_invoice(invoice)

        return JsonResponse({'result': True, 'msg': 'Eşleşme kaldırıldı.'})

    except BankTransaction.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi bulunamadı.'})
    except Exception as e:
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


# ──────────────────────────────────────────────────────
# 7. MYSOFT'TA OKUNDU İŞARETLE
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def mark_as_read(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)
    txn_ids = body.get('transaction_ids', [])

    if not txn_ids:
        return JsonResponse({'result': False, 'msg': 'Kayıt seçilmedi.'})

    try:
        transactions = BankTransaction.objects.filter(id__in=txn_ids, store=store)
        api_ids = [t.api_transaction_id for t in transactions if t.api_transaction_id]

        if not api_ids:
            return JsonResponse({'result': False, 'msg': 'Entegratör ID bulunamadı.'})

        client = ESurecBankingClient()
        result = client.mark_read(api_ids, store)

        if result.get('result'):
            transactions.update(is_read=True)
            return JsonResponse({'result': True, 'msg': f'{len(api_ids)} kayıt okundu olarak işaretlendi.'})

        return JsonResponse({'result': False, 'msg': result.get('msg', 'Hata.')})

    except Exception as e:
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


# ──────────────────────────────────────────────────────
# 8. TEK TIKLA E-FATURA OLUŞTUR
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def create_invoice_from_transaction(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)

    txn_id = body.get('transaction_id')
    vat_rate_raw = body.get('vat_rate', 20)
    item_desc = body.get('item_description', '')
    auto_send = body.get('auto_send_to_gib', True)

    try:
        vat_rate = Decimal(str(vat_rate_raw))
    except (InvalidOperation, ValueError):
        vat_rate = Decimal('20')

    if not txn_id:
        return JsonResponse({'result': False, 'msg': 'transaction_id gerekli.'})

    try:
        bank_txn = BankTransaction.objects.select_related('customer', 'invoice').get(
            id=txn_id, store=store
        )
    except BankTransaction.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi bulunamadı.'})

    # İdempotency
    if bank_txn.invoice_id:
        invoice = bank_txn.invoice
        return JsonResponse({
            'result': True,
            'idempotent': True,
            'invoice_id': str(invoice.id),
            'invoice_no': invoice.invoice_no,
            'invoice_status': invoice.status,
            'msg': f'Bu harekete zaten fatura bağlı: {invoice.invoice_no}',
        })

    # Cari yoksa otomatik eşleştir
    if not bank_txn.customer_id:
        matcher = CariMatchingService(store=store)
        match_result = matcher.find_match(bank_txn)
        if match_result['customer']:
            bank_txn.customer = match_result['customer']
            bank_txn.match_score = match_result['score']
            bank_txn.match_status = BankTransaction.MatchStatus.AUTO_MATCHED
            bank_txn.save(update_fields=['customer', 'match_score', 'match_status', 'updated_on'])
        else:
            return JsonResponse({
                'result': False,
                'msg': 'Cari müşteri bulunamadı. Önce manuel eşleştirme yapın.',
            })

    try:
        service = InvoiceAutoService(store=store, user=request.user)
        result = service.create_and_send(
            bank_txn=bank_txn,
            vat_rate=vat_rate,
            item_description=item_desc,
            auto_send_to_gib=auto_send,
        )
        return JsonResponse(result)
    except Exception as e:
        log.exception(f"[Banking] create_invoice_from_transaction hatası: {e}")
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


# ──────────────────────────────────────────────────────
# 9. CARİ ÖNERİ (AJAX)
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def suggest_customer_for_transaction(request):
    txn_id = request.GET.get('transaction_id')
    if not txn_id:
        return JsonResponse({'result': False, 'msg': 'transaction_id gerekli.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)

    try:
        bank_txn = BankTransaction.objects.get(id=txn_id, store=store)
        matcher = CariMatchingService(store=store)
        result = matcher.find_match(bank_txn)

        customer = result.get('customer')
        return JsonResponse({
            'result': True,
            'has_suggestion': customer is not None,
            'score': result.get('score', 0),
            'method': result.get('method', 'none'),
            'details': result.get('details', ''),
            'customer': {
                'id': str(customer.id),
                'name': f"{customer.first_name} {customer.last_name}",
                'identification_number': customer.identification_number or '',
                'phone': customer.phone or '',
            } if customer else None,
        })

    except BankTransaction.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi bulunamadı.'})
    except Exception as e:
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


# ──────────────────────────────────────────────────────
# 10. BANKA HESAPLARI — CRUD
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def get_bank_accounts(request):
    """
    Banka hesaplarını JSON olarak döndürür.

    GET parametreleri (opsiyonel):
        account_type:    'POS' | 'BANK' | 'CASH' — Sadece bu tipe ait hesapları filtreler.
                         Verilmezse tüm aktif hesaplar döner.
        active_only:     'true' (default) | 'false' — Pasif hesapları da dahil eder.
        currency:        'TRY' | 'USD' | 'EUR' vb. — Para birimine göre filtreler.
        include_balance: 'true' | 'false' (default) — True ise Payment tablosundan
                         hesaplanan güncel bakiyeyi (balance) response'a ekler.

    Dönen JSON:
        { "result": true, "data": [{"id": "...", "name": "...", "bank_name": "...",
          "iban": "...", "currency": "...", "is_active": true, "account_type": "POS",
          "balance": 40237.19}] }
    """
    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)

    # --- FAZ 22: include_balance parametresi ---
    include_balance = request.GET.get('include_balance', 'false').lower() == 'true'

    if include_balance:
        # FAZ 22 - Circular Import Hatasını Önlemek İçin Local Import:
        from apps.banking.bank_views import get_bank_balance_qs
        qs = get_bank_balance_qs(store)
    else:
        qs = BankAccount.objects.filter(store=store, is_deleted=False)

    # --- Faz 2: account_type filtresi ---
    account_type = (request.GET.get('account_type') or '').strip().upper()
    valid_types = {c[0] for c in BankAccount.AccountType.choices}
    if account_type and account_type in valid_types:
        qs = qs.filter(account_type=account_type)

    # --- FAZ 18.3: currency filtresi (çapraz döviz güvenlik duvarı) ---
    currency_filter = (request.GET.get('currency') or '').strip().upper()
    if currency_filter:
        qs = qs.filter(currency=currency_filter)

    # --- active_only filtresi (varsayılan: True) ---
    active_only = request.GET.get('active_only', 'true').lower()
    if active_only != 'false':
        qs = qs.filter(is_active=True)

    fields = [
        'id', 'name', 'bank_name', 'iban', 'currency', 'is_active',
        'account_type', 'reconciliation_tolerance',
    ]
    if include_balance:
        fields += ['total_in', 'total_out']

    accounts = list(qs.values(*fields))

    # --- FAZ 22: Bakiye hesaplama (include_balance=true ise) ---
    if include_balance:
        for acc in accounts:
            total_in = acc.pop('total_in', None) or Decimal('0')
            total_out = acc.pop('total_out', None) or Decimal('0')
            acc['balance'] = float(total_in - total_out)

    return JsonResponse({'result': True, 'data': accounts})


# ──────────────────────────────────────────────────────
# FAZ 23: FX BREAKDOWN (Merkez Döviz Kasası — Döviz bazlı bakiye)
# ──────────────────────────────────────────────────────
@login_required(login_url='login')
def get_fx_breakdown(request):
    """
    Merkez Döviz Kasası (currency='FX') için döviz bazlı bakiye kırılımını döndürür.

    GET Parametreleri:
        account_id : uuid — BankAccount ID (zorunlu)

    Dönen JSON:
        {
            "result": true,
            "data": [
                {"currency": "USD", "balance": 1250.00},
                {"currency": "EUR", "balance": 830.50}
            ]
        }
    """
    from apps.banking.bank_views import _get_fx_breakdown

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    account_id = request.GET.get('account_id', '').strip()

    if not account_id:
        return JsonResponse({'result': False, 'msg': 'account_id parametresi zorunludur.'})

    try:
        account = BankAccount.objects.get(id=account_id, store=store, is_deleted=False)
    except BankAccount.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Hesap bulunamadı.'})

    if account.currency != 'FX':
        return JsonResponse({'result': False, 'msg': 'Bu hesap FX tipinde değil.'})

    breakdown = _get_fx_breakdown(account)
    result = []
    if breakdown:
        for cur_code, balance_str in breakdown.items():
            bal = float(Decimal(balance_str))
            if abs(bal) > 0.001:
                result.append({'currency': cur_code, 'balance': bal})

    return JsonResponse({'result': True, 'data': result})


@login_required(login_url='login')
def save_bank_account(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)

    record_id = body.get('id')
    name = (body.get('name') or '').strip()
    bank_name = (body.get('bank_name') or '').strip()
    iban = (body.get('iban') or '').strip()
    currency = body.get('currency', 'TRY')
    account_type = (body.get('account_type') or '').strip().upper()

    if not name:
        return JsonResponse({'result': False, 'msg': 'Hesap adı zorunludur.'})

    # account_type validasyonu
    valid_types = {c[0] for c in BankAccount.AccountType.choices}
    if account_type and account_type not in valid_types:
        return JsonResponse({'result': False, 'msg': f'Geçersiz hesap tipi: {account_type}'})

    if record_id:
        acc = BankAccount.objects.filter(id=record_id, store=store, is_deleted=False).first()
        if not acc:
            return JsonResponse({'result': False, 'msg': 'Hesap bulunamadı.'})
        msg = 'Banka hesabı güncellendi.'
    else:
        acc = BankAccount(store=store)
        msg = 'Banka hesabı eklendi.'

    acc.name = name
    acc.bank_name = bank_name
    acc.iban = iban
    acc.currency = currency
    if account_type:
        acc.account_type = account_type

    # --- Faz 4: reconciliation_tolerance ve is_active ---
    tolerance = body.get('reconciliation_tolerance')
    if tolerance is not None:
        try:
            acc.reconciliation_tolerance = Decimal(str(tolerance))
        except Exception:
            pass

    is_active = body.get('is_active')
    if is_active is not None:
        acc.is_active = bool(is_active)

    acc.save()

    return JsonResponse({'result': True, 'msg': msg, 'id': str(acc.id)})


@login_required(login_url='login')
def delete_bank_account(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)
    account_id = body.get('id')

    updated = BankAccount.objects.filter(id=account_id, store=store).update(is_deleted=True)
    if updated:
        return JsonResponse({'result': True, 'msg': 'Hesap silindi.'})
    return JsonResponse({'result': False, 'msg': 'Hesap bulunamadı.'})


# ──────────────────────────────────────────────────────
# 11. e-SÜREÇ ENTEGRASYON AYARLARI (FAZ 2)
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
def integration_settings(request):
    """
    Mağaza yetkilisinin e-Süreç Tenant Token'ını girebileceği,
    kaydedebileceği ve bağlantıyı test edebileceği ayar sayfası.

    GET  → Ayar sayfasını render eder.
    POST → JSON action bazlı işlem yapar:
        action='save_token'      → Token kaydet/güncelle
        action='test_connection' → e-Süreç'e canlı S2S health check atar
        action='deactivate'      → Entegrasyonu devre dışı bırak
    """
    store = _get_store(request)
    if not store:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'result': False, 'msg': 'Kullanıcıya bağlı mağaza bulunamadı.'})
        return render(request, 'management/banking/settings.html', {
            'title': 'e-Süreç Entegrasyon Ayarları',
            'error_msg': 'Kullanıcıya bağlı mağaza bulunamadı.',
        })

    # ── Yetki kontrolü: Superuser veya mağaza sahibi ─────────────
    if not request.user.is_superuser:
        user_store = _get_store(request)
        if not user_store or str(user_store.id) != str(store.id):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'result': False, 'msg': 'Bu işlem için yetkiniz yok.'}, status=403)
            return render(request, 'management/banking/settings.html', {
                'title': 'e-Süreç Entegrasyon Ayarları',
                'error_msg': 'Bu işlem için yetkiniz yok.',
            })

    # ── Credential al veya oluştur ────────────────────────────────
    cred, created = EsurecTenantCredential.objects.get_or_create(
        store=store,
        defaults={'is_active': False},
    )

    # ── POST: AJAX action'ları ────────────────────────────────────
    if request.method == 'POST':
        body = _parse_body(request)
        action = body.get('action', '')

        if action == 'save_token':
            raw_token = (body.get('tenant_token') or '').strip()
            if not raw_token:
                return JsonResponse({'result': False, 'msg': 'Token boş olamaz.'})

            cred.tenant_token = raw_token
            cred.is_active = True
            # Token süresi: 90 gün
            from datetime import timedelta
            cred.token_expires_at = timezone.now() + timedelta(days=90)
            # Health check'i sıfırla (yeni token ile taze sorgu gerekir)
            cred.last_health_status = EsurecTenantCredential.HealthStatus.UNKNOWN
            cred.last_health_check_at = None
            cred.save(update_fields=[
                'tenant_token_enc', 'is_active', 'token_expires_at',
                'last_health_status', 'last_health_check_at', 'updated_at',
            ])
            return JsonResponse({
                'result': True,
                'msg': 'Token başarıyla kaydedildi. Bağlantıyı test etmeniz önerilir.',
            })

        elif action == 'test_connection':
            service = EsurecHealthCheckService(store)
            result = service.check(force=True)
            return JsonResponse(result)

        elif action == 'save_invoice_defaults':
            # Açık Bankacılık Toplu Fatura için varsayılan ayarları kaydet
            from apps.invoices.models import StoreEInvoiceSettings

            # Superuser/staff, body'den store_id göndererek başka bir mağazanın
            # ayarını yönetebilir (ör. stores/store_detail admin sayfasından).
            # Diğer kullanıcılar için daima request.user.store kullanılır.
            target_store = store
            override_store_id = body.get('store_id')
            if override_store_id and (request.user.is_superuser or request.user.is_staff):
                try:
                    from apps.stores.models import Stores
                    target_store = Stores.objects.get(id=override_store_id, is_deleted=False)
                except (Stores.DoesNotExist, ValueError, TypeError):
                    return JsonResponse({
                        'result': False,
                        'msg': 'Geçersiz mağaza kimliği.',
                    }, status=400)

            product_name = (body.get('default_invoice_product_name') or '').strip()
            karat_raw = body.get('default_invoice_karat')
            labor_type = (body.get('default_invoice_labor_type') or '').strip().upper()
            labor_value_raw = body.get('default_invoice_labor_value')

            # Validasyonlar
            if not product_name:
                return JsonResponse({
                    'result': False,
                    'msg': 'Ürün adı boş olamaz.',
                }, status=400)
            if len(product_name) > 150:
                return JsonResponse({
                    'result': False,
                    'msg': 'Ürün adı en fazla 150 karakter olabilir.',
                }, status=400)

            valid_karats = {24, 22, 18, 14, 8}
            try:
                karat = int(karat_raw)
            except (TypeError, ValueError):
                return JsonResponse({
                    'result': False,
                    'msg': 'Geçersiz ayar değeri. 24, 22, 18, 14 veya 8 seçin.',
                }, status=400)
            if karat not in valid_karats:
                return JsonResponse({
                    'result': False,
                    'msg': 'Geçersiz ayar değeri. 24, 22, 18, 14 veya 8 seçin.',
                }, status=400)

            valid_labor_types = {
                StoreEInvoiceSettings.LaborType.AMOUNT,
                StoreEInvoiceSettings.LaborType.PERCENT,
            }
            if labor_type not in valid_labor_types:
                return JsonResponse({
                    'result': False,
                    'msg': 'Geçersiz işçilik tipi. AMOUNT veya PERCENT olmalı.',
                }, status=400)

            try:
                labor_value = Decimal(str(labor_value_raw or '0'))
            except (InvalidOperation, TypeError, ValueError):
                return JsonResponse({
                    'result': False,
                    'msg': 'Geçersiz işçilik değeri. Sayısal bir değer girin.',
                }, status=400)
            if labor_value < 0:
                return JsonResponse({
                    'result': False,
                    'msg': 'İşçilik değeri negatif olamaz.',
                }, status=400)
            if labor_type == StoreEInvoiceSettings.LaborType.PERCENT and labor_value > Decimal('100'):
                return JsonResponse({
                    'result': False,
                    'msg': 'Yüzde tipi için işçilik değeri 100\'den büyük olamaz.',
                }, status=400)

            # Kaydet (get_or_create — canlıda kayıt yoksa oluştur)
            settings_obj, _created = StoreEInvoiceSettings.objects.get_or_create(store=target_store)
            settings_obj.default_invoice_product_name = product_name
            settings_obj.default_invoice_karat = karat
            settings_obj.default_invoice_labor_type = labor_type
            settings_obj.default_invoice_labor_value = labor_value
            settings_obj.save(update_fields=[
                'default_invoice_product_name',
                'default_invoice_karat',
                'default_invoice_labor_type',
                'default_invoice_labor_value',
                'updated_at',
            ])

            return JsonResponse({
                'result': True,
                'msg': 'Açık Bankacılık Fatura Ayarları başarıyla kaydedildi.',
                'settings': {
                    'default_invoice_product_name': settings_obj.default_invoice_product_name,
                    'default_invoice_karat': settings_obj.default_invoice_karat,
                    'default_invoice_labor_type': settings_obj.default_invoice_labor_type,
                    'default_invoice_labor_value': str(settings_obj.default_invoice_labor_value),
                },
            })

        elif action == 'deactivate':
            cred.is_active = False
            cred.save(update_fields=['is_active', 'updated_at'])
            return JsonResponse({
                'result': True,
                'msg': 'e-Süreç entegrasyonu devre dışı bırakıldı.',
            })

        return JsonResponse({'result': False, 'msg': 'Geçersiz action parametresi.'})

    # ── GET: Ayar sayfasını render et ─────────────────────────────
    # Açık Bankacılık Toplu Fatura varsayılan ayarlarını da context'e geç
    from apps.invoices.models import StoreEInvoiceSettings as _SES
    einvoice_settings, _ = _SES.objects.get_or_create(store=store)

    return render(request, 'management/banking/settings.html', {
        'title': 'e-Süreç Entegrasyon Ayarları',
        'credential': cred,
        'has_token': bool(cred.tenant_token_enc),
        'token_valid': cred.is_token_valid,
        'token_expires_soon': cred.token_expires_soon,
        'health_stale': cred.health_check_stale,
        'einvoice_settings': einvoice_settings,
        'labor_type_choices': _SES.LaborType.choices,
        'karat_choices': _SES.Karat.choices,
    })


# ============================================================================
# POS KOMİSYON YÖNETİMİ — FAZ 4
# ============================================================================


@login_required(login_url='login')
def get_commission_rates(request):
    """
    Belirli bir POS hesabının komisyon oranlarını döndürür.

    GET parametreleri:
        bank_account_id (zorunlu): POS hesabının UUID'si

    Dönen JSON:
        {"result": true, "data": [{"id": "...", "card_type": "GENERIC", ...}]}
    """
    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    ba_id = request.GET.get('bank_account_id', '').strip()
    if not ba_id:
        return JsonResponse({'result': False, 'msg': 'bank_account_id gereklidir.'})

    # Hesabın bu mağazaya ait olduğunu doğrula
    ba = BankAccount.objects.filter(id=ba_id, store=store, is_deleted=False).first()
    if not ba:
        return JsonResponse({'result': False, 'msg': 'Hesap bulunamadı.'})

    rates = POSCommissionRate.objects.filter(
        bank_account=ba, is_active=True
    ).order_by('card_type', 'installment_count').values(
        'id', 'card_type', 'installment_count', 'commission_rate', 'maturity_days'
    )
    return JsonResponse({'result': True, 'data': list(rates)})


@login_required(login_url='login')
def save_commission_rate(request):
    """
    Komisyon oranı oluşturur veya günceller.

    POST Body:
        {bank_account_id, card_type, installment_count, commission_rate, maturity_days}
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)

    ba_id = (body.get('bank_account_id') or '').strip()
    ba = BankAccount.objects.filter(id=ba_id, store=store, is_deleted=False, account_type='POS').first()
    if not ba:
        return JsonResponse({'result': False, 'msg': 'POS hesabı bulunamadı.'})

    card_type = (body.get('card_type') or 'GENERIC').strip().upper()
    installment_count = int(body.get('installment_count', 1) or 1)
    commission_rate = body.get('commission_rate')
    maturity_days = int(body.get('maturity_days', 1) or 1)

    if commission_rate is None:
        return JsonResponse({'result': False, 'msg': 'Komisyon oranı gereklidir.'})

    try:
        svc = POSCommissionService()
        obj, created = svc.save_rate(
            bank_account=ba,
            card_type=card_type,
            installment_count=installment_count,
            commission_rate=commission_rate,
            maturity_days=maturity_days,
        )
        msg = 'Komisyon oranı eklendi.' if created else 'Komisyon oranı güncellendi.'
        return JsonResponse({'result': True, 'msg': msg, 'id': str(obj.id)})
    except Exception as e:
        return JsonResponse({'result': False, 'msg': str(e)})


@login_required(login_url='login')
def delete_commission_rate(request):
    """Komisyon oranını soft-delete yapar."""
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    body = _parse_body(request)
    rate_id = (body.get('id') or '').strip()
    if not rate_id:
        return JsonResponse({'result': False, 'msg': 'Oran ID gereklidir.'})

    svc = POSCommissionService()
    deleted = svc.delete_rate(rate_id)
    if deleted:
        return JsonResponse({'result': True, 'msg': 'Komisyon oranı silindi.'})
    return JsonResponse({'result': False, 'msg': 'Oran bulunamadı.'})


@login_required(login_url='login')
def calculate_commission_preview(request):
    """
    Ödeme modalında anlık komisyon hesaplama (AJAX).

    GET parametreleri:
        bank_account_id (zorunlu): POS hesabı UUID
        amount (zorunlu): Brüt tutar
        installment_count: Taksit sayısı (varsayılan: 1)
        card_type: Kart tipi (varsayılan: GENERIC)

    Dönen JSON:
        {"result": true, "rate": "2.49", "commission": "24.90",
         "net_amount": "975.10", "maturity_date": "2026-04-27", "source": "exact"}
    """
    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    ba_id = request.GET.get('bank_account_id', '').strip()
    amount_str = request.GET.get('amount', '0').strip()
    installment = int(request.GET.get('installment_count', '1') or 1)
    card_type = request.GET.get('card_type', 'GENERIC').strip().upper()

    if not ba_id:
        return JsonResponse({'result': False, 'msg': 'bank_account_id gereklidir.'})

    ba = BankAccount.objects.filter(id=ba_id, store=store, is_deleted=False).first()
    if not ba:
        return JsonResponse({'result': False, 'msg': 'Hesap bulunamadı.'})

    try:
        amount = Decimal(amount_str)
    except Exception:
        return JsonResponse({'result': False, 'msg': 'Geçersiz tutar.'})

    svc = POSCommissionService()
    try:
        result = svc.calculate(
            bank_account=ba,
            amount=amount,
            installment_count=installment,
            card_type=card_type,
        )
    except Exception:
        # Komisyon hesaplanamadiysa %0 ile devam et
        result = {
            'rate': Decimal('0'),
            'commission': Decimal('0'),
            'net_amount': amount,
            'maturity_date': None,
            'maturity_days': 1,
            'source': 'error_fallback',
        }

    return JsonResponse({
        'result': True,
        'rate': str(result['rate']),
        'commission': str(result['commission']),
        'net_amount': str(result['net_amount']),
        'maturity_date': result['maturity_date'].isoformat() if result.get('maturity_date') else None,
        'maturity_days': result.get('maturity_days', 1),
        'source': result.get('source', 'none'),
    })


@login_required(login_url='login')
def commission_report(request):
    """
    Komisyon raporu: DataTables server-side formatında.

    GET parametreleri (DataTables):
        draw, start, length, search[value], order[0][column], order[0][dir]
    Ek filtreler:
        date_from, date_to, bank_account_id, installment_count
    """
    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)

    # DataTables parametreleri
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 25))
    search_val = request.GET.get('search[value]', '').strip()
    order_col = int(request.GET.get('order[0][column]', 0))
    order_dir = request.GET.get('order[0][dir]', 'desc')

    # Filtreler
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    ba_filter = request.GET.get('bank_account_id', '').strip()
    inst_filter = request.GET.get('installment_count', '').strip()

    # Temel QuerySet: komisyon tutarı dolu olan ödemeler
    qs = Payment.objects.filter(
        bank_account__store=store,
        payment_type='CREDIT_CARD',
        commission_amount__isnull=False,
    ).select_related('bank_account')

    # Tarih filtresi
    if date_from:
        qs = qs.filter(date__date__gte=date_from)
    if date_to:
        qs = qs.filter(date__date__lte=date_to)

    # Hesap filtresi
    if ba_filter:
        qs = qs.filter(bank_account_id=ba_filter)

    # Taksit filtresi
    if inst_filter:
        qs = qs.filter(installment=int(inst_filter))

    # Arama
    if search_val:
        from django.db.models import Q
        qs = qs.filter(
            Q(process_no__icontains=search_val) |
            Q(bank_account__name__icontains=search_val) |
            Q(bank_account__bank_name__icontains=search_val)
        )

    total = qs.count()

    # Sıralama
    column_map = {
        0: 'date', 1: 'process_no', 2: 'bank_account__name',
        3: 'amount', 4: 'commission_rate_applied', 5: 'commission_amount',
        6: 'net_amount', 7: 'maturity_date',
    }
    order_field = column_map.get(order_col, 'date')
    if order_dir == 'desc':
        order_field = '-' + order_field
    qs = qs.order_by(order_field)

    # Sayfalama
    page = qs[start:start + length]

    # Özet istatistikler
    from django.db.models import Sum, Avg, Count
    summary_qs = qs.aggregate(
        total_commission=Sum('commission_amount'),
        avg_rate=Avg('commission_rate_applied'),
        total_count=Count('id'),
    )

    data = []
    for p in page:
        data.append({
            'date': p.date.strftime('%d.%m.%Y %H:%M') if p.date else '-',
            'process_no': p.process_no or '-',
            'bank_account': p.bank_account.name if p.bank_account else '-',
            'bank_name': p.bank_account.bank_name if p.bank_account else '-',
            'amount': str(p.amount),
            'commission_rate': str(p.commission_rate_applied) if p.commission_rate_applied else '-',
            'commission_amount': str(p.commission_amount) if p.commission_amount else '-',
            'net_amount': str(p.net_amount) if p.net_amount else '-',
            'installment': p.installment or 1,
            'maturity_date': p.maturity_date.strftime('%d.%m.%Y') if p.maturity_date else '-',
            'reconciliation_status': p.reconciliation_status,
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total,
        'recordsFiltered': total,
        'data': data,
        'summary': {
            'total_commission': str(summary_qs['total_commission'] or 0),
            'avg_rate': str(round(summary_qs['avg_rate'] or 0, 2)),
            'total_count': summary_qs['total_count'] or 0,
        },
    })


# ============================================================================
# MUTABAKAT (Reconciliation) — FAZ 3
# ============================================================================


@login_required(login_url='login')
def reconcile_run_all(request):
    """Mağazanın tüm PENDING ödemelerine toplu mutabakat çalıştırır."""
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)

    try:
        service = ReconciliationService(store=store)
        result = service.reconcile_all_pending()
        return JsonResponse({
            'result': True,
            'msg': (
                f"{result['matched']} eşleşti, "
                f"{result['discrepancy']} uyuşmazlık, "
                f"{result['pending']} bekliyor."
            ),
            'matched': result['matched'],
            'discrepancy': result['discrepancy'],
            'pending': result['pending'],
        })
    except Exception as e:
        log.exception("[Banking] reconcile_run_all hatası: %s", e)
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


@login_required(login_url='login')
def reconcile_manual_match(request):
    """Payment ile BankTransaction arasında manuel mutabakat eşleştirmesi."""
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)
    body = _parse_body(request)

    payment_id = body.get('payment_id')
    bank_transaction_id = body.get('bank_transaction_id')

    if not payment_id or not bank_transaction_id:
        return JsonResponse({'result': False, 'msg': 'payment_id ve bank_transaction_id zorunludur.'})

    try:
        payment = Payment.objects.get(
            id=payment_id,
            bank_account__store=store,
        )
        bank_txn = BankTransaction.objects.get(
            id=bank_transaction_id,
            store=store,
        )

        service = ReconciliationService(store=store)
        result = service.manual_match(payment, bank_txn, user=request.user)
        return JsonResponse(result)

    except Payment.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Ödeme kaydı bulunamadı.'})
    except BankTransaction.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi bulunamadı.'})
    except Exception as e:
        log.exception("[Banking] reconcile_manual_match hatası: %s", e)
        return JsonResponse({'result': False, 'msg': str(e)[:300]})


@login_required(login_url='login')
def reconcile_summary(request):
    """Dashboard için mutabakat durumu sayıları."""
    err = _require_store(request)
    if err:
        return err

    store = _get_store(request)

    from django.db.models import Count

    qs = Payment.objects.filter(
        bank_account__store=store,
        reconciliation_status__in=[
            Payment.ReconciliationStatus.PENDING,
            Payment.ReconciliationStatus.MATCHED,
            Payment.ReconciliationStatus.PARTIAL,
            Payment.ReconciliationStatus.DISCREPANCY,
            Payment.ReconciliationStatus.MANUAL,
        ],
    ).values('reconciliation_status').annotate(count=Count('id'))

    summary = {
        'PENDING': 0,
        'MATCHED': 0,
        'PARTIAL': 0,
        'DISCREPANCY': 0,
        'MANUAL': 0,
    }
    for row in qs:
        summary[row['reconciliation_status']] = row['count']

    return JsonResponse({
        'result': True,
        'data': summary,
        'total': sum(summary.values()),
    })


@login_required(login_url='login')
def reconcile_payments_list(request):
    """DataTables server-side endpoint: Payment kayıtları mutabakat verisiyle."""
    err = _require_store(request)
    if err:
        return JsonResponse({'draw': 0, 'recordsTotal': 0, 'recordsFiltered': 0, 'data': []})

    store = _get_store(request)

    draw = int(request.GET.get('draw', 0))
    length = int(request.GET.get('length', 25))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    order_col_idx = int(request.GET.get('order[0][column]', 0))
    order_dir = request.GET.get('order[0][dir]', 'desc')

    recon_filter = request.GET.get('recon_status', '')

    column_map = {
        0: 'date',
        1: 'bank_account__name',
        2: 'amount',
        3: 'payment_type',
        4: 'reconciliation_status',
        5: 'reconciled_at',
    }
    order_col = column_map.get(order_col_idx, 'date')
    if order_dir == 'desc':
        order_col = f'-{order_col}'

    qs = Payment.objects.filter(
        bank_account__store=store,
        reconciliation_status__in=[
            Payment.ReconciliationStatus.PENDING,
            Payment.ReconciliationStatus.MATCHED,
            Payment.ReconciliationStatus.PARTIAL,
            Payment.ReconciliationStatus.DISCREPANCY,
            Payment.ReconciliationStatus.MANUAL,
        ],
    ).select_related('bank_account', 'matched_bank_transaction', 'reconciled_by')

    total_records = qs.count()

    # ── Durum filtresi ──
    if recon_filter:
        values = [v.strip() for v in recon_filter.split(',') if v.strip()]
        if len(values) == 1:
            qs = qs.filter(reconciliation_status=values[0])
        else:
            qs = qs.filter(reconciliation_status__in=values)

    # ── Arama ──
    if search_value:
        qs = qs.filter(
            Q(bank_account__name__icontains=search_value)
            | Q(bank_account__bank_name__icontains=search_value)
            | Q(process_no__icontains=search_value)
            | Q(reference__icontains=search_value)
        )

    filtered_count = qs.count()

    # ── Sıralama & Sayfalama ──
    qs = qs.order_by(order_col)
    if length != -1:
        qs = qs[start:start + length]

    data = []
    for pmt in qs:
        bank_txn_info = None
        if pmt.matched_bank_transaction:
            bt = pmt.matched_bank_transaction
            bank_txn_info = {
                'id': str(bt.id),
                'amount': str(bt.amount),
                'doc_date': bt.doc_date.strftime('%d.%m.%Y %H:%M') if bt.doc_date else '-',
                'other_name': bt.other_name or '-',
            }

        data.append({
            'id': str(pmt.id),
            'date': pmt.date.strftime('%d.%m.%Y %H:%M') if pmt.date else '-',
            'bank_account_name': pmt.bank_account.name if pmt.bank_account else '-',
            'bank_account_id': str(pmt.bank_account.id) if pmt.bank_account else None,
            'amount': str(pmt.amount),
            'payment_type': pmt.payment_type,
            'payment_type_display': pmt.get_payment_type_display(),
            'reconciliation_status': pmt.reconciliation_status,
            'reconciliation_diff': str(pmt.reconciliation_diff) if pmt.reconciliation_diff is not None else None,
            'reconciled_at': pmt.reconciled_at.strftime('%d.%m.%Y %H:%M') if pmt.reconciled_at else None,
            'reconciled_by': (
                f"{pmt.reconciled_by.first_name} {pmt.reconciled_by.last_name}"
                if pmt.reconciled_by else None
            ),
            'process_no': pmt.process_no or '-',
            'reference': pmt.reference or '-',
            'matched_bank_txn': bank_txn_info,
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': filtered_count,
        'data': data,
    })


# ============================================================================
# SY-01: BANKA HAREKETLERİNDEN TOPLU 24 AYAR ALTIN FATURASI
# ============================================================================

@login_required(login_url='login')
def bulk_invoice_from_bank_transactions(request):
    """
    SY-01: Seçili banka hareketlerinden tek tıkla 24 ayar altın faturası oluşturur.

    POST body: { "transaction_ids": ["uuid1", "uuid2", ...] }

    İş kuralları:
      - Yalnızca DEBIT (gelen para) hareketler faturalanabilir
      - Hareketin eşleştirilmiş müşterisi olmalı
      - Mağaza ayarlarında varsayılan 24 ayar ürün seçili olmalı
      - Her hareket için ayrı bir taslak fatura oluşturulur
      - Fatura kalemi: product=24 ayar, quantity=1, unit_price=havale tutarı, KDV=%0

    Sonra kullanıcı faturayı düzenleyip (gram düzeltme) e-Süreç'e gönderebilir.
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek.'})

    store = _get_store(request)
    if not store:
        return JsonResponse({'result': False, 'msg': 'Mağazaya bağlı kullanıcı bulunamadı.'})

    try:
        body = json.loads(request.body.decode('utf-8'))
        txn_ids = body.get('transaction_ids', [])
    except Exception:
        txn_ids = request.POST.getlist('transaction_ids[]', [])

    if not txn_ids:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi seçilmedi.'})

    # Mağaza ayarından varsayılan 24 ayar ürünü al
    config = getattr(store, 'config', None)
    if not config or not config.default_24k_product:
        return JsonResponse({
            'result': False,
            'msg': 'Mağaza Ayarları > Varsayılan 24 Ayar Altın Ürünü seçili değil. '
                   'Önce bu ayarı yapılandırın.',
        })

    default_product = config.default_24k_product
    from apps.invoices.models import InvoiceItem

    created = []
    skipped = []

    for txn_id in txn_ids:
        try:
            txn = BankTransaction.objects.select_related('customer').get(
                id=txn_id, store=store,
            )

            # Doğrulamalar
            if txn.plus_minus != BankTransaction.PlusMinus.DEBIT:
                skipped.append(f'{txn.doc_no or txn_id}: Giden para, faturalanmaz.')
                continue

            if txn.invoice_id:
                skipped.append(f'{txn.doc_no or txn_id}: Zaten faturası var.')
                continue

            if not txn.customer:
                skipped.append(f'{txn.doc_no or txn_id}: Müşteri eşleştirilmemiş.')
                continue

            # Fatura oluştur
            with db_transaction.atomic():
                invoice_no, seq_no = Invoice.next_number_for(store)

                inv = Invoice.objects.create(
                    store=store,
                    customer=txn.customer,
                    invoice_no=invoice_no,
                    sequence_no=seq_no,
                    issue_date=timezone.now(),
                    invoice_type=Invoice.Type.SALE,
                    doc_class=Invoice.DocumentClass.PROFORMA,
                    status=Invoice.Status.DRAFT,
                    currency='TRY',
                    notes=f'Banka havalesi: {txn.doc_no or ""} ({txn.bank_name or ""})',
                )

                item = InvoiceItem.objects.create(
                    invoice=inv,
                    product=default_product,
                    product_name=default_product.name or '24 Ayar Altın',
                    barcode=getattr(default_product, 'barcode', '') or '',
                    is_gram_bullion=True,
                    quantity=Decimal('1.000'),
                    unit=InvoiceItem.Unit.PIECE,
                    unit_price=Decimal(str(txn.amount)),
                    vat_rate=Decimal('0.00'),
                    exemption_reason='KDVK Madde 17/4-g',
                    notes='KDVK 17/4-g — 24 ayar külçe altın',
                )
                item.recompute(save=True)
                inv.recompute_totals(save=True)

                # Banka hareketini fatura ile eşleştir
                txn.invoice = inv
                txn.payment_status = BankTransaction.PaymentStatus.PAID
                txn.save(update_fields=['invoice', 'payment_status', 'updated_on'])

            created.append({
                'invoice_id': str(inv.id),
                'invoice_no': inv.invoice_no,
                'customer': f'{txn.customer.first_name} {txn.customer.last_name}'.strip(),
                'amount': str(txn.amount),
                'bank_doc_no': txn.doc_no or '',
            })

        except BankTransaction.DoesNotExist:
            skipped.append(f'{txn_id}: Banka hareketi bulunamadı.')
        except Exception as e:
            log.exception(f"[SY-01] Fatura oluşturma hatası (txn={txn_id}): {type(e).__name__}")
            skipped.append(f'{txn_id}: İşlem hatası ({type(e).__name__})')

    msg = f'{len(created)} fatura oluşturuldu.'
    if skipped:
        msg += f' {len(skipped)} hareket atlandı.'

    return JsonResponse({
        'result': len(created) > 0,
        'msg': msg,
        'created': created,
        'skipped': skipped[:10],
        'invoice_ids': [c['invoice_id'] for c in created],
    })


# ============================================================================
# AÇIK BANKACILIK — TOPLU FATURA OTOMASYONU (2026-04)
# Mevcut bulk_invoice_from_bank_transactions view'ı korunur; bu yeni view
# mağaza varsayılan ayarlarını kullanarak MATEMATİKSEL AYRIŞTIRMA + MİLYEM
# + OPSİYONEL GİB gönderimi yapar.
# ============================================================================

@login_required(login_url='login')
def bulk_invoice_with_defaults(request):
    """
    Seçili banka hareketlerini, mağazanın "Açık Bankacılık Fatura Ayarları"
    varsayılan değerleri üzerinden toplu olarak OZELMATRAH tipi taslağa
    dönüştürür.

    POST JSON body:
        {
            "transaction_ids": ["uuid1", "uuid2", ...],
            "send_to_gib": true     // opsiyonel (varsayılan false)
        }

    İş kuralları:
      - Yalnızca DEBIT (gelen) hareketler faturalandırılır.
      - Zaten invoice_id'si olan hareketler atlanır (idempotency).
      - Mağaza ayarları eksikse 422 hatası döner.
      - Her hareket kendi transaction.atomic() içinde işlenir;
        bir hatanın diğerlerini etkilememesi için yalıtılır.
      - send_to_gib=True ise transaction.on_commit() ile Celery görevi tetiklenir.

    Dönüş:
        {
            "result": true,
            "msg": "3 fatura oluşturuldu. 1 atlandı. 2 GİB'e kuyruğa alındı.",
            "created": [...],
            "skipped": [...],
            "failed":  [...],
            "gib_queued": 2
        }
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'msg': 'Geçersiz istek yöntemi.'}, status=405)

    store = _get_store(request)
    if not store:
        return JsonResponse({'result': False, 'msg': 'Kullanıcıya bağlı mağaza bulunamadı.'})

    body = _parse_body(request)
    txn_ids = body.get('transaction_ids') or body.get('bank_transaction_ids') or []
    if not txn_ids:
        try:
            txn_ids = request.POST.getlist('transaction_ids[]') or []
        except Exception:
            txn_ids = []

    if not txn_ids:
        return JsonResponse({'result': False, 'msg': 'Banka hareketi seçilmedi.'})

    send_to_gib = bool(body.get('send_to_gib', False))

    # ── Servis katmanını çağır ──
    try:
        from apps.banking.services_bulk_invoice import BankBulkInvoiceService
        service = BankBulkInvoiceService(store=store)
    except ValueError as ve:
        return JsonResponse({'result': False, 'msg': str(ve)}, status=422)
    except Exception as e:
        log.exception(f"[BulkInvoice] Servis başlatılamadı: {type(e).__name__}")
        return JsonResponse(
            {'result': False, 'msg': 'Servis başlatılamadı. Ayarları kontrol edin.'},
            status=500,
        )

    # ── Ayarların minimum doluluğu ──
    settings = service.settings
    if not (settings.default_invoice_product_name or '').strip():
        return JsonResponse({
            'result': False,
            'msg': 'Varsayılan ürün adı tanımlı değil. '
                   'Banka Ayarları > Açık Bankacılık Fatura Ayarları\'ndan '
                   'varsayılanları yapılandırın.',
        }, status=422)

    # ── Anlık kur kontrolü (sadece uyarı) ──
    if service.has_sale_tl <= 0:
        # Kurumsal hata değil — kullanıcı bilgisel uyarı alır ama devam eder
        log.warning(
            "[BulkInvoice] Mağazanın has satış kuru 0 — gram hesabı yapılamayacak. "
            "store=%s", store.id,
        )

    # ── Toplu işlem ──
    try:
        result = service.build_bulk(txn_ids=[str(t) for t in txn_ids], send_to_gib=send_to_gib)
    except Exception as e:
        log.exception(f"[BulkInvoice] Beklenmeyen hata: {type(e).__name__}")
        return JsonResponse({
            'result': False,
            'msg': f'İşlem sırasında hata oluştu: {type(e).__name__}',
        }, status=500)

    msg_parts = []
    if result.created:
        msg_parts.append(f'{len(result.created)} fatura oluşturuldu')
    if result.skipped:
        msg_parts.append(f'{len(result.skipped)} hareket atlandı')
    if result.failed:
        msg_parts.append(f'{len(result.failed)} hareket hata aldı')
    if send_to_gib and result.gib_queued:
        msg_parts.append(f"{result.gib_queued} fatura GİB'e gönderim kuyruğuna alındı")

    msg = ', '.join(msg_parts) + '.' if msg_parts else 'İşlenecek hareket bulunamadı.'

    return JsonResponse({
        'result': len(result.created) > 0,
        'msg': msg,
        'created': result.created,
        'skipped': result.skipped[:20],
        'failed': result.failed[:20],
        'gib_queued': result.gib_queued,
        'invoice_ids': [c['invoice_id'] for c in result.created],
    })