# ============================================================================
# DOSYA: apps/invoices/esurec_serializers.py
# KONUM: Kuyum Plus projesi içinde
# AÇIKLAMA: Invoice → e-Süreç JSON payload serializer
#
# Kuyumcu fatura tipleri:
#   1. Özel Matrah (OZELMATRAH): Altın satışı + İşçilik
#      - Metal kalemi: KDV %0, special_tax_reason_code = '805' (KDVK 23/e)
#      - İşçilik kalemi: Özel matrah, KDV %20, special_tax_reason_code = '805'
#      - GİB kuralı: OZELMATRAH faturalarda '350' kodu YASAKTIR.
#        Tüm kalemler (metal dahil) '805' kullanmalıdır.
#   2. Normal Satış (SATIS): İşçilik/hizmet tek başına kesiliyorsa
#
# e-Fatura vs e-Arşiv:
#   - Mükellef sorgulaması yapılır, sonuca göre scenario belirlenir
#   - e-Fatura mükellefiyse: TEMELFATURA
#   - Değilse: EARSIVFATURA
#
# DÜZELTMEv4:
#   - Metal/İşçilik tespiti is_gram_bullion + vat_rate bazlı (notes yedek)
#   - invoice_type her zaman OZELMATRAH (metal+işçilik varsa)
#   - OZELMATRAH faturalarda TÜM kalemler special_tax_reason_code = '805'
#   - exemption_code ('350') sadece ISTISNA tipi faturalarda kullanılır
#   - notes alanından ESUREC_ID tagları temizleniyor (e-Süreç'e sızmaz)
#   - vat_rate int olarak gönderiliyor (e-Süreç int bekler)
# ============================================================================

import logging
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from django.utils import timezone

log = logging.getLogger(__name__)


def _s(val):
    return str(val).strip() if val else ''


# ── KP-01 DÜZELTMESİ: Ondalık hassasiyet kaybını önleme ──
# _f() float dönüşümü IEEE-754 yuvarlama hatalarına neden oluyordu.
# Altın gram hesaplamalarında (3.141 gr × 1000 TL/gr) bozuk
# değerler üretebiliyordu. Artık tüm ara hesaplamalar Decimal
# üzerinden yapılır, yalnızca JSON çıktısında float'a çevrilir.

def _d(val, default='0'):
    """Güvenli Decimal dönüşümü — ara hesaplamalar için."""
    try:
        return Decimal(str(val)) if val else Decimal(default)
    except (TypeError, ValueError, InvalidOperation):
        return Decimal(default)


def _n(val, dp=2):
    """
    JSON-safe float — Decimal üzerinden yuvarlayıp float'a çevirir.
    dp=2: tutar alanları (unit_price, discount, vat, total)
    dp=3: miktar alanları (quantity)
    dp=6: kur alanları (exchange_rate)
    """
    d = _d(val)
    quantized = d.quantize(Decimal(10) ** -dp, rounding=ROUND_HALF_UP)
    return float(quantized)


def _clean_notes(notes: str) -> str:
    """
    notes alanından e-Süreç'e gönderilmemesi gereken satırları temizler:

    1. ESUREC_ID:xxx → İç takip etiketi, e-Süreç'e sızmamalı.
    2. KDV İstisnası: → Otomatik eklenen yanlış/mükerrer istisna notları
       (örn: "KDV İstisnası: 801 - Milli Piyango..." gibi yanlış ibareler).
       Doğru istisna açıklaması zaten XML TaxExemptionReason alanında yer alır;
       notes alanında tekrar edilmesine gerek yoktur.
    """
    if not notes:
        return ''
    lines = []
    for line in notes.split('\n'):
        stripped = line.strip()
        # İç etiket: ESUREC_ID takip tagı
        if stripped.startswith('ESUREC_ID:'):
            continue
        # Otomatik eklenen istisna notu (801, 805 vb. — hepsi filtrelenir)
        if stripped.startswith('KDV İstisnası:'):
            continue
        lines.append(line)
    return '\n'.join(lines).strip()


