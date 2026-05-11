# ============================================================================
# DOSYA: apps/banking/expense_views.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v1 — FAZ 61 (Hızlı Gider Modülü)
#
# Bu dosya /banks/expense/ prefix'i altında çalışan "Hızlı Gider Girişi"
# modülünü besler. Mevcut bank_views.manual_expense endpoint'i tek satır
# girişi için kalır; bu modül toplu giriş + kategori CRUD + raporlama için
# yeni endpoint'leri sunar.
#
# View'lar:
#   1. expense_quick_index              — Hızlı giriş sayfası (HTML)
#   2. expense_bulk_save                — Toplu (N satır) atomik POST
#   3. expense_categories_index         — Kategori CRUD sayfası (HTML)
#   4. expense_categories_list          — DataTable JSON
#   5. expense_categories_options       — Aktif kategoriler (dropdown JSON)
#   6. expense_categories_save          — Yeni/güncelle (POST)
#   7. expense_categories_toggle        — Aktif/pasif toggle
#   8. expense_categories_delete        — Sil (sistem preseti silinemez)
#   9. expense_report_index             — Gider raporu sayfası (HTML)
#  10. expense_report_data              — Rapor JSON (kategori bazlı agregasyon)
#  11. expense_reverse                  — Tek gider iptali (REVERSAL)
#  12. expense_today_kpi                — FAZ 65: Hızlı giriş üst KPI (read-only JSON)
#
# MİMARİ:
#   - Tüm yazımlar transaction.atomic ile sarılır.
#   - Bulk save: tek atomic blok içinde N satır → ya hepsi ya hiçbiri.
#   - Mevcut Payment / CashboxLedger / IncomeExpenseLedger zinciri
#     dokunulmadan kullanılır (FAZ 31 manuel_expense ile aynı pattern).
#   - Yeni IncomeExpenseLedger.expense_category FK opsiyonel doldurulur.
# ============================================================================

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.banking.models import (
    BankAccount,
    CashboxLedger,
    ExpenseCategory,
    IncomeExpenseLedger,
)
from apps.banking.services import FX_SENTINEL_MAP
from apps.process.models import Payment

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
# YARDIMCILAR
# ──────────────────────────────────────────────────────

def _get_store(request):
    return getattr(request.user, 'store', None)


def _store_or_error(request):
    """Mağaza sağlamazsa JSON hata döner; aksi halde (store, None) döner."""
    store = _get_store(request)
    if not store:
        return None, JsonResponse(
            {'result': False, 'msg': 'Kullanıcıya bağlı mağaza bulunamadı.'},
            status=400,
        )
    return store, None


def _parse_decimal(raw, default=None):
    """Türkçe locale (virgül) desteği ile Decimal parse."""
    if raw is None or raw == '':
        return default
    try:
        return Decimal(str(raw).replace(',', '.').strip())
    except (InvalidOperation, ValueError):
        return None


def _fmt_amount(d):
    """Decimal'i 2 ondalıklı string'e döndürür (yuvarlama: HALF_UP)."""
    if d is None:
        return '0.00'
    return f"{Decimal(d).quantize(Decimal('0.01'))}"


# ════════════════════════════════════════════════════════════════════════════
# 1) HIZLI GİRİŞ SAYFASI (HTML)
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
def expense_quick_index(request):
    """Hızlı gider girişi sayfası. Kategoriler + aktif kasalar context'e gelir.

    UI: tek ekran defter benzeri arayüz; Tab/Enter/Ctrl+Enter klavye
    navigasyonu ile saniyeler içinde N satır girilebilir.
    """
    store = _get_store(request)
    if not store:
        return render(request, 'management/banks/expense_quick.html', {
            'categories': [],
            'accounts': [],
            'no_store': True,
        })

    categories = list(
        ExpenseCategory.objects.filter(store=store, is_active=True)
        .order_by('display_order', 'name')
        .values('id', 'name', 'short_code', 'icon', 'color_css')
    )
    accounts = list(
        BankAccount.objects.filter(
            store=store, is_active=True, is_deleted=False,
            is_inter_branch_transit_account=False,
        )
        .order_by('account_type', 'name')
        .values('id', 'name', 'bank_name', 'account_type', 'currency')
    )

    # UUID'leri string'e çevir (template/JSON serializasyon için)
    for c in categories:
        c['id'] = str(c['id'])
    for a in accounts:
        a['id'] = str(a['id'])

    ctx = {
        'categories_json': json.dumps(categories),
        'accounts_json': json.dumps(accounts),
        'categories': categories,
        'accounts': accounts,
        'no_store': False,
    }
    return render(request, 'management/banks/expense_quick.html', ctx)


