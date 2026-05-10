from apps.stores.models import *


def calculate_process_profit(process: Process, current_buy_price: Decimal = None) -> Decimal:
    """
    Verilen işlem için Kar/Zarar hesabı yapar.
    Formül: Satış Tutarı - (Miktar * Alış Fiyatı)

    :param process: İşlem nesnesi
    :param current_buy_price: O anki güncel alış fiyatı (Frontend'den gelen veya veritabanındaki)
    :return: Decimal cinsinden Kar tutarı
    """
    # Sadece SATIŞ işlemlerinde kar hesaplanır
    if process.transaction_type != 'SALE':
        return Decimal('0.00')

    # 1. Miktar Belirleme (Gram veya Adet)
    # Process modelinde gram varsa gram, yoksa adet baz alınır.
    is_gram = (process.gram and process.gram > Decimal('0'))
    qty = process.gram if is_gram else Decimal(process.piece)

    if qty <= 0:
        return Decimal('0.00')

    # 2. Maliyet Hesabı (Cost)
    # Eğer parametre olarak alış fiyatı gelmişse onu kullan (ekrandaki anlık fiyat),
    # gelmemişse ürün kartındaki güncel alış fiyatını (buy_price_eur) çek.
    cost_unit_price = Decimal('0.00')

    if current_buy_price is not None:
        cost_unit_price = current_buy_price
    elif process.product:
        cost_unit_price = process.product.buy_price_eur or Decimal('0.00')

    total_cost = (qty * cost_unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # 3. Gelir Hesabı (Revenue)
    # Process.amount müşterinin ödediği son toplam tutardır (Metal + İşçilik + KDV).
    total_revenue = process.amount

    # 4. Kar = Gelir - Maliyet
    profit = total_revenue - total_cost

    return profit


from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from apps.settings.models import StoreConfiguration


def safe_dec(val, default='0', q='0.01'):
    """Güvenli Decimal çevirici (Eski _dec ve _d fonksiyonlarının birleşimi)"""
    if isinstance(val, (int, float, Decimal)):
        try:
            return Decimal(str(val)).quantize(Decimal(q), rounding=ROUND_HALF_UP)
        except:
            return Decimal(default)
    try:
        clean_val = str(val or default).replace(',', '.')
        return Decimal(clean_val).quantize(Decimal(q), rounding=ROUND_HALF_UP)
    except:
        return Decimal(default)


def get_store_config(user):
    """Mağaza konfigürasyonunu getirir."""
    store = getattr(user, 'store', None)
    if not store: return None
    config, _ = StoreConfiguration.objects.get_or_create(store=store)
    return config


def check_legal_limits(config, total_amount, is_output, payment_type, is_pos_flow, is_manual, manual_cash, customer):
    """
    2026 Yasal Limit Kontrolleri (Nakit, Fatura, MASAK)
    """
    CASH_LIMIT = Decimal('30000.00')
    INVOICE_LIMIT = Decimal('36000.00')
    MASAK_LIMIT = Decimal('185000.00')

    # Türkiye yasal limitleri kaldırıldı (Almanya akışı)
    enforce_cash = False
    enforce_invoice = False
    enforce_masak = False

    # 1. Nakit Limiti (Sadece Tahsilat yani Giriş işlemlerinde kritiktir)
    if enforce_cash and total_amount > CASH_LIMIT:
        error_msg = '30.000 TL üzeri tutarlar yasa gereği Nakit işlem yapılamaz.'

        # Manuel ödemede nakit kısmı limiti aşıyor mu?
        if is_manual and manual_cash > CASH_LIMIT:
            return error_msg

        # Tek çekim nakit ise (Manuel değil, POS değil)
        if not is_manual and not is_pos_flow and payment_type == 'CASH':
            return error_msg

    # 2. Fatura Limiti
    if enforce_invoice and total_amount >= INVOICE_LIMIT:
        if not customer:
            return f'{INVOICE_LIMIT} TL ve üzeri işlemlerde fatura zorunluluğu nedeniyle müşteri seçimi zorunludur.'

    # 3. MASAK Limiti
    if enforce_masak and total_amount >= MASAK_LIMIT:
        if not customer:
            return f'{MASAK_LIMIT} TL ve üzeri işlemlerde MASAK gereği müşteri seçimi zorunludur.'

        missing = []
        if not (getattr(customer, 'identification_number', '') or '').strip(): missing.append('TCKN')
        if not getattr(customer, 'identification_front_image', None): missing.append('Kimlik Ön Yüz')
        if not getattr(customer, 'identification_back_image', None): missing.append('Kimlik Arka Yüz')
        if missing:
            return 'MASAK gereği müşteri kimlik bilgileri zorunludur. Eksikler: ' + ', '.join(missing)

    return None