def determine_invoice_scenario(invoice, esurec_client=None):
    """
    Alıcının VKN/TCKN'sini kullanarak e-Süreç üzerinden GİB mükellef
    sorgulaması yapar ve faturanın e-Fatura mı e-Arşiv mi olacağını belirler.

    Returns:
        tuple: (scenario, is_einvoice, gib_info_dict)
            gib_info_dict içinde ek anahtarlar:
                'gib_query_warning': str  → GİB sorgusu başarısızsa uyarı mesajı
                                           (boşsa uyarı yok, dolu ise UI'da gösterilmeli)
    """
    buyer_vkn = _get_buyer_vkn(invoice)

    if not buyer_vkn or buyer_vkn == '11111111111':
        return 'EARSIVFATURA', False, {}

    if esurec_client is None:
        try:
            from apps.invoices.esurec_client import ESurecClient
            esurec_client = ESurecClient()
        except Exception as e:
            log.warning(f"ESurecClient oluşturulamadı, e-Arşiv varsayılıyor: {type(e).__name__}")
            return 'EARSIVFATURA', False, {
                'gib_query_warning': (
                    f'e-Süreç bağlantısı kurulamadığı için GİB mükellefiyet sorgusu yapılamadı. '
                    f'e-Arşiv formatında fatura oluşturulacak. '
                    f'Alıcı VKN: {buyer_vkn}'
                ),
            }

    try:
        seller_vkn = _get_seller_vkn(invoice)
        resp = esurec_client.check_gib_user(buyer_vkn, seller_vkn=seller_vkn or None)
        if resp.get('result'):
            data = resp.get('data', {})
            if data.get('is_einvoice'):
                return 'TEMELFATURA', True, data
            else:
                return 'EARSIVFATURA', False, data
        else:
            warning_msg = (
                f'GİB mükellefiyet sorgusu başarısız oldu (VKN: {buyer_vkn}). '
                f'e-Arşiv formatında devam ediliyor. '
                f'Alıcı e-Fatura mükellefi ise yanlış formatta fatura kesilmiş olabilir. '
                f'Detay: {resp.get("error_msg", "Bilinmeyen hata")[:150]}'
            )
            log.warning(f"Mükellef sorgulama başarısız (VKN: {buyer_vkn}): {resp.get('error_msg')}")
            return 'EARSIVFATURA', False, {'gib_query_warning': warning_msg}
    except Exception as e:
        warning_msg = (
            f'GİB mükellefiyet sorgusu sırasında hata oluştu (VKN: {buyer_vkn}). '
            f'e-Arşiv formatında devam ediliyor.'
        )
        log.warning(f"Mükellef sorgulama hatası (VKN: {buyer_vkn}): {type(e).__name__}")
        return 'EARSIVFATURA', False, {'gib_query_warning': warning_msg}


def _get_buyer_vkn(invoice):
    if invoice.customer:
        return _s(invoice.customer.identification_number)
    if invoice.supplier:
        return _s(invoice.supplier.tax_number)
    return ''


def _get_seller_vkn(invoice):
    """Satıcı (mağaza/firma) VKN'si — e-Süreç GİB sorgusu için dealer çözümlemesinde kullanılır."""
    store = getattr(invoice, 'store', None)
    if not store:
        return ''
    company = getattr(store, 'company', None)
    return _s(getattr(company, 'tax_number', '') or getattr(store, 'tax_number', ''))