# ════════════════════════════════════════════════════════════════════════════
# 2) TOPLU KAYIT (Atomik) — Bulk Save
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
@require_POST
@transaction.atomic
def expense_bulk_save(request):
    """N satırlık gider listesini tek atomic blokta yazar.

    POST body (JSON):
        {
          "entries": [
            {
              "account_id": "<uuid>",
              "category_id": "<uuid|null>",
              "amount": "220.00",
              "description": "Öğle yemeği",
              "currency": "TRY"        // opsiyonel; FX kasalarda zorunlu
            },
            ...
          ]
        }

    Kural: bir satır geçersizse HİÇBİR satır yazılmaz (atomic rollback).
    Yan etkiler her satır için (FAZ 31 patterni):
      • Payment(is_output=True, payment_type='EXPENSE')
      • CashboxLedger.EXPENSE
      • IncomeExpenseLedger.OTHER_EXPENSE (+ expense_category FK)
    """
    store, err = _store_or_error(request)
    if err:
        return err

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(
            {'result': False, 'msg': 'Geçersiz JSON gövdesi.'}, status=400
        )

    entries = body.get('entries') or []
    if not isinstance(entries, list) or len(entries) == 0:
        return JsonResponse(
            {'result': False, 'msg': 'En az bir gider satırı gereklidir.'},
            status=400,
        )

    # Aşırı büyük gönderim koruması (DoS engeli)
    if len(entries) > 200:
        return JsonResponse(
            {'result': False,
             'msg': 'Tek seferde en fazla 200 gider satırı gönderilebilir.'},
            status=400,
        )

    # ──────────────────────────────────────────────────────
    # 1. Pre-Flight: tüm satırları doğrula (yazım yok henüz)
    # ──────────────────────────────────────────────────────
    cleaned_rows = []
    for idx, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            return JsonResponse({
                'result': False,
                'msg': f'Satır {idx}: geçersiz format.',
                'row': idx,
            }, status=400)

        account_id = (raw.get('account_id') or '').strip()
        category_id = (raw.get('category_id') or '').strip() or None
        amount_raw = raw.get('amount')
        description = (raw.get('description') or '').strip()
        currency = (raw.get('currency') or '').strip().upper() or None

        if not account_id:
            return JsonResponse({
                'result': False,
                'msg': f'Satır {idx}: kasa seçilmedi.',
                'row': idx,
            }, status=400)

        amount = _parse_decimal(amount_raw)
        if amount is None or amount <= 0:
            return JsonResponse({
                'result': False,
                'msg': f'Satır {idx}: tutar pozitif olmalıdır.',
                'row': idx,
            }, status=400)

        if not description:
            # Kategori varsa ad yeter — açıklama opsiyonel
            description = ''

        account = BankAccount.objects.filter(
            id=account_id,
            store=store,
            is_deleted=False,
            is_active=True,
            is_inter_branch_transit_account=False,
        ).first()
        if not account:
            return JsonResponse({
                'result': False,
                'msg': f'Satır {idx}: kasa bulunamadı veya aktif değil.',
                'row': idx,
            }, status=400)

        category = None
        if category_id:
            category = ExpenseCategory.objects.filter(
                id=category_id, store=store, is_active=True,
            ).first()
            if not category:
                return JsonResponse({
                    'result': False,
                    'msg': f'Satır {idx}: kategori bulunamadı veya aktif değil.',
                    'row': idx,
                }, status=400)

        # En az kategori veya açıklama dolu olmalı (boş satır engeli)
        if not category and not description:
            return JsonResponse({
                'result': False,
                'msg': f'Satır {idx}: kategori veya açıklama girilmelidir.',
                'row': idx,
            }, status=400)

        cleaned_rows.append({
            'account': account,
            'category': category,
            'amount': amount,
            'description': description,
            'currency': currency,
        })

    # ──────────────────────────────────────────────────────
    # 2. Yazım: her satır için 3 katmanlı atomik yazım
    # ──────────────────────────────────────────────────────
    written_ids = []
    total_amount = Decimal('0')

    for idx, row in enumerate(cleaned_rows, start=1):
        account = row['account']
        category = row['category']
        amount = row['amount']
        description = row['description']
        exp_currency = row['currency']

        # Görüntülenecek açıklama: kategori + serbest metin
        cat_label = category.name if category else 'Kategorisiz'
        full_desc = f'{cat_label}: {description}' if description else cat_label
        full_desc = full_desc[:255]

        acct_currency = getattr(account, 'currency', 'TRY') or 'TRY'

        # Payment ek alanları (FX kasaları için)
        _pay_extra = {}
        if acct_currency == 'FX' and exp_currency and exp_currency != 'TRY':
            _pay_extra['currency_amount'] = amount
            _pay_extra['exchange_rate'] = FX_SENTINEL_MAP.get(
                exp_currency, Decimal('0.09')
            )
        elif acct_currency != 'TRY' and acct_currency != 'FX':
            _pay_extra['currency_amount'] = amount
            _pay_extra['exchange_rate'] = Decimal('1')

        _ref_prefix = (
            f'[{exp_currency}] '
            if (acct_currency == 'FX' and exp_currency)
            else ''
        )
        _ref_text = f'{_ref_prefix}GIDER: {full_desc}'[:100]

        # 1) Payment
        payment = Payment.objects.create(
            process_no=None,
            payment_type='EXPENSE',
            amount=amount,
            is_output=True,
            bank_account=account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=True,
            reference=_ref_text,
            performed_by=request.user,
            notes=full_desc,
            **_pay_extra,
        )

        # 2) CashboxLedger.EXPENSE
        cb_currency_choice = (
            exp_currency if (acct_currency == 'FX' and exp_currency)
            else acct_currency
        )
        if cb_currency_choice not in ('TRY', 'USD', 'EUR', 'GBP', 'HS'):
            cb_currency_choice = 'TRY'

        try:
            prior_balance = account.get_balance(currency=cb_currency_choice)
        except Exception:
            prior_balance = Decimal('0')
        new_balance = (
            Decimal(str(prior_balance)) - amount
        ).quantize(Decimal('0.01'))

        CashboxLedger.objects.create(
            cashbox=account,
            store=store,
            movement_type=CashboxLedger.MovementType.EXPENSE,
            amount=amount.quantize(Decimal('0.01')),
            currency=cb_currency_choice,
            amount_eur_equivalent=amount.quantize(Decimal('0.01')),
            exchange_rate=_pay_extra.get('exchange_rate'),
            balance_snapshot=new_balance,
            related_payment=payment,
            process_no=None,
            description=f'Manuel gider — {full_desc}'[:255],
            created_by=request.user,
            ip_address=request.META.get('REMOTE_ADDR') or None,
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:512],
        )

        # 3) IncomeExpenseLedger.OTHER_EXPENSE
        iel = IncomeExpenseLedger.objects.create(
            store=store,
            entry_type=IncomeExpenseLedger.EntryType.OTHER_EXPENSE,
            amount_eur=amount.quantize(Decimal('0.01')),
            amount_hs=Decimal('0'),
            exchange_rate_eur=Decimal('0'),
            related_payment=payment,
            expense_category=category,
            description=full_desc,
            created_by=request.user,
            ip_address=request.META.get('REMOTE_ADDR') or None,
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:512],
        )

        written_ids.append(str(iel.id))
        total_amount += amount

    log.info(
        "FAZ61 EXPENSE_BULK_SAVE: store=%s rows=%s total=%s user=%s",
        store.id, len(cleaned_rows), total_amount, request.user.username,
    )

    return JsonResponse({
        'result': True,
        'msg': (
            f'{len(cleaned_rows)} gider satırı kaydedildi. '
            f'Toplam: {_fmt_amount(total_amount)} TL'
        ),
        'count': len(cleaned_rows),
        'total_amount': _fmt_amount(total_amount),
        'ids': written_ids,
    })


