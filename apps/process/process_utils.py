"""
Perakende ve toptan süreçleri için ortak utility fonksiyonları.
fast_views.py'den taşındı — Pavo/Invoice bağımlılığı olmayan saf yardımcılar.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from apps.process.models import Payment
from apps.products.models import Products
from apps.settings.models import StoreConfiguration
from apps.stock_management.models import StockSnapshot
from apps.banking.services import FXBalanceGuard

log = logging.getLogger(__name__)


def _dec(x, q='0.01'):
    try:
        return Decimal(str(x)).quantize(Decimal(q), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')


def _get_store_config(user):
    store = getattr(user, 'store', None)
    if not store:
        return None
    config, _ = StoreConfiguration.objects.get_or_create(store=store)
    return config


_CURRENCY_PRODUCT_MAP = {
    'USD': 'USDTRY',
    'EUR': 'EURTRY',
    'GBP': 'GBPTRY',
    'CAD': 'CADTRY',
    'QAR': 'QARTRY',
}


def _get_exchange_rate_for_currency(currency_code):
    if not currency_code or currency_code == 'TRY':
        return Decimal('1')
    product_name_key = _CURRENCY_PRODUCT_MAP.get(currency_code.upper())
    if not product_name_key:
        log.warning("Bilinmeyen döviz kodu: %s — kur bulunamadı.", currency_code)
        return None
    prod = Products.objects.filter(name__icontains=product_name_key).first()
    if prod and prod.sale_price_eur and prod.sale_price_eur > 0:
        return Decimal(str(prod.sale_price_eur))
    return None


def _build_currency_extra(bank_account, amount_eur):
    if not bank_account:
        return {}
    acct_currency = getattr(bank_account, 'currency', 'TRY') or 'TRY'
    if acct_currency == 'TRY':
        return {}
    rate = _get_exchange_rate_for_currency(acct_currency)
    if not rate or rate <= 0:
        return {}
    currency_amount = (amount_eur / rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'currency_amount': currency_amount, 'exchange_rate': rate}


def _resolve_or_create_cash_account(store, currency='TRY'):
    from apps.banking.models import BankAccount

    if currency == 'TRY':
        qs = BankAccount.objects.filter(
            store=store, account_type='CASH', currency='TRY',
            is_active=True, is_deleted=False,
        )
        count = qs.count()
        if count == 0:
            new_account = BankAccount.objects.create(
                store=store, name='Merkez TRY Nakit Kasası',
                account_type='CASH', currency='TRY', is_active=True,
            )
            return new_account, 1
        if count == 1:
            return qs.first(), 1
        return None, count

    fx_qs = BankAccount.objects.filter(
        store=store, account_type='CASH', currency='FX',
        is_active=True, is_deleted=False,
    )
    fx_count = fx_qs.count()
    if fx_count == 0:
        new_account = BankAccount.objects.create(
            store=store, name='Merkez Döviz Kasası',
            account_type='CASH', currency='FX', is_active=True,
        )
        return new_account, 1
    if fx_count == 1:
        return fx_qs.first(), 1
    return None, fx_count


def _get_approval_status(user):
    store = getattr(user, 'store', None)
    if not store:
        return False
    try:
        return bool(store.config.is_safe_approval_required)
    except Exception:
        return False


def _process_currency_exchange(
    process_no, product, qty, unit_price, total_amount_eur,
    operation_type, user, needs_approval,
    try_bank_account_id=None, fx_bank_account_id=None,
):
    store = getattr(user, 'store', None)
    if not store:
        raise ValidationError("Mağaza bilgisi bulunamadı.")

    product_name = (product.name or '').upper().strip()
    fx_currency = None
    for code in ['USD', 'EUR', 'GBP', 'CAD', 'QAR']:
        if product_name.startswith(code):
            fx_currency = code
            break

    if not fx_currency:
        raise ValidationError(
            f"Döviz kodu tespit edilemedi: '{product.name}'. "
            "Ürün adı USDTRY, EURTRY vb. formatında olmalı."
        )

    from apps.banking.models import BankAccount

    fx_account = None
    if fx_bank_account_id:
        fx_account = BankAccount.objects.filter(
            id=fx_bank_account_id, store=store,
            account_type='CASH', is_active=True, is_deleted=False,
        ).first()
    if not fx_account:
        fx_account, fx_count = _resolve_or_create_cash_account(store, fx_currency)
        if not fx_account:
            raise ValidationError(
                f"{fx_currency} para biriminde birden fazla nakit kasa bulundu ({fx_count} adet). "
                "Lütfen kasa seçimi yapınız."
            )

    try_account = None
    if try_bank_account_id:
        try_account = BankAccount.objects.filter(
            id=try_bank_account_id, store=store,
            account_type='CASH', currency='TRY',
            is_active=True, is_deleted=False,
        ).first()
    if not try_account:
        try_account, try_count = _resolve_or_create_cash_account(store, 'TRY')
        if not try_account:
            raise ValidationError(
                f"TRY para biriminde birden fazla nakit kasa bulundu ({try_count} adet). "
                "Lütfen kasa seçimi yapınız."
            )

    _is_approved = not needs_approval
    exchange_rate = unit_price

    if operation_type == 'PURCHASE':
        Payment.objects.create(
            process_no=process_no, payment_type='CASH',
            amount=total_amount_eur, currency_amount=qty, exchange_rate=exchange_rate,
            is_output=False, bank_account=fx_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
        )
        Payment.objects.create(
            process_no=process_no, payment_type='CASH',
            amount=total_amount_eur, is_output=True, bank_account=try_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
        )
    else:
        FXBalanceGuard.assert_sufficient(
            fx_bank_account=fx_account, currency_code=fx_currency,
            requested_amount=qty, use_lock=True,
        )
        Payment.objects.create(
            process_no=process_no, payment_type='CASH',
            amount=total_amount_eur, currency_amount=qty, exchange_rate=exchange_rate,
            is_output=True, bank_account=fx_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
        )
        Payment.objects.create(
            process_no=process_no, payment_type='CASH',
            amount=total_amount_eur, is_output=False, bank_account=try_account,
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
            is_approved=_is_approved,
        )

    return total_amount_eur, fx_account, try_account


def _process_barter_currency_entry(process_no, product, qty, unit_price, total_amount_eur, user, needs_approval):
    store = getattr(user, 'store', None)
    if not store:
        log.warning("Mağaza bilgisi bulunamadı — barter currency entry atlanıyor.")
        return None, Decimal('0')

    product_name = (product.name or '').upper().strip()
    fx_currency = None
    for code in ['USD', 'EUR', 'GBP', 'CAD', 'QAR']:
        if product_name.startswith(code):
            fx_currency = code
            break

    if not fx_currency:
        log.warning("Döviz kodu tespit edilemedi: '%s'", product.name)
        return None, Decimal('0')

    fx_account, fx_count = _resolve_or_create_cash_account(store, fx_currency)
    if not fx_account:
        log.warning("%s kasası bulunamadı/çoklu (%d). Takas döviz girişi atlanıyor.", fx_currency, fx_count)
        return None, Decimal('0')

    _is_approved = not needs_approval
    Payment.objects.create(
        process_no=process_no, payment_type='CASH',
        amount=total_amount_eur, currency_amount=qty, exchange_rate=unit_price,
        is_output=False, bank_account=fx_account,
        reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
        is_approved=_is_approved,
    )
    return fx_account, qty


def _calculate_and_save_profit(process, operation_type, purchase_amount, sale_amount, qty):
    if operation_type != 'SALE':
        return
    try:
        p_cost = _dec(purchase_amount)
        p_sale = _dec(sale_amount)

        if p_cost <= Decimal('0') and process.product:
            _snap = StockSnapshot.objects.filter(
                product=process.product, store=process.store
            ).first()
            urun_has_maliyet = _snap.weighted_avg_cost_hs if _snap else Decimal('0')
            urun_tl_maliyet = _snap.weighted_avg_cost_eur if _snap else Decimal('0')

            if not _snap or (urun_has_maliyet <= Decimal('0') and urun_tl_maliyet <= Decimal('0')):
                urun_has_maliyet = getattr(process.product, 'buy_price_hs', 0)
                urun_tl_maliyet = getattr(process.product, 'buy_price_eur', 0)

            if urun_has_maliyet and _dec(urun_has_maliyet) > Decimal('0'):
                kur = _dec(getattr(process, 'hs_rate_buy_eur', 0))
                if kur <= Decimal('0'):
                    kur = _dec(getattr(process, 'hs_rate_sale_eur', 0))
                if kur > Decimal('0'):
                    p_cost = _dec(urun_has_maliyet) * kur
            elif urun_tl_maliyet and _dec(urun_tl_maliyet) > Decimal('0'):
                p_cost = _dec(urun_tl_maliyet)

        if p_cost > Decimal('0') and p_sale > Decimal('0'):
            cost_amount_eur = _dec(p_cost * qty, '0.01')
            gross_profit_val = (p_sale - p_cost) * qty
            process.gross_profit = _dec(gross_profit_val, '0.01')
            tax_amount = gross_profit_val * Decimal('0.20')
            process.net_profit = _dec(gross_profit_val - tax_amount, '0.01')
            process.cost_amount_eur = cost_amount_eur

            hs_rate = _dec(
                getattr(process, 'hs_rate_buy_eur', None)
                or getattr(process, 'hs_rate_sale_eur', None)
                or 0
            )
            if hs_rate > Decimal('0'):
                process.cost_amount_hs = _dec(cost_amount_eur / hs_rate, '0.001')
            else:
                process.cost_amount_hs = Decimal('0.000')

            process.save(update_fields=['gross_profit', 'net_profit', 'cost_amount_eur', 'cost_amount_hs'])
    except Exception as e:
        log.error("Kâr hesaplama hatası: %s", e)