def _is_metal_item(item) -> bool:
    """
    Kalem altın/gümüş/metal kalemi mi? Güçlü tespit:
    1. is_gram_bullion alanı True VE vat_rate == 0 → kesinlikle metal
    2. notes alanında '17/4', 'metal', 'altın', 'külçe', 'has' → yedek tespit
    3. vat_rate == 0 ve birim GR/GRM/KG → büyük olasılıkla metal
    """
    vat = _d(item.vat_rate)

    # Birincil tespit: is_gram_bullion + KDV 0
    if getattr(item, 'is_gram_bullion', False) and vat == 0:
        return True

    # İkincil tespit: notes alanından keyword kontrolü
    notes_lower = (getattr(item, 'notes', '') or '').lower()
    if vat == 0 and any(kw in notes_lower for kw in ['17/4', 'metal', 'altın', 'külçe', 'kdv 0', 'has altın']):
        return True

    # Üçüncül tespit: KDV 0 + birim gram/kilogram
    if vat == 0:
        unit = str(getattr(item, 'unit', '') or '').upper()
        if unit in ('GR', 'GRM', 'KG', 'KGM'):
            return True

    return False


def _is_labor_item(item) -> bool:
    """
    Kalem işçilik kalemi mi? Güçlü tespit:
    1. is_gram_bullion False VE vat_rate > 0 → kesinlikle işçilik
    2. notes alanında 'işçilik', 'özel matrah', '23/e' → yedek tespit
    """
    vat = _d(item.vat_rate)

    # Birincil tespit: is_gram_bullion False + KDV > 0
    if not getattr(item, 'is_gram_bullion', True) and vat > 0:
        return True

    # İkincil tespit: notes keyword kontrolü
    notes_lower = (getattr(item, 'notes', '') or '').lower()
    if vat > 0 and any(kw in notes_lower for kw in ['işçilik', '23/e', 'özel matrah']):
        return True

    return False


def _determine_invoice_type(invoice):
    """
    Fatura kalemlerini inceleyerek fatura tipini belirler.

    Kuyumcu kuralları:
    - Metal (%0 KDV) + İşçilik (%20 KDV) varsa → OZELMATRAH
    - Sadece işçilik/hizmet varsa → SATIS
    - Sadece metal varsa → SATIS (istisna kodlu)
    - İade ise → IADE
    """
    if invoice.invoice_type == 'RETURN':
        return 'IADE'

    has_metal = False
    has_labor = False

    for item in invoice.items.all():
        if _is_metal_item(item):
            has_metal = True
        elif _is_labor_item(item):
            has_labor = True
        elif _d(item.vat_rate) > 0:
            # KDV'li ama sınıflandırılamayan kalem → işçilik/hizmet sayılır
            has_labor = True

    if has_metal and has_labor:
        return 'OZELMATRAH'
    if has_metal:
        return 'ISTISNA'

    return 'SATIS'