# ════════════════════════════════════════════════════════════════════════════
# 3) KATEGORİ YÖNETİMİ — Sayfa + CRUD
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
def expense_categories_index(request):
    """Kategori yönetim sayfası (HTML)."""
    return render(request, 'management/banks/expense_categories.html', {})


@login_required(login_url='login')
def expense_categories_list(request):
    """DataTable JSON: mağazanın tüm kategorileri (aktif + pasif)."""
    store, err = _store_or_error(request)
    if err:
        return err

    rows = []
    qs = ExpenseCategory.objects.filter(store=store).order_by(
        'display_order', 'name'
    )
    for c in qs:
        rows.append({
            'id': str(c.id),
            'name': c.name,
            'short_code': c.short_code,
            'icon': c.icon,
            'color_css': c.color_css,
            'display_order': c.display_order,
            'is_active': c.is_active,
            'is_system_preset': c.is_system_preset,
            'created_on': c.created_on.strftime('%d.%m.%Y %H:%M'),
        })
    return JsonResponse({'result': True, 'data': rows})


@login_required(login_url='login')
def expense_categories_options(request):
    """Hızlı giriş dropdown'ı için aktif kategoriler (kısa JSON)."""
    store, err = _store_or_error(request)
    if err:
        return err

    rows = list(
        ExpenseCategory.objects.filter(store=store, is_active=True)
        .order_by('display_order', 'name')
        .values('id', 'name', 'short_code', 'icon', 'color_css')
    )
    for r in rows:
        r['id'] = str(r['id'])
    return JsonResponse({'result': True, 'data': rows})


@login_required(login_url='login')
@require_POST
@transaction.atomic
def expense_categories_save(request):
    """Yeni kategori oluşturur veya mevcut kategoriyi günceller.

    POST:
        id            — UUID; boş ise yeni kategori
        name          — zorunlu
        short_code    — opsiyonel (max 10)
        icon          — opsiyonel (Bootstrap Icons)
        color_css     — opsiyonel
        display_order — opsiyonel (default 100)
    """
    store, err = _store_or_error(request)
    if err:
        return err

    cat_id = (request.POST.get('id') or '').strip() or None
    name = (request.POST.get('name') or '').strip()
    short_code = (request.POST.get('short_code') or '').strip()[:10]
    icon = (request.POST.get('icon') or '').strip()[:30]
    color_css = (request.POST.get('color_css') or '').strip()[:20]
    display_order_raw = (request.POST.get('display_order') or '').strip()

    if not name:
        return JsonResponse(
            {'result': False, 'msg': 'Kategori adı boş olamaz.'}, status=400
        )

    try:
        display_order = int(display_order_raw) if display_order_raw else 100
    except (ValueError, TypeError):
        display_order = 100
    if display_order < 0:
        display_order = 100

    # Yeni mi, güncelleme mi?
    if cat_id:
        cat = ExpenseCategory.objects.filter(id=cat_id, store=store).first()
        if not cat:
            return JsonResponse(
                {'result': False, 'msg': 'Kategori bulunamadı.'}, status=404
            )
        # Sistem preseti adı değiştirilemez (sadece görünüm + display_order)
        if cat.is_system_preset and cat.name != name:
            return JsonResponse({
                'result': False,
                'msg': 'Sistem preseti kategorinin adı değiştirilemez.',
            }, status=400)

        # Aynı isimde başka kategori var mı? (kendisi hariç)
        dup = ExpenseCategory.objects.filter(
            store=store, name=name,
        ).exclude(id=cat.id).exists()
        if dup:
            return JsonResponse({
                'result': False,
                'msg': 'Bu isimde başka bir kategori zaten var.',
            }, status=400)

        cat.name = name
        cat.short_code = short_code
        cat.icon = icon
        cat.color_css = color_css
        cat.display_order = display_order
        cat.save(update_fields=[
            'name', 'short_code', 'icon', 'color_css',
            'display_order', 'updated_on',
        ])
        action = 'updated'
    else:
        # Yeni
        if ExpenseCategory.objects.filter(store=store, name=name).exists():
            return JsonResponse({
                'result': False,
                'msg': 'Bu isimde bir kategori zaten var.',
            }, status=400)

        cat = ExpenseCategory.objects.create(
            store=store,
            name=name,
            short_code=short_code,
            icon=icon,
            color_css=color_css,
            display_order=display_order,
            is_active=True,
            is_system_preset=False,
            created_by=request.user,
        )
        action = 'created'

    log.info(
        "FAZ61 EXPENSE_CATEGORY_%s: id=%s name=%s store=%s user=%s",
        action.upper(), cat.id, cat.name, store.id, request.user.username,
    )

    return JsonResponse({
        'result': True,
        'msg': 'Kategori kaydedildi.',
        'id': str(cat.id),
    })