def serialize_invoice_for_esurec(invoice, scenario=None, is_einvoice=None, gib_info=None) -> dict:
    """
    Invoice nesnesini e-Süreç'in beklediği JSON formatına çevirir.

    Args:
        invoice: Invoice model instance
        scenario: Önceden belirlenmiş senaryo (None ise otomatik belirlenir)
        is_einvoice: Önceden belirlenmiş mükellef durumu
        gib_info: GİB mükellef sorgusu sonucu (receiver_alias/pk bilgisi içerir)
    """
    store = invoice.store
    company = getattr(store, 'company', None)

    if scenario is None:
        scenario = invoice.scenario or 'TEMELFATURA'

    # Kuyum Plus iş kuralı: Tüm faturalar OZELMATRAH olarak kesilir.
    # İade faturası ise GİB kuralına göre IADE tipi zorunludur.
    if invoice.invoice_type == 'RETURN':
        invoice_type = 'IADE'
    else:
        invoice_type = 'OZELMATRAH'

    seller = {
        'title': _s(getattr(company, 'title', '') or getattr(store, 'title', '')),
        'tax_number': _s(getattr(company, 'tax_number', '')),
        'tax_office': _s(getattr(company, 'tax_office', '')),
        'tax_office_code': _s(getattr(company, 'tax_office_code', '')),
        'mersis_no': _s(getattr(company, 'mersis_no', '')),
        'trade_registry_no': _s(getattr(company, 'trade_registry_no', '')),
        'address': _s(getattr(company, 'address', '') or getattr(store, 'address', '')),
        'city': _s(getattr(company, 'city', '') or getattr(store, 'city', '')),
        'district': _s(getattr(company, 'district', '') or getattr(store, 'district', '')),
        'phone': _s(getattr(company, 'phone', '') or getattr(store, 'phone', '')),
        'email': _s(getattr(company, 'email', '') or getattr(store, 'email', '')),
    }

    buyer = _build_buyer(invoice)

    # GİB sorgusu sonucundan receiver PK alias bilgisini buyer'a ekle
    if gib_info and isinstance(gib_info, dict):
        pk_alias = gib_info.get('pk', '') or gib_info.get('receiver_alias', '')
        if pk_alias:
            buyer['receiver_alias'] = pk_alias

    header = {
        'external_ref': _s(invoice.invoice_no),
        'scenario': scenario,
        'invoice_type': invoice_type,
        # KRİTİK: issue_date DateTimeField.
        # USE_TZ=True → DB'de UTC saklanır, strftime() UTC tarihini verir (KAYMA!).
        # USE_TZ=False → DB'de naive yerel saat saklanır, strftime() zaten doğru.
        # timezone.is_aware() ile kontrol et: aware ise localtime ile yerel zamana
        # çevir, naive ise (USE_TZ=False) olduğu gibi kullan.
        'invoice_date': (timezone.localtime(invoice.issue_date) if timezone.is_aware(invoice.issue_date) else invoice.issue_date).strftime('%Y-%m-%d') if invoice.issue_date else timezone.now().strftime('%Y-%m-%d'),
        'issue_time': (timezone.localtime(invoice.issue_date) if timezone.is_aware(invoice.issue_date) else invoice.issue_date).strftime('%H:%M:%S') if invoice.issue_date else timezone.now().strftime('%H:%M:%S'),
        'currency': _s(invoice.currency or 'TRY'),
        'exchange_rate': _n(invoice.exrate_to_try, 6),
        'notes': _clean_notes(invoice.notes),
    }
    if invoice.due_date:
        header['due_date'] = str(invoice.due_date)

    unit_map = {'GR': 'GRM', 'AD': 'C62', 'CM': 'CMT', 'KG': 'KGM'}
    items = []
    for item in invoice.items.all():
        vat = _d(item.vat_rate)
        vat_int = int(vat)

        d = {
            'name': _s(item.product_name),
            'code': _s(item.barcode),
            'quantity': _n(item.quantity, 3),
            'unit': unit_map.get(str(item.unit or '').upper(), 'C62'),
            'unit_price': _n(item.unit_price, 3),
            'vat_rate': vat_int,
            'discount_amount': _n(item.discount_amount, 2),
            # KRİTİK: Sayısal alanlar asla null gönderilmemeli.
            # e-Süreç InvoiceItem.withholding_taxable_amount NOT NULL (default=0).
            # null gönderilirse PostgreSQL constraint ihlali oluşur.
            'withholding_taxable_amount': _n(getattr(item, 'withholding_taxable_amount', 0), 2),
            'exemption_reason': _s(item.exemption_reason),
            'notes': _s(item.notes),
        }

        if item.price_hs:
            d['meta_price_hs'] = _n(item.price_hs, 3)

        if item.withholding_rate and _d(item.withholding_rate) > 0:
            d['withholding_rate'] = _n(item.withholding_rate, 2)
            # Tevkifat matrahı: withholding_taxable_amount yoksa KDV tutarını hesapla
            # KP-01 & KP-05: Tüm ara hesaplamalar Decimal üzerinden yapılır
            if d['withholding_taxable_amount'] == 0.0:
                net = _d(item.quantity) * _d(item.unit_price) - _d(item.discount_amount)
                wh_amount = (net * vat / _d(100)).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                ) if vat > 0 else Decimal('0')
                d['withholding_taxable_amount'] = float(wh_amount)

        if invoice_type == 'OZELMATRAH':
            # ── OZELMATRAH FATURA: TÜM KALEMLER '805' KULLANMALI ──
            # GİB Şematron kuralı: OZELMATRAH faturalarda '350' (Tam İstisna) YASAKTIR.
            # Metal (%0 KDV) dahil tüm kalemlerde special_tax_reason_code = '805' (KDVK 23/e).
            d['special_tax_reason_code'] = '805'

            # İşçilik kalemleri için özel matrah tutarını da ekle
            if _is_labor_item(item):
                d['special_tax_base'] = _n(item.total_excl_vat, 2)

        else:
            # ── OZELMATRAH DIŞI FATURALAR ──
            # Metal kalemi: Tam İstisna (KDVK 17/4-g) — sadece ISTISNA tipi faturalarda geçerli
            if _is_metal_item(item):
                d['exemption_code'] = '350'

            # İşçilik kalemi: Normal KDV uygulanır (özel matrah yok)

        items.append(d)

    totals = {
        'subtotal': _n(invoice.subtotal, 2),
        'discount_total': _n(invoice.discount_total, 2),
        'tax_total': _n(invoice.tax_total, 2),
        'grand_total': _n(invoice.grand_total, 2),
        'paid_total': _n(invoice.paid_total, 2),
    }
    if invoice.tax_breakdown:
        totals['tax_breakdown'] = invoice.tax_breakdown

    return {
        'invoice_type': invoice_type,
        'profile_id': scenario,
        'header': header,
        'seller': seller,
        'buyer': buyer,
        'items': items,
        'totals': totals,
    }


def _build_buyer(invoice) -> dict:
    """
    Invoice → e-Süreç 'buyer' bölümü.

    KP-13: Nihai tüketici fallback burada gerçekleşir. View ve Celery task
    katmanındaki _validate_customer_for_esurec threshold kontrolü yaptıktan
    sonra buraya erişim izni verilir. Bu seviyede gelen boş TCKN/VKN'ler
    yasal olarak "Nihai Tüketici" kabul edilir ve 11111111111 atanır.
    Audit log her fallback işlemi için kayıt bırakır (operasyon takibi).
    """
    if invoice.customer:
        c = invoice.customer
        raw_id = _s(c.identification_number)
        if not raw_id:
            log.info(
                "[KP-13] Nihai tüketici fallback → 11111111111 atandı: "
                "fatura=%s müşteri=%s",
                getattr(invoice, 'invoice_no', '?'),
                f"{_s(c.first_name)} {_s(c.last_name)}".strip() or '(isim yok)',
            )
        return {
            'type': 'customer',
            'title': f"{_s(c.first_name)} {_s(c.last_name)}".strip() or 'Nihai Tüketici',
            'tax_number': raw_id or '11111111111',
            'tax_office': _s(getattr(c.tax_office, 'name', '')) if c.tax_office else '',
            'address': _s(c.address),
            'city': _s(getattr(c.city, 'name', '')) if c.city else '',
            'district': _s(getattr(c.district, 'name', '')) if c.district else '',
            'phone': _s(c.phone),
            'email': _s(c.email),
        }
    if invoice.supplier:
        s = invoice.supplier
        return {
            'type': 'supplier',
            'title': _s(s.company_name),
            'tax_number': _s(s.tax_number),
            'tax_office': _s(getattr(s.tax_office, 'name', '')) if s.tax_office else '',
            'address': _s(s.company_address),
            'phone': _s(s.phone),
            'email': _s(s.email),
        }
    # Müşteri de tedarikçi de yok → Muhtelif Müşteri (view/task threshold'u geçti)
    log.info(
        "[KP-13] Nihai tüketici fallback → 'Muhtelif Müşteri' + 11111111111 "
        "atandı: fatura=%s",
        getattr(invoice, 'invoice_no', '?'),
    )
    return {
        'type': 'unknown',
        'title': 'Muhtelif Müşteri',
        'tax_number': '11111111111',
    }