@login_required(login_url='login')
@require_POST
@transaction.atomic
def expense_categories_toggle(request):
    """Kategoriyi aktif/pasif yapar."""
    store, err = _store_or_error(request)
    if err:
        return err

    cat_id = (request.POST.get('id') or '').strip()
    if not cat_id:
        return JsonResponse(
            {'result': False, 'msg': 'Kategori ID gerekli.'}, status=400
        )

    cat = ExpenseCategory.objects.filter(id=cat_id, store=store).first()
    if not cat:
        return JsonResponse(
            {'result': False, 'msg': 'Kategori bulunamadı.'}, status=404
        )

    cat.is_active = not cat.is_active
    cat.save(update_fields=['is_active', 'updated_on'])

    return JsonResponse({
        'result': True,
        'msg': 'Aktif yapıldı.' if cat.is_active else 'Pasif yapıldı.',
        'is_active': cat.is_active,
    })


@login_required(login_url='login')
@require_POST
@transaction.atomic
def expense_categories_delete(request):
    """Kategoriyi siler. Sistem preseti veya bağlı ledger satırı varsa
    silmek yerine pasif yapılır.

    Kurallar:
        - is_system_preset=True → silme reddedilir, "Sadece pasif yapılabilir."
        - Bağlı IncomeExpenseLedger satırı varsa → silme reddedilir,
          kullanıcı pasif yapmaya yönlendirilir (mevcut raporlamayı korumak için).
        - Aksi halde hard delete.
    """
    store, err = _store_or_error(request)
    if err:
        return err

    cat_id = (request.POST.get('id') or '').strip()
    if not cat_id:
        return JsonResponse(
            {'result': False, 'msg': 'Kategori ID gerekli.'}, status=400
        )

    cat = ExpenseCategory.objects.filter(id=cat_id, store=store).first()
    if not cat:
        return JsonResponse(
            {'result': False, 'msg': 'Kategori bulunamadı.'}, status=404
        )

    if cat.is_system_preset:
        return JsonResponse({
            'result': False,
            'msg': 'Sistem preseti kategoriler silinemez. Pasif yapabilirsiniz.',
        }, status=400)

    has_entries = IncomeExpenseLedger.objects.filter(
        expense_category=cat
    ).exists()
    if has_entries:
        return JsonResponse({
            'result': False,
            'msg': (
                'Bu kategoriye bağlı gider kaydı var. Geçmiş raporları korumak '
                'için silmek yerine pasif yapın.'
            ),
        }, status=400)

    cat.delete()
    return JsonResponse({'result': True, 'msg': 'Kategori silindi.'})


# ════════════════════════════════════════════════════════════════════════════
# 4) RAPOR — Sayfa + JSON
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
def expense_report_index(request):
    """Gider raporu sayfası (HTML)."""
    store = _get_store(request)
    accounts = []
    if store:
        accounts = list(
            BankAccount.objects.filter(
                store=store, is_active=True, is_deleted=False,
                is_inter_branch_transit_account=False,
            )
            .order_by('account_type', 'name')
            .values('id', 'name', 'account_type', 'currency')
        )
        for a in accounts:
            a['id'] = str(a['id'])

    ctx = {
        'accounts': accounts,
        'accounts_json': json.dumps(accounts),
    }
    return render(request, 'management/banks/expense_report.html', ctx)


@login_required(login_url='login')
def expense_report_data(request):
    """Tarih aralığı + filtre bazlı gider raporu (JSON).

    GET parametreleri:
        date_from   — YYYY-MM-DD (zorunlu)
        date_to     — YYYY-MM-DD (zorunlu)
        category_id — UUID (opsiyonel)
        account_id  — UUID (opsiyonel)

    Response:
        {
          "result": true,
          "summary": {"total_tl": "...", "count": N, "avg_tl": "..."},
          "by_category": [{"id":"<uuid|null>", "name":"...", "total_tl":"...","count":N}],
          "by_day": [{"date":"YYYY-MM-DD","total_tl":"...","count":N}],
          "rows": [{"id","date","category","description","amount_eur","payment_id"}]
        }
    """
    store, err = _store_or_error(request)
    if err:
        return err

    date_from_raw = (request.GET.get('date_from') or '').strip()
    date_to_raw = (request.GET.get('date_to') or '').strip()
    category_id = (request.GET.get('category_id') or '').strip() or None
    account_id = (request.GET.get('account_id') or '').strip() or None

    # Varsayılan: son 30 gün
    today = timezone.localdate()
    try:
        date_from = (
            datetime.strptime(date_from_raw, '%Y-%m-%d').date()
            if date_from_raw else today - timedelta(days=30)
        )
        date_to = (
            datetime.strptime(date_to_raw, '%Y-%m-%d').date()
            if date_to_raw else today
        )
    except ValueError:
        return JsonResponse(
            {'result': False, 'msg': 'Geçersiz tarih formatı (YYYY-MM-DD).'},
            status=400,
        )

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    # Aralığı dahil edici hale getir
    range_end = datetime.combine(
        date_to, datetime.max.time()
    )
    range_start = datetime.combine(date_from, datetime.min.time())

    qs = IncomeExpenseLedger.objects.filter(
        store=store,
        entry_type=IncomeExpenseLedger.EntryType.OTHER_EXPENSE,
        is_reversed=False,
        created_on__gte=range_start,
        created_on__lte=range_end,
    )

    if category_id:
        qs = qs.filter(expense_category_id=category_id)
    if account_id:
        qs = qs.filter(related_payment__bank_account_id=account_id)

    qs = qs.select_related('expense_category', 'related_payment__bank_account')

    # ── Özet ──
    agg = qs.aggregate(total=Sum('amount_eur'), cnt=Count('id'))
    total_tl = agg['total'] or Decimal('0')
    cnt = agg['cnt'] or 0
    avg_tl = (total_tl / cnt) if cnt else Decimal('0')

    summary = {
        'total_tl': _fmt_amount(total_tl),
        'count': cnt,
        'avg_tl': _fmt_amount(avg_tl),
    }

    # ── Kategori bazlı agregasyon ──
    cat_rows = (
        qs.values(
            'expense_category_id',
            'expense_category__name',
            'expense_category__color_css',
            'expense_category__icon',
        )
        .annotate(total_tl=Sum('amount_eur'), cnt=Count('id'))
        .order_by('-total_tl')
    )
    by_category = []
    for r in cat_rows:
        by_category.append({
            'id': str(r['expense_category_id']) if r['expense_category_id'] else None,
            'name': r['expense_category__name'] or 'Kategorisiz',
            'color_css': r['expense_category__color_css'] or '',
            'icon': r['expense_category__icon'] or '',
            'total_tl': _fmt_amount(r['total_tl'] or 0),
            'count': r['cnt'] or 0,
        })

    # ── Günlük dağılım ──
    from django.db.models.functions import TruncDate
    day_rows = (
        qs.annotate(d=TruncDate('created_on'))
        .values('d')
        .annotate(total_tl=Sum('amount_eur'), cnt=Count('id'))
        .order_by('d')
    )
    by_day = [
        {
            'date': r['d'].strftime('%Y-%m-%d') if r['d'] else '',
            'total_tl': _fmt_amount(r['total_tl'] or 0),
            'count': r['cnt'] or 0,
        }
        for r in day_rows
    ]

    # ── Detay satırları (limit 500) ──
    rows = []
    for entry in qs.order_by('-created_on')[:500]:
        cat_name = (
            entry.expense_category.name
            if entry.expense_category_id else 'Kategorisiz'
        )
        cash_name = ''
        if entry.related_payment_id and entry.related_payment.bank_account_id:
            cash_name = entry.related_payment.bank_account.name
        rows.append({
            'id': str(entry.id),
            'date': entry.created_on.strftime('%d.%m.%Y %H:%M'),
            'category': cat_name,
            'description': entry.description or '',
            'amount_eur': _fmt_amount(entry.amount_eur),
            'cashbox': cash_name,
            'payment_id': (
                str(entry.related_payment_id)
                if entry.related_payment_id else None
            ),
        })

    return JsonResponse({
        'result': True,
        'date_from': date_from.strftime('%Y-%m-%d'),
        'date_to': date_to.strftime('%Y-%m-%d'),
        'summary': summary,
        'by_category': by_category,
        'by_day': by_day,
        'rows': rows,
    })


# ════════════════════════════════════════════════════════════════════════════
# 5) GİDER KAYDI İPTALİ (REVERSAL)
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
@require_POST
@transaction.atomic
def expense_reverse(request):
    """Yanlış girilmiş gider kaydını iptal eder.

    Atomik üçlü:
      • Payment.is_cancelled=True (mevcut iptal patterni)
      • CashboxLedger için yeni REVERSAL satırı (parent FK; bakiye geri döner)
      • IncomeExpenseLedger.is_reversed=True (orijinalde, denormalize bayrak)

    Sadece OTHER_EXPENSE tipinde + henüz iptal edilmemiş kayıtlara izin verir.
    Müşteri carisine bağlı (related_customer_ledger) kayıtlar iptal edilemez —
    bunlar için mevcut LedgerService.reverse_entry akışı kullanılır.
    """
    store, err = _store_or_error(request)
    if err:
        return err

    entry_id = (request.POST.get('entry_id') or '').strip()
    if not entry_id:
        return JsonResponse(
            {'result': False, 'msg': 'Gider kaydı ID gerekli.'}, status=400
        )

    entry = (
        IncomeExpenseLedger.objects
        .select_related('related_payment__bank_account')
        .filter(id=entry_id, store=store)
        .first()
    )
    if not entry:
        return JsonResponse(
            {'result': False, 'msg': 'Gider kaydı bulunamadı.'}, status=404
        )

    if entry.entry_type != IncomeExpenseLedger.EntryType.OTHER_EXPENSE:
        return JsonResponse({
            'result': False,
            'msg': 'Sadece manuel gider kayıtları bu ekrandan iptal edilebilir.',
        }, status=400)

    if entry.is_reversed:
        return JsonResponse(
            {'result': False, 'msg': 'Bu kayıt zaten iptal edilmiş.'}, status=400
        )

    if entry.related_customer_ledger_id:
        return JsonResponse({
            'result': False,
            'msg': (
                'Müşteri carisine bağlı kayıtlar buradan iptal edilemez. '
                'Mevcut iptal akışını kullanın.'
            ),
        }, status=400)

    payment = entry.related_payment
    if not payment:
        return JsonResponse({
            'result': False,
            'msg': 'Bu gider kaydı bir ödeme satırına bağlı değil; iptal edilemez.',
        }, status=400)

    if payment.is_cancelled:
        # Eskiden iptal edilmiş ama is_reversed işaretlenmemiş — onar ve uyar
        entry.is_reversed = True
        entry.save(update_fields=['is_reversed'])
        return JsonResponse({
            'result': True,
            'msg': 'Bu ödeme zaten iptal edilmişti; gider kaydı senkronize edildi.',
        })

    account = payment.bank_account
    if not account:
        return JsonResponse({
            'result': False,
            'msg': 'Ödemenin bağlı olduğu kasa bulunamadı.',
        }, status=400)

    amount = Decimal(str(payment.amount))

    # Orijinal CashboxLedger satırı (REVERSAL parent için)
    original_cb = CashboxLedger.objects.filter(
        related_payment=payment,
        movement_type=CashboxLedger.MovementType.EXPENSE,
    ).first()

    # Bakiye snapshot için anlık bakiye
    cb_currency = (
        original_cb.currency
        if original_cb else (getattr(account, 'currency', 'TRY') or 'TRY')
    )
    if cb_currency not in ('TRY', 'USD', 'EUR', 'GBP', 'HS'):
        cb_currency = 'TRY'

    try:
        prior_balance = account.get_balance(currency=cb_currency)
    except Exception:
        prior_balance = Decimal('0')
    new_balance = (
        Decimal(str(prior_balance)) + amount
    ).quantize(Decimal('0.01'))

    # 1) Payment iptali
    payment.is_cancelled = True
    payment.cancelled_at = timezone.now()
    payment.save(update_fields=['is_cancelled', 'cancelled_at'])

    # 2) CashboxLedger.REVERSAL (parent FK ile orijinale bağlı)
    CashboxLedger.objects.create(
        cashbox=account,
        store=store,
        movement_type=CashboxLedger.MovementType.REVERSAL,
        amount=amount.quantize(Decimal('0.01')),
        currency=cb_currency,
        amount_eur_equivalent=amount.quantize(Decimal('0.01')),
        exchange_rate=(original_cb.exchange_rate if original_cb else None),
        balance_snapshot=new_balance,
        related_payment=payment,
        parent=original_cb,
        process_no=None,
        description=f'İPTAL — {entry.description or "Manuel gider"}'[:255],
        created_by=request.user,
        ip_address=request.META.get('REMOTE_ADDR') or None,
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:512],
    )

    # 3) IncomeExpenseLedger.is_reversed=True
    entry.is_reversed = True
    entry.save(update_fields=['is_reversed'])

    log.info(
        "FAZ61 EXPENSE_REVERSE: entry=%s amount=%s account=%s user=%s",
        entry.id, amount, account.name, request.user.username,
    )

    return JsonResponse({
        'result': True,
        'msg': f'{_fmt_amount(amount)} {cb_currency} gider iptal edildi; bakiye geri yüklendi.',
    })


# ════════════════════════════════════════════════════════════════════════════
# 6) BUGÜNKÜ ÖZET KPI (FAZ 65 — Hızlı Giriş Üst Başlık)
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
def expense_today_kpi(request):
    """Hızlı giriş sayfasının üst KPI satırı için anlık özet (read-only).

    Hesaplananlar (sadece is_reversed=False kayıtlar):
      - Bugün toplam tutar + satır sayısı
      - Bu ay toplam tutar + satır sayısı
      - Bu ay en yüksek kategori (ad + tutar)

    Response:
        {
          "result": true,
          "today": {"total_tl": "...", "count": N},
          "month": {"total_tl": "...", "count": N},
          "top_category": {"name": "...", "total_tl": "..."} | null
        }
    """
    store, err = _store_or_error(request)
    if err:
        return err

    today = timezone.localdate()
    month_start = today.replace(day=1)

    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    month_start_dt = datetime.combine(month_start, datetime.min.time())

    base_qs = IncomeExpenseLedger.objects.filter(
        store=store,
        entry_type=IncomeExpenseLedger.EntryType.OTHER_EXPENSE,
        is_reversed=False,
    )

    today_qs = base_qs.filter(
        created_on__gte=today_start,
        created_on__lte=today_end,
    )
    today_agg = today_qs.aggregate(total=Sum('amount_eur'), cnt=Count('id'))

    month_qs = base_qs.filter(created_on__gte=month_start_dt)
    month_agg = month_qs.aggregate(total=Sum('amount_eur'), cnt=Count('id'))

    top_cat_row = (
        month_qs.values('expense_category__name')
        .annotate(total_tl=Sum('amount_eur'))
        .order_by('-total_tl')
        .first()
    )
    top_category = None
    if top_cat_row and top_cat_row.get('total_tl'):
        top_category = {
            'name': top_cat_row.get('expense_category__name') or 'Kategorisiz',
            'total_tl': _fmt_amount(top_cat_row['total_tl']),
        }

    return JsonResponse({
        'result': True,
        'today': {
            'total_tl': _fmt_amount(today_agg['total'] or 0),
            'count': today_agg['cnt'] or 0,
        },
        'month': {
            'total_tl': _fmt_amount(month_agg['total'] or 0),
            'count': month_agg['cnt'] or 0,
        },
        'top_category': top_category,
    })
