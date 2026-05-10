# apps/custody/views.py
"""Emanet (Custody) Yönetimi — View katmanı.

FAZ 22 — Emanet Kriz Fix kapsamı:
  A-1) `delete` endpoint artık fiziksel DELETE yapmaz; soft-delete +
       REVERSAL pattern üzerinden iz korunur.
  A-2) `change_status` toggle akışı idempotent kontrolle korunur:
       Zaten tam teslim edilmiş bir kayıt tekrar OUT'a çevrilemez.
  A-3) Kısmi teslim artık orijinal IN kaydını mutate ETMEZ; yeni OUT
       kaydı `parent=IN` ile yazılır. Bakiye sorgusu append-only
       prensibine uyum sağlar.
  B-1/B-2/B-3) Yeni endpoint'ler:
       - custody_receipt_data → tek emanet için fiş bilgisi
       - customer_custody_history → müşteri bazlı emanet ekstresi
       - custody_reverse → REVERSAL yazımı (UI butonu)
"""
import random
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET

from apps.roles.decorators import role_required
from apps.custody.models import CustomerCustodyLedger
from apps.customers.models import Customers
from apps.products.models import Products

from apps.stock_management.services.stock_service import StockService, InsufficientStockError
from apps.stock_management.services.cancel_service import cancel_stock_entry
from apps.stock_management.models import StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.definitions.categories.models import Categories
from apps.scraps.models import Scraps
from apps.bracelets.models import Bracelets

# FAZ 49 — Emanet havuzu artık ana Hurdalar / Bilezikler sayfalarıyla
# AYNI Products kayıtlarını paylaşır. Bunun için ortak helper'lar (canonical
# isimlendirme + havuz bulma) doğrudan ana modüllerden ithal edilir; emanet
# tarafı paralel/duplike bir Products oluşturmaz. Cüzdan izolasyonu
# StockSnapshot.custody_gram alanı tarafından korunur (StockService
# CUSTODY_IN reason'ı yalnız custody_gram'a yazar, quantity_gram/WAC
# değişmez — kanıt: stock_management/services/stock_service.py:346-360).
from apps.scraps.views import (
    extract_scrap_karat_label,
    find_scrap_pool_by_selected_karat,
)
from apps.bracelets.views import find_bracelet_pool_by_name

from apps.customers.services.custody_offset import (
    CustodyOffsetService,
    get_custody_balance_hs,
)
from apps.customers.services.custody_to_stock import (
    CustodyToStockService,
)
from apps.customers.services.audit import extract_audit_context
from apps.customers.services.exceptions import (
    LedgerError, InvalidLedgerStateError, InsufficientCustodyError,
)

logger = logging.getLogger(__name__)


def generate_custody_process_no():
    return 'CUS' + ''.join(random.choices('0123456789', k=10))


def _decimal_or_zero(raw):
    if raw is None or raw == '':
        return Decimal('0')
    try:
        return Decimal(str(raw).replace(',', '.'))
    except (InvalidOperation, ValueError):
        return Decimal('0')


def _staff_name(u):
    if not u:
        return '-'
    return f"{u.first_name or ''} {u.last_name or ''}".strip() or (u.username or '-')


# ──────────────────────────────────────────────────────────────────
# 1) ANA SAYFA
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CUSTODY_INDEX')
def custody_index(request):
    store = request.user.store

    # FAZ 27 — BUG-FIX (Senaryo 1 / Hata 2):
    #   Append-only mimaride is_returned filtresi tek başına yeterli
    #   değil; orijinal IN, fully-delivered olsa da is_returned=False
    #   kalır. Sayfa header'ındaki "total_hs" KPI'ı net kalan üzerinden
    #   gösterilmeli — her IN için remaining_amount_hs property'si
    #   toplanır (DB-level Sum yerine Python tek geçişi).
    in_qs = CustomerCustodyLedger.objects.filter(
        store=store,
        custody_type=CustomerCustodyLedger.CUSTODY_IN,
        is_active=True,
        is_deleted=False,
        is_returned=False,
    )
    _net_total_hs = Decimal('0')
    for _r in in_qs.iterator():
        _rem = _r.remaining_amount_hs
        if _rem > Decimal('0.0005'):
            _net_total_hs += _rem
    qs = {'total_hs': _net_total_hs}

    # FAZ 49 — Sarrafiye/Ziynet dropdown'ı yalnızca SAF sarrafiye ürünlerini
    # içermeli: barkodlu ürünler, bilezikler, hurdalar, döviz ve gümüş hariç.
    # Kullanıcı şikayeti: Listede "Bileklik" / "14 Ayar" gibi alakasız
    # kayıtlar görünüyordu — kategori filtreleri yetersizdi (Bracelets tablosu
    # ve barcode alanı kontrol edilmiyordu).
    products = (
        Products.objects.filter(
            Q(store=store) | Q(store__isnull=True),
            is_active=True,
            is_deleted=False,
            is_scrap=False,
            is_currency=False,
            material_type='GOLD',
        )
        # Yalnızca barkodsuz (sarrafiye) ürünler
        .filter(Q(barcode__isnull=True) | Q(barcode=''))
        .exclude(
            Q(category__name__icontains='Hurda') |
            Q(category__name__icontains='Bilezik') |
            Q(category__name__icontains='Döviz') |
            Q(category__name__icontains='Doviz') |
            Q(name__endswith='TRY') |
            Q(name__in=['USD', 'EUR', 'GBP', 'CAD', 'QAR', 'CHF', 'JPY', 'SAR',
                        'AED', 'AUD', 'KWD', 'OMR', 'RUB', 'BGN', 'NOK', 'SEK',
                        'DKK', 'CNY', 'ILS', 'MAD', 'JOD', 'EUR/KG', 'onstry'])
        )
        # Bracelets tablosunda kayıtlı her ürünü dışla (kategori adı farklı
        # olsa bile bilezikler bu çocuk tablo üzerinden tespit edilir).
        .exclude(
            id__in=Bracelets.objects.filter(is_deleted=False).values('product_id')
        )
        .order_by('display_order', 'name')
    )

    return render(request, 'management/custody/index.html', {
        'title': 'Emanet (Depo)',
        'customers': Customers.objects.filter(store=store, is_active=True, is_deleted=False),
        'products': products,
        'total_hs': qs['total_hs'],
        'store': store,
    })


# ──────────────────────────────────────────────────────────────────
# 2) EMANET EKLEME
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_ADD_CUSTODY')
@transaction.atomic
def add_custody(request):
    """FAZ 49 — Emanet Ekle: 3 sekme (Sarrafiye / Hurda / Bilezik).

    Kritik mimari kurallar:
      • CÜZDAN İZOLASYONU: StockSnapshot.custody_gram alanı emanet için
        ayrılmıştır. CUSTODY_IN reason'ı yalnız bu alanı artırır;
        satılabilir mağaza stoğu (quantity_gram + WAC) etkilenmez.
      • ORTAK HAVUZ: Emanet, ana Hurdalar/Bilezikler sayfalarıyla aynı
        Products kaydını paylaşır. Yeni Products yaratma kararı
        scraps/bracelets helper'larından çağrılır; canonical isimlendirme
        ("14 Ayar", kullanıcının bilezik adı) korunur.
      • PRODUCT.product_mileage DOKUNULMAZ: Emanet, paylaşılan ürün
        kartının ağırlıklı milyemini değiştirmez (yalnız öz mal etkiler).
        Müşterinin emaneti CustomerCustodyLedger.amount_hs alanında
        donmuş şekilde saklanır (= gram × kullanıcı_milyemi / 1000).
      • İŞÇİLİK ÖLÜ: Bilezik/hurda emanetinde işçilik (labor_mileage,
        piece_labor) hesaba katılmaz; yeni bilezik havuzu açılırken bu
        alanlar 0 set edilir.
      • MANUEL OVERRIDE: Sarrafiye birim has, hurda/bilezik milyem
        kullanıcı tarafından düzenlenebilir; backend her zaman kendi
        amount_hs hesabını yapar (JS değerine güvenmez).
    """
    try:
        store = request.user.store
        cust = Customers.objects.get(id=request.POST.get('customer_id'))
        custody_id = request.POST.get('custody_id')
        ctype = request.POST.get('custody_type') or CustomerCustodyLedger.CUSTODY_IN

        emanet_turu = request.POST.get('emanet_turu')  # 'ziynet' | 'hurda' | 'bilezik'
        description = (request.POST.get('description') or '').strip()

        # Backend daima kendi amount_hs'ini hesaplar — JS gönderdiği değer
        # güvenilmez (manipüle edilebilir).
        qty_piece = 0
        qty_gram = Decimal('0')
        amount_hs = Decimal('0')
        unit_cost_hs = Decimal('0')

        product = None

        # ============================================================
        # 1. KISIM — TÜRE GÖRE ÜRÜN BULMA / YARATMA + AMOUNT_HS HESABI
        # ============================================================
        if emanet_turu == 'ziynet':
            prod_id = request.POST.get('product_id')
            if not prod_id:
                return JsonResponse({'result': False,
                                     'error_msg': 'Ziynet ürünü seçilmelidir.'})
            try:
                product = Products.objects.get(id=prod_id)
            except Products.DoesNotExist:
                return JsonResponse({'result': False,
                                     'error_msg': 'Seçilen ürün bulunamadı.'})

            # Adet veya gram girişi (ürünün is_gram_bullion'ına göre)
            raw_val = (request.POST.get('quantity_piece') or '0').strip().replace(',', '.')
            try:
                raw_amount = Decimal(raw_val) if raw_val else Decimal('0')
            except (InvalidOperation, ValueError):
                return JsonResponse({'result': False,
                                     'error_msg': 'Adet/Gram alanı sayısal olmalıdır.'})
            if raw_amount <= 0:
                return JsonResponse({'result': False,
                                     'error_msg': 'Adet/Gram 0\'dan büyük olmalıdır.'})

            if getattr(product, 'is_gram_bullion', False):
                qty_gram = raw_amount
                qty_piece = 0
            else:
                qty_piece = int(raw_amount)
                qty_gram = Decimal('0')

            # Birim Has — kullanıcı manuel override edebilir; aksi halde
            # ürünün buy_price_hs değeri esas alınır.
            override_raw = (request.POST.get('unit_hs_override') or '').strip().replace(',', '.')
            if override_raw:
                try:
                    unit_cost_hs = Decimal(override_raw)
                except (InvalidOperation, ValueError):
                    return JsonResponse({'result': False,
                                         'error_msg': 'Birim Has alanı geçersiz.'})
                if unit_cost_hs <= 0:
                    return JsonResponse({'result': False,
                                         'error_msg': 'Birim Has 0\'dan büyük olmalıdır.'})
            else:
                unit_cost_hs = Decimal(str(product.buy_price_hs or '0'))

            # amount_hs = miktar × birim has (gram veya adet)
            qty_for_calc = qty_gram if qty_gram > 0 else Decimal(qty_piece)
            amount_hs = (qty_for_calc * unit_cost_hs).quantize(Decimal('0.001'))

        elif emanet_turu == 'hurda':
            qty_gram = _decimal_or_zero(request.POST.get('quantity_gram', '0'))
            mileage = _decimal_or_zero(request.POST.get('milyem', '0'))
            ayar_label = (request.POST.get('ayar_label') or '').strip()

            if qty_gram <= 0:
                return JsonResponse({'result': False,
                                     'error_msg': 'Gram 0\'dan büyük olmalıdır.'})
            if mileage < 1 or mileage > 999:
                return JsonResponse({'result': False,
                                     'error_msg': 'Milyem 1 ile 999 arasında olmalıdır.'})

            # Ana Hurdalar sayfasıyla AYNI kategoriyi yakala (icontains).
            category = Categories.objects.filter(name__icontains='Hurda').first()
            if category is None:
                category, _ = Categories.objects.get_or_create(name='Hurda')

            # Canonical havuz anahtarı: kullanıcının seçtiği ayar etiketi.
            # Yoksa milyemden floor yöntemiyle türetilir (helper'ın
            # sorumluluğu).
            karat_label = extract_scrap_karat_label(
                scrap_name=ayar_label,
                fallback_mileage=mileage,
                material_type='GOLD',
            )

            product = find_scrap_pool_by_selected_karat(
                store=store, category=category,
                scrap_name=ayar_label,
                fallback_mileage=mileage,
                is_scrap=True,
                material_type='GOLD',
            )

            if product is None:
                # Yeni canonical havuz aç — ana Hurdalar sayfasıyla
                # aynı isimlendirme ("14 Ayar", "22 Ayar"...).
                pool_name = karat_label or f"{int(mileage)} Milyem"
                pool_initial_hs = (mileage / Decimal('1000')).quantize(Decimal('0.001'))
                product = Products.objects.create(
                    store=store, category=category,
                    name=pool_name, gram=Decimal('0'),
                    product_mileage=Decimal(int(mileage)),
                    buy_price_hs=pool_initial_hs,
                    sale_price_hs=pool_initial_hs,
                    is_scrap=True, is_gram_bullion=True, is_active=True,
                    material_type='GOLD',
                )
                Scraps.objects.create(store=store, product=product,
                                      created_by=request.user)
            else:
                # Mevcut Products bulundu — Scraps kaydını garanti et.
                # Önceden soft-delete edilmiş bir havuza emanet geliyorsa
                # Scraps satırı yoksa veya pasifse aktive edilir; aksi halde
                # CUSTODY_2_STK sonrası ana Hurdalar sayfasında görünmez
                # (base queryset is_deleted=False filtresi).
                scrap_row = Scraps.objects.filter(
                    product=product, store=store,
                ).first()
                if scrap_row:
                    _sf = []
                    if scrap_row.is_deleted:
                        scrap_row.is_deleted = False
                        _sf.append('is_deleted')
                    if scrap_row.is_active is False:
                        scrap_row.is_active = True
                        _sf.append('is_active')
                    if _sf:
                        scrap_row.save(update_fields=_sf)
                else:
                    Scraps.objects.create(
                        store=store, product=product,
                        created_by=request.user,
                    )
                if product.is_active is False or product.is_deleted is True:
                    Products.objects.filter(id=product.id).update(
                        is_active=True, is_deleted=False,
                    )
                    product.is_active = True
                    product.is_deleted = False

            unit_cost_hs = (mileage / Decimal('1000')).quantize(Decimal('0.001'))
            amount_hs = (qty_gram * unit_cost_hs).quantize(Decimal('0.001'))
            qty_piece = 0

        elif emanet_turu == 'bilezik':
            qty_gram = _decimal_or_zero(request.POST.get('quantity_gram', '0'))
            mileage = _decimal_or_zero(request.POST.get('milyem', '0'))
            b_name = (request.POST.get('bracelet_name') or '').strip()

            if len(b_name) < 2:
                return JsonResponse({'result': False,
                                     'error_msg': 'Bilezik adı en az 2 karakter olmalıdır.'})
            if qty_gram <= 0:
                return JsonResponse({'result': False,
                                     'error_msg': 'Gram 0\'dan büyük olmalıdır.'})
            if mileage < 1 or mileage > 999:
                return JsonResponse({'result': False,
                                     'error_msg': 'Milyem 1 ile 999 arasında olmalıdır.'})

            # Ana Bilezikler sayfasıyla AYNI kategori (icontains).
            category = Categories.objects.filter(name__icontains='Bilezik').first()
            if category is None:
                category, _ = Categories.objects.get_or_create(name='Bilezik')

            # İsim bazlı havuz arama (case-insensitive). Aynı isimli aktif
            # bilezik kaydı varsa o kullanılır.
            product = find_bracelet_pool_by_name(
                store=store, category=category, name=b_name,
            )

            if product is None:
                # Yeni bilezik havuzu — İŞÇİLİK ÖLÜ kuralı: labor_mileage
                # ve piece_labor sıfır set edilir.
                pool_initial_hs = (mileage / Decimal('1000')).quantize(Decimal('0.001'))
                product = Products.objects.create(
                    store=store, category=category,
                    name=b_name, gram=Decimal('0'),
                    product_mileage=Decimal(int(mileage)),
                    buy_price_hs=pool_initial_hs,
                    sale_price_hs=pool_initial_hs,
                    labor_mileage=Decimal('0'),
                    piece_labor=Decimal('0'),
                    is_gram_bullion=True, is_active=True,
                    material_type='GOLD',
                )
                Bracelets.objects.create(store=store, product=product,
                                         created_by=request.user)
            else:
                # Mevcut Products bulundu — Bracelets kaydını garanti et.
                # bracelets/views.py:583-597 ile birebir simetri.
                bracelet_row = Bracelets.objects.filter(
                    product=product, store=store,
                ).first()
                if bracelet_row:
                    _bf = []
                    if bracelet_row.is_deleted:
                        bracelet_row.is_deleted = False
                        _bf.append('is_deleted')
                    if bracelet_row.is_active is False:
                        bracelet_row.is_active = True
                        _bf.append('is_active')
                    if _bf:
                        bracelet_row.save(update_fields=_bf)
                else:
                    Bracelets.objects.create(
                        store=store, product=product,
                        created_by=request.user,
                    )
                if product.is_active is False or product.is_deleted is True:
                    Products.objects.filter(id=product.id).update(
                        is_active=True, is_deleted=False,
                    )
                    product.is_active = True
                    product.is_deleted = False

            unit_cost_hs = (mileage / Decimal('1000')).quantize(Decimal('0.001'))
            amount_hs = (qty_gram * unit_cost_hs).quantize(Decimal('0.001'))
            qty_piece = 1

        else:
            return JsonResponse({'result': False,
                                 'error_msg': 'Geçersiz emanet türü.'})

        # ============================================================
        # 2. KISIM — EMANET DEFTERİNE KAYIT (CustomerCustodyLedger)
        # ============================================================
        if custody_id:
            # APPEND-ONLY kuralı: Quantity / amount alanları
            # düzenlenmez; yalnız metinsel açıklama güncellenir.
            row = CustomerCustodyLedger.objects.get(id=custody_id, store=store)
            if row.is_deleted or not row.is_active:
                return JsonResponse({'result': False,
                                     'error_msg': 'Bu kayıt iptal/silinmiş; düzenlenemez.'})
            row.description = description
            row.save(update_fields=['description'])
        else:
            # YENİ EMANET — TL raporlama için anlık piyasa kuru çek
            try:
                hs_data = PriceService.get_price('GOLD_24K')
                current_has_buy_eur = Decimal(str(hs_data.get('buy_tl', '0')))
            except Exception:
                current_has_buy_eur = Decimal('0')

            amount_eur = (
                (amount_hs * current_has_buy_eur).quantize(Decimal('0.01'))
                if current_has_buy_eur > 0 else Decimal('0.00')
            )

            process_no = generate_custody_process_no()
            row = CustomerCustodyLedger.objects.create(
                customer=cust, store=store, product=product,
                custody_type=ctype,
                quantity_piece=qty_piece, quantity_gram=qty_gram,
                amount_hs=amount_hs,
                exchange_rate_eur=current_has_buy_eur,
                amount_eur=amount_eur,
                process_no=process_no, description=description,
                is_returned=False, is_active=True, is_deleted=False,
                created_by=request.user, received_by=request.user,
                ip_address=(request.META.get('HTTP_X_FORWARDED_FOR')
                            or request.META.get('REMOTE_ADDR')
                            or '').split(',')[0].strip() or None,
                user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:255],
            )

            # ========================================================
            # 3. KISIM — STOK SNAPSHOT (CUSTODY_GRAM bumping)
            # ========================================================
            # CUSTODY_IN reason'ı yalnız StockSnapshot.custody_gram alanını
            # artırır (bkz: stock_management/services/stock_service.py:346).
            # quantity_gram (öz mal) ve weighted_avg_cost_hs DEĞİŞMEZ —
            # bu sayede aynı Products kaydı emanet ile öz malı fiziksel
            # düzeyde ayırır.
            if product and ctype == CustomerCustodyLedger.CUSTODY_IN:
                unit_cost_eur = (
                    (unit_cost_hs * current_has_buy_eur).quantize(Decimal('0.01'))
                    if current_has_buy_eur > 0 else Decimal('0')
                )
                StockService.record_entry(
                    product=product, store=store,
                    quantity_gram=qty_gram, quantity_pieces=qty_piece,
                    reason=StockLedger.Reason.CUSTODY_IN,
                    ref_type='custody_manual', ref_id=str(row.id),
                    unit_cost_hs=unit_cost_hs, unit_cost_eur=unit_cost_eur,
                    hs_rate_eur=current_has_buy_eur,
                    user=request.user,
                    notes=f"Emanet Alındı ({(emanet_turu or '').upper()}) - İşlem: {process_no}",
                )

        return JsonResponse({
            'result': True,
            'row_id': str(row.id),
            'process_no': row.process_no,
        })

    except Customers.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri bulunamadı.'})
    except Exception as e:
        logger.exception("Emanet ekleme hatası")
        return JsonResponse({'result': False, 'error_msg': f"Hata: {str(e)}"})


# ──────────────────────────────────────────────────────────────────
# 3) EMANET SİL — A-1: Soft-delete + REVERSAL
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_DELETE')
@transaction.atomic
def delete(request):
    """A-1: Manuel silme artık APPEND-ONLY çalışır.

    Eski davranış: `objects.filter(...).delete()` — ledger ve stok karışıyordu.
    Yeni: Her seçili kayıt için
      1) Henüz iptal edilmediyse REVERSAL yazılır (CustodyOffsetService).
      2) Bağlı StockLedger kaydı `cancel_stock_entry` ile geri sarılır.
      3) Orijinal kayıt `is_deleted=True` ile soft-delete yapılır.
    """
    ids = request.POST.getlist('ids[]')
    reason = (request.POST.get('reason') or 'Manuel silme').strip() or 'Manuel silme'
    # FAZ 51 (R-04) — toplu silmede de cascade onay opsiyonu.
    cascade_raw = (request.POST.get('cascade') or '').strip().lower()
    cascade_children = cascade_raw in ('1', 'true', 'yes', 'on')

    if not ids:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'}, status=400)

    audit = extract_audit_context(request)
    rows = CustomerCustodyLedger.objects.filter(id__in=ids, store=request.user.store)

    cancelled = 0
    skipped = []

    for r in rows:
        if r.is_deleted:
            skipped.append(str(r.id))
            continue
        try:
            # IN tipi ve REVERSAL'i olmayanlara REVERSAL yaz.
            if r.custody_type == CustomerCustodyLedger.CUSTODY_IN and not r.has_reversal:
                CustodyOffsetService.reverse_custody_entry(
                    original=r, audit=audit, reason=reason,
                    cascade_children=cascade_children,
                )
                # Stok satırını da geri sar
                try:
                    cancel_stock_entry(
                        ref_type='custody_manual',
                        ref_id=str(r.id),
                        user=request.user,
                        reverse_supplier_ledger=False,
                        notes=f'Emanet manuel iptal — {reason}',
                        raise_if_not_found=False,
                    )
                except Exception as se:
                    logger.warning("custody delete stock revert failed (id=%s): %s", r.id, se)
            else:
                # Diğer tiplerde sadece pasifleştir (denetim izi korunur).
                r.mark_cancelled(user=request.user, reason=reason)

            # Soft-delete bayrağı (zaten REVERSAL pasifleştirdi; ek olarak silinmiş işareti)
            r.refresh_from_db()
            r.is_deleted = True
            r.save(update_fields=['is_deleted'])
            cancelled += 1
        except LedgerError as le:
            skipped.append(f'{r.id}: {le.message}')
        except Exception as ex:
            logger.exception("custody delete failed (id=%s)", r.id)
            skipped.append(f'{r.id}: {ex}')

    return JsonResponse({
        'result': True,
        'cancelled': cancelled,
        'skipped_count': len(skipped),
        'skipped': skipped,
    })


# ──────────────────────────────────────────────────────────────────
# 4) DURUM DEĞİŞTİRME — A-2 idempotent + A-3 append-only kısmi
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CHANGE_STATUS')
@transaction.atomic
def change_status(request):
    """Emanet Kısmi/Tam İade (Teslim) İşlemi.

    A-2: Tam toggle artık idempotent — zaten teslim edilmiş bir kayıt
         tekrar OUT yapılamaz (sınırsız geri-ileri toggle yasak). Geri
         alma için REVERSAL endpoint'i (custody_reverse) kullanılmalı.
    A-3: Kısmi teslimde orijinal IN kaydı MUTATE EDİLMEZ; yeni OUT
         kaydı `parent=IN` referansıyla yazılır (append-only).
    """
    obj_id = request.POST.get('id')
    transfer_val = request.POST.get('transfer_amount')
    delivery_note = (request.POST.get('delivery_note') or '').strip()

    if not obj_id:
        ids = request.POST.getlist('ids[]')
        if not ids:
            return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'})
        rows = CustomerCustodyLedger.objects.filter(
            id__in=ids, store=request.user.store, is_deleted=False, is_active=True,
        )
        for r in rows:
            try:
                _toggle_full_delivery(request, r, delivery_note)
            except Exception as ex:
                logger.exception("toggle batch failed (id=%s)", r.id)
                return JsonResponse({'result': False, 'error_msg': f'{r.id}: {ex}'})
        return JsonResponse({'result': True})

    try:
        row = CustomerCustodyLedger.objects.select_for_update().get(
            id=obj_id, store=request.user.store, is_deleted=False, is_active=True,
        )
    except CustomerCustodyLedger.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt bulunamadı veya iptal edilmiş.'})

    # FAZ 26 — BUG-FIX: Teslim Et erken validasyon.
    # `_toggle_full_delivery` zaten dahili kontrol yapıyor; ancak burada
    # erken çıkış kullanıcıya daha net mesaj döndürür ve FIFO settlement
    # sonrası remaining=0 IN kayıtlarındaki "phantom Teslim Et" denemelerini
    # backend tarafında da bloklar (frontend guard'larına ek bir savunma katmanı).
    if row.custody_type != CustomerCustodyLedger.CUSTODY_IN:
        return JsonResponse({
            'result': False,
            'error_msg': 'Sadece Emanet (IN) kayıtlarından teslim alınabilir. '
                        'Diğer kayıtlar için "İptal" (REVERSAL) kullanılmalıdır.',
        })
    if row.has_reversal:
        return JsonResponse({
            'result': False,
            'error_msg': 'Bu kayıt zaten REVERSAL ile iptal edilmiş; teslim yapılamaz.',
        })
    _remaining_now = row.remaining_amount_hs
    if _remaining_now <= Decimal('0.0005'):
        return JsonResponse({
            'result': False,
            'error_msg': (
                f'Bu emanette teslim edilebilecek kalan bakiye yok. '
                f'(Toplam: {row.amount_hs} HS, Teslim Edilen: {row.delivered_amount_hs} HS)'
            ),
        })

    # Eğer miktar girilmemişse → tam teslim/geri al
    if not transfer_val:
        try:
            _toggle_full_delivery(request, row, delivery_note)
        except Exception as ex:
            return JsonResponse({'result': False, 'error_msg': str(ex)})
        return JsonResponse({'result': True})

    transfer_amount = _decimal_or_zero(transfer_val)
    if transfer_amount <= 0:
        return JsonResponse({'result': False, 'error_msg': 'Miktar 0 dan büyük olmalı.'})

    is_gram_based = row.quantity_gram > 0
    # A-3: Kalan miktar üzerinden validasyon (parent OUT'ları dahil).
    remaining_amount = row.remaining_quantity_gram if is_gram_based else Decimal(row.remaining_quantity_piece)

    if remaining_amount <= 0:
        return JsonResponse({'result': False, 'error_msg': 'Bu emanette kalan miktar yok.'})

    if transfer_amount > remaining_amount + Decimal('0.0005'):
        return JsonResponse({
            'result': False,
            'error_msg': f'Girilen miktar kalan miktardan fazla olamaz. Kalan: {remaining_amount}',
        })

    # Tamı = remaining → tam teslim
    if abs(transfer_amount - remaining_amount) < Decimal('0.0005'):
        try:
            _toggle_full_delivery(request, row, delivery_note)
        except Exception as ex:
            return JsonResponse({'result': False, 'error_msg': str(ex)})
        return JsonResponse({'result': True})

    # ── Kısmi teslim (APPEND-ONLY) ────────────────────────────────
    if row.custody_type != CustomerCustodyLedger.CUSTODY_IN:
        return JsonResponse({'result': False, 'error_msg': 'Sadece IN kayıtlarından kısmi teslim alınabilir.'})

    # Oran kalan üzerinden değil orijinal üzerinden hesaplanır (HS oranı tutarlı kalır).
    ratio_base = row.quantity_gram if is_gram_based else Decimal(row.quantity_piece)
    if ratio_base <= 0:
        return JsonResponse({'result': False, 'error_msg': 'Orijinal miktar geçersiz.'})
    ratio = transfer_amount / ratio_base

    if is_gram_based:
        out_gram = transfer_amount
        out_piece = int(row.quantity_piece * float(ratio))
    else:
        out_piece = int(transfer_amount)
        out_gram = (row.quantity_gram * ratio).quantize(Decimal('0.001'))
    out_amount_hs = (row.amount_hs * ratio).quantize(Decimal('0.001'))

    # Anlık kur (raporlama)
    try:
        hs_data = PriceService.get_price('GOLD_24K')
        current_has_buy_eur = Decimal(str(hs_data.get('buy_tl', '0')))
    except Exception:
        current_has_buy_eur = Decimal('0')
    out_amount_eur = (out_amount_hs * current_has_buy_eur).quantize(Decimal('0.01')) if current_has_buy_eur > 0 else Decimal('0.00')

    new_row = CustomerCustodyLedger.objects.create(
        customer=row.customer, store=row.store, product=row.product,
        custody_type=CustomerCustodyLedger.CUSTODY_OUT,
        quantity_piece=out_piece,
        quantity_gram=out_gram,
        amount_hs=out_amount_hs,
        exchange_rate_eur=current_has_buy_eur,
        amount_eur=out_amount_eur,
        process_no=row.process_no,
        parent=row,  # APPEND-ONLY: orijinal IN'e bağ
        description=(delivery_note or 'Kısmi Teslim')[:255],
        is_returned=True,
        is_active=True, is_deleted=False,
        created_by=request.user, received_by=row.received_by, delivered_by=request.user,
        ip_address=(request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR') or '').split(',')[0].strip() or None,
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:255],
    )

    # A-3: Orijinal IN'e DOKUNULMAZ (mutasyon yasağı). is_returned False kalır;
    # remaining_* property'leri kalan miktarı dinamik hesaplar.

    # --- KISMİ EMANET ÇIKIŞI: STOK HAVUZUNDAN DÜŞÜLÜYOR ---
    if new_row.product:
        try:
            StockService.record_exit(
                product=new_row.product,
                store=request.user.store,
                quantity_gram=new_row.quantity_gram,
                quantity_pieces=new_row.quantity_piece,
                reason=StockLedger.Reason.CUSTODY_OUT,
                ref_type='custody_return',
                ref_id=str(new_row.id),
                user=request.user,
                notes=f"Emanet Kısmi Teslim - Müşteri: {row.customer.first_name}"
            )
        except InsufficientStockError:
            raise Exception(f"Emaneti teslim etmek için havuzda yeterli '{new_row.product.name}' bulunmuyor!")

    return JsonResponse({
        'result': True,
        'row_id': str(new_row.id),
        'remaining_gram': str(row.remaining_quantity_gram),
        'remaining_amount_hs': str(row.remaining_amount_hs),
    })


def _toggle_full_delivery(request, r, delivery_note=''):
    """A-2 + FAZ 24: Tam teslim APPEND-ONLY.

    Kurallar:
      - Kayıt iptal/silinmişse → işlem yapılmaz.
      - Kayıt zaten REVERSAL ile iptal edilmişse → işlem yapılmaz.
      - Backward (Teslim → Emanet) YASAK; REVERSAL kullanılmalı.
      - Forward: orijinal IN kaydı **MUTATE EDİLMEZ**; yeni OUT kaydı
        `parent=r` ile yazılır (FAZ 24 — BUG-3-C). Böylece
        `delivered_amount_hs` doğru toplanır ve aynı IN'den birden
        fazla "tam teslim" denemesi engellenir.
      - Kalan miktar `remaining_amount_hs` üzerinden kontrol edilir;
        eski kayıtlar için legacy fallback property tarafından kapsanır.
    """
    if r.is_deleted or not r.is_active:
        raise Exception('Bu kayıt iptal/silinmiş; toggle yapılamaz.')

    if r.has_reversal:
        raise Exception('Bu kayıt zaten REVERSAL ile iptal edilmiş.')

    # Backward yön (Teslim → Emanet) artık YASAK. REVERSAL kullanılmalı.
    if r.custody_type != CustomerCustodyLedger.CUSTODY_IN or r.is_returned:
        raise Exception(
            'Teslim edilmiş bir emaneti geri almak için "İptal Karşı Girişi" '
            '(REVERSAL) kullanılmalı. Geri alma butonu yerine "İptal" seçin.'
        )

    # FAZ 24 — Kalan kontrolü (legacy fallback dahil)
    remaining_gram = r.remaining_quantity_gram
    remaining_piece = r.remaining_quantity_piece
    remaining_hs = r.remaining_amount_hs
    if remaining_hs <= Decimal('0.0005'):
        raise Exception(
            'Bu emanette teslim edilebilecek bakiye yok. '
            f'(Toplam: {r.amount_hs} HS, Teslim Edilen: {r.delivered_amount_hs} HS)'
        )

    # Anlık kur (raporlama için)
    try:
        hs_data = PriceService.get_price('GOLD_24K')
        current_has_buy_eur = Decimal(str(hs_data.get('buy_tl', '0')))
    except Exception:
        current_has_buy_eur = Decimal('0')
    out_amount_eur = (
        (remaining_hs * current_has_buy_eur).quantize(Decimal('0.01'))
        if current_has_buy_eur > 0 else Decimal('0.00')
    )

    # APPEND-ONLY: yeni OUT kaydı yaz, orijinal IN'e dokunma.
    new_row = CustomerCustodyLedger.objects.create(
        customer=r.customer, store=r.store, product=r.product,
        custody_type=CustomerCustodyLedger.CUSTODY_OUT,
        quantity_piece=remaining_piece if remaining_piece > 0 else r.quantity_piece,
        quantity_gram=remaining_gram if remaining_gram > 0 else r.quantity_gram,
        amount_hs=remaining_hs,
        exchange_rate_eur=current_has_buy_eur,
        amount_eur=out_amount_eur,
        process_no=r.process_no,
        parent=r,
        description=(
            (delivery_note or 'Tam Teslim')
            + (f' | {delivery_note}' if delivery_note and delivery_note != 'Tam Teslim' else '')
        )[:255],
        is_returned=True,
        is_active=True, is_deleted=False,
        created_by=request.user, received_by=r.received_by,
        delivered_by=request.user,
        ip_address=(
            request.META.get('HTTP_X_FORWARDED_FOR')
            or request.META.get('REMOTE_ADDR') or ''
        ).split(',')[0].strip() or None,
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:255],
    )

    if new_row.product:
        try:
            StockService.record_exit(
                product=new_row.product, store=request.user.store,
                quantity_gram=new_row.quantity_gram,
                quantity_pieces=new_row.quantity_piece,
                reason=StockLedger.Reason.CUSTODY_OUT, ref_type='custody_return',
                ref_id=str(new_row.id), user=request.user,
                notes=f"Emanet Tam Teslim - Müşteri: {r.customer.first_name}"
            )
        except InsufficientStockError:
            raise Exception(
                "Müşteriye teslim edilecek yeterli stok (havuzda) bulunmuyor!"
            )
    return new_row


# ──────────────────────────────────────────────────────────────────
# 5) REVERSAL (Yeni endpoint) — A-1/A-2 destekleyici
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CHANGE_STATUS')
@require_POST
@transaction.atomic
def custody_reverse(request):
    """Bir emanet hareketini APPEND-ONLY (REVERSAL) iptal eder.

    POST:
        id        UUID — iptal edilecek kayıt
        reason    str  — neden (zorunlu)
        cascade   '1' / 'true' (opsiyonel) — IN için bağlı çocukları
                  da otomatik reverse et (FAZ 51 R-04). UI önce
                  cascade=false ile dener; servisten "bağlı işlemler
                  mevcut" hatası alırsa kullanıcıya onay sorup tekrar
                  cascade=true ile gönderir.
    """
    obj_id = request.POST.get('id')
    reason = (request.POST.get('reason') or '').strip()
    cascade_raw = (request.POST.get('cascade') or '').strip().lower()
    cascade_children = cascade_raw in ('1', 'true', 'yes', 'on')

    if not obj_id:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt ID zorunludur.'}, status=400)
    if not reason:
        return JsonResponse({'result': False, 'error_msg': 'İptal nedeni zorunludur.'}, status=400)

    try:
        row = CustomerCustodyLedger.objects.select_for_update().get(
            id=obj_id, store=request.user.store, is_deleted=False,
        )
    except CustomerCustodyLedger.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt bulunamadı.'}, status=404)

    audit = extract_audit_context(request)
    try:
        rev = CustodyOffsetService.reverse_custody_entry(
            original=row, audit=audit, reason=reason,
            cascade_children=cascade_children,
        )
    except LedgerError as le:
        # FAZ 51 (R-04) — bağımlı işlem hatası ise cascade önerisi gönder.
        msg = le.message if hasattr(le, 'message') else str(le)
        needs_cascade = (
            row.custody_type == CustomerCustodyLedger.CUSTODY_IN
            and not cascade_children
            and 'bağlı işlemler' in msg.lower()
        )
        return JsonResponse({
            'result': False,
            'error_msg': msg,
            'cascade_available': bool(needs_cascade),
        }, status=400)

    # Stok tarafını da geri sar (IN ise OUT yazılır, OUT ise IN)
    try:
        if row.custody_type == CustomerCustodyLedger.CUSTODY_IN:
            cancel_stock_entry(
                ref_type='custody_manual',
                ref_id=str(row.id),
                user=request.user,
                reverse_supplier_ledger=False,
                notes=f'Emanet REVERSAL — {reason}',
                raise_if_not_found=False,
            )
        elif row.custody_type == CustomerCustodyLedger.CUSTODY_OUT:
            cancel_stock_entry(
                ref_type='custody_return',
                ref_id=str(row.id),
                user=request.user,
                reverse_supplier_ledger=False,
                notes=f'Emanet teslim REVERSAL — {reason}',
                raise_if_not_found=False,
            )
    except Exception as se:
        logger.warning("custody reversal stock revert failed (id=%s): %s", row.id, se)

    return JsonResponse({
        'result': True,
        'reversal_id': str(rev.id),
        'original_id': str(row.id),
    })


# ──────────────────────────────────────────────────────────────────
# 6) DataTable — Liste
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CUSTODY_GET_ALL')
def custody_get_all(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))

    ctype = request.GET.get('custody_type', '')
    is_returned = request.GET.get('is_returned', '')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    search_txt = request.GET.get('search[value]', '')
    show_inactive = request.GET.get('show_inactive', 'false').lower() == 'true'

    qs = (CustomerCustodyLedger.objects
          .filter(store=request.user.store)
          .select_related('customer', 'product', 'created_by', 'parent'))

    # FAZ 22: varsayılan olarak iptal edilmiş kayıtlar gizlenir.
    if not show_inactive:
        qs = qs.filter(is_active=True, is_deleted=False)

    customer_id = request.GET.get("customer_id")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)

    if ctype:
        # B-7: Birden çok tip desteği (virgülle ayrılmış)
        types = [t.strip() for t in ctype.split(',') if t.strip()]
        if len(types) == 1:
            qs = qs.filter(custody_type=types[0])
        else:
            qs = qs.filter(custody_type__in=types)
    if is_returned != '':
        qs = qs.filter(is_returned=(is_returned.lower() == 'true'))
    if date_from:
        qs = qs.filter(created_on__date__gte=parse_date(date_from))
    if date_to:
        qs = qs.filter(created_on__date__lte=parse_date(date_to))
    if search_txt:
        qs = qs.filter(
            Q(customer__first_name__icontains=search_txt) |
            Q(customer__last_name__icontains=search_txt) |
            Q(process_no__icontains=search_txt) |
            Q(description__icontains=search_txt) |
            Q(product__name__icontains=search_txt)
        )

    total = qs.count()
    columns = [None, "customer__first_name", "process_no", "created_by__first_name", "product__name",
               "quantity_piece", "quantity_gram", "amount_hs", "created_on", "is_returned", None]
    col_idx = int(request.GET.get('order[0][column]', 9))
    order_dir = request.GET.get('order[0][dir]', 'desc')
    order_by_field = columns[col_idx] if 0 <= col_idx < len(columns) else "created_on"
    if not order_by_field:
        order_by_field = "created_on"
    order_by = f"-{order_by_field}" if order_dir == 'desc' else order_by_field

    qs = qs.order_by(order_by)[start:start + length] if length != -1 else qs.order_by(order_by)

    data = []
    for r in qs:
        # Kalan miktar (FAZ 22 — A-3): IN için dinamik hesap
        if r.custody_type == CustomerCustodyLedger.CUSTODY_IN:
            remaining_gram = r.remaining_quantity_gram
            remaining_amount_hs = r.remaining_amount_hs
        else:
            remaining_gram = Decimal('0')
            remaining_amount_hs = Decimal('0')

        data.append({
            "id": str(r.id),
            "customer_full": f"{r.customer.first_name} {r.customer.last_name}",
            "customer_id": r.customer.id,
            "product": r.product.name if r.product else "-",
            "product_id": r.product.id if r.product else None,
            "quantity_piece": r.quantity_piece,
            "quantity_gram": f"{r.quantity_gram:.3f}",
            "amount_hs": f"{r.amount_hs:.3f}",
            "amount_eur": f"{r.amount_eur:.2f}",
            "exchange_rate_eur": f"{r.exchange_rate_eur:.6f}",
            "remaining_gram": f"{remaining_gram:.3f}",
            "remaining_amount_hs": f"{remaining_amount_hs:.3f}",
            "process_no": r.process_no or "-",
            "custody_type": r.custody_type,
            "staff_received": _staff_name(r.received_by),
            "staff_delivered": _staff_name(r.delivered_by),
            "staff": _staff_name(r.created_by),
            "description": r.description or "",
            "created_on": r.created_on.strftime("%d/%m/%Y %H:%M"),
            "is_returned": r.is_returned,
            "is_active": r.is_active,
            "is_deleted": r.is_deleted,
            "has_reversal": r.has_reversal,
            "parent_id": str(r.parent_id) if r.parent_id else None,
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data,
    })


# ──────────────────────────────────────────────────────────────────
# 7) FİŞ DATA — B-1/B-2 (tek kayıt, fiş yazdırma için)
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CUSTODY_GET_ALL')
@require_GET
def custody_receipt_data(request, custody_id):
    """Tek emanet kaydı için fiş verisi (yazdırma).

    Response:
      - emanet bilgileri (no, tarih, müşteri, ürün, gram, has, kur, TL)
      - personel bilgileri (alan, teslim eden)
      - mağaza bilgisi
      - kalan/teslim edilen oran (kısmi durumlar için)
    """
    r = get_object_or_404(
        CustomerCustodyLedger.objects.select_related(
            'customer', 'product', 'store', 'created_by',
            'received_by', 'delivered_by', 'parent',
        ),
        pk=custody_id, store=request.user.store,
    )

    store = r.store
    cust = r.customer

    payload = {
        'id': str(r.id),
        'process_no': r.process_no or '',
        'custody_type': r.custody_type,
        'custody_type_label': dict(CustomerCustodyLedger.CUSTODY_TYPE_CHOICES).get(r.custody_type, r.custody_type),
        'created_on': r.created_on.strftime('%d/%m/%Y %H:%M'),
        'created_on_iso': r.created_on.isoformat(),

        'customer': {
            'id': str(cust.id),
            'first_name': cust.first_name or '',
            'last_name': cust.last_name or '',
            'full_name': f"{cust.first_name or ''} {cust.last_name or ''}".strip(),
            'phone': getattr(cust, 'phone', '') or '',
            'tc': getattr(cust, 'tc_no', '') or getattr(cust, 'tc', '') or '',
        },

        'store': {
            'id': str(store.id),
            'name': getattr(store, 'name', '') or '',
            'address': getattr(store, 'address', '') or '',
            'phone': getattr(store, 'phone', '') or '',
        },

        'product': {
            'id': str(r.product.id) if r.product else None,
            'name': r.product.name if r.product else '',
            'mileage': str(getattr(r.product, 'product_mileage', '') or ''),
            'is_gram_bullion': bool(getattr(r.product, 'is_gram_bullion', False)) if r.product else False,
        },

        'amounts': {
            'quantity_piece': r.quantity_piece,
            'quantity_gram': f"{r.quantity_gram:.3f}",
            'amount_hs': f"{r.amount_hs:.3f}",
            'amount_eur': f"{r.amount_eur:.2f}",
            'exchange_rate_eur': f"{r.exchange_rate_eur:.6f}",
        },

        'remaining': {
            'gram': f"{r.remaining_quantity_gram:.3f}",
            'piece': r.remaining_quantity_piece,
            'amount_hs': f"{r.remaining_amount_hs:.3f}",
        },

        'staff': {
            'received_by': _staff_name(r.received_by),
            'delivered_by': _staff_name(r.delivered_by),
            'created_by': _staff_name(r.created_by),
        },

        'flags': {
            'is_returned': r.is_returned,
            'is_active': r.is_active,
            'is_deleted': r.is_deleted,
            'has_reversal': r.has_reversal,
        },

        'description': r.description or '',
        'reverse_reason': r.reverse_reason or '',
    }

    return JsonResponse({'result': True, 'data': payload})


# ──────────────────────────────────────────────────────────────────
# 8) MÜŞTERİ EKSTRESİ — B-3/B-5/B-6
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def customer_custody_history(request, customer_id):
    """Müşteri bazlı emanet ekstresi.

    Query:
      ?date_from=YYYY-MM-DD
      ?date_to=YYYY-MM-DD
      ?include_inactive=true → iptal/silinmiş kayıtları da göster

    Response:
      summary: { in_total_hs, out_total_hs, offset_total_hs,
                 reversal_effect_hs, balance_hs, current_balance_eur }
      rows: [ { id, custody_type, ... } ]
    """
    store = request.user.store
    customer = get_object_or_404(Customers, pk=customer_id, store=store)

    include_inactive = request.GET.get('include_inactive', 'false').lower() == 'true'
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    qs = CustomerCustodyLedger.objects.filter(
        customer=customer, store=store,
    ).select_related('product', 'created_by', 'received_by', 'delivered_by', 'parent', 'related_ledger')

    if not include_inactive:
        qs = qs.filter(is_active=True, is_deleted=False)

    if date_from:
        qs = qs.filter(created_on__date__gte=parse_date(date_from))
    if date_to:
        qs = qs.filter(created_on__date__lte=parse_date(date_to))

    qs = qs.order_by('-created_on')

    rows = []
    in_total = Decimal('0')
    out_total = Decimal('0')
    offset_total = Decimal('0')
    reversal_effect = Decimal('0')

    for r in qs:
        if r.is_active and not r.is_deleted:
            if r.custody_type == CustomerCustodyLedger.CUSTODY_IN and not r.is_returned:
                in_total += r.amount_hs
            elif r.custody_type == CustomerCustodyLedger.CUSTODY_OUT:
                out_total += r.amount_hs
            elif r.custody_type == CustomerCustodyLedger.CUSTODY_OFFSET:
                offset_total += r.amount_hs
            elif r.custody_type == CustomerCustodyLedger.CUSTODY_REVERSAL:
                reversal_effect += r.signed_amount_hs

        rows.append({
            'id': str(r.id),
            'custody_type': r.custody_type,
            'custody_type_label': dict(CustomerCustodyLedger.CUSTODY_TYPE_CHOICES).get(r.custody_type, r.custody_type),
            'product': r.product.name if r.product else '-',
            'process_no': r.process_no or '',
            'quantity_piece': r.quantity_piece,
            'quantity_gram': f"{r.quantity_gram:.3f}",
            'amount_hs': f"{r.amount_hs:.3f}",
            'amount_eur': f"{r.amount_eur:.2f}",
            'exchange_rate_eur': f"{r.exchange_rate_eur:.6f}",
            'remaining_gram': f"{r.remaining_quantity_gram:.3f}" if r.custody_type == CustomerCustodyLedger.CUSTODY_IN else '0.000',
            'remaining_amount_hs': f"{r.remaining_amount_hs:.3f}" if r.custody_type == CustomerCustodyLedger.CUSTODY_IN else '0.000',
            'description': r.description or '',
            'reverse_reason': r.reverse_reason or '',
            'is_returned': r.is_returned,
            'is_active': r.is_active,
            'is_deleted': r.is_deleted,
            'has_reversal': r.has_reversal,
            'parent_id': str(r.parent_id) if r.parent_id else None,
            'related_ledger_id': str(r.related_ledger_id) if r.related_ledger_id else None,
            'staff_received': _staff_name(r.received_by),
            'staff_delivered': _staff_name(r.delivered_by),
            'created_by': _staff_name(r.created_by),
            'created_on': r.created_on.strftime('%d/%m/%Y %H:%M'),
            'created_on_iso': r.created_on.isoformat(),
        })

    balance_hs = (in_total - out_total - offset_total + reversal_effect).quantize(Decimal('0.001'))

    # Bugünkü kurla TL bakiye
    from apps.banking.exchange_rate_service import get_current_has_rate
    current_rate = get_current_has_rate(store) or Decimal('0')
    current_balance_eur = (balance_hs * current_rate).quantize(Decimal('0.01'))

    summary = {
        'in_total_hs': f"{in_total:.3f}",
        'out_total_hs': f"{out_total:.3f}",
        'offset_total_hs': f"{offset_total:.3f}",
        'reversal_effect_hs': f"{reversal_effect:.3f}",
        'balance_hs': f"{balance_hs:.3f}",
        'current_rate': f"{current_rate:.6f}",
        'current_balance_eur': f"{current_balance_eur:.2f}",
        'row_count': len(rows),
    }

    return JsonResponse({
        'result': True,
        'customer': {
            'id': str(customer.id),
            'full_name': f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
            'phone': getattr(customer, 'phone', '') or '',
        },
        'summary': summary,
        'rows': rows,
    })


# ──────────────────────────────────────────────────────────────────
# 8.1) FAZ 24 — BUG-1: process_no bazlı emanet hareketi detayı
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def custody_by_process_no(request, process_no):
    """Bir process_no'ya ait tüm emanet hareketleri (CUS prefix dahil).

    BUG-1 fix: Emanet sayfasındaki "İşlem No"ya tıklama eskiden
    /process/get-process-details çağırıyordu; CUS-prefixli emanet
    numaraları orada bulunmadığı için hata alınıyordu. Bu endpoint
    aynı process_no'ya bağlı tüm CCL satırlarını döndürür.
    """
    store = request.user.store
    process_no = (process_no or '').strip()
    if not process_no:
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz işlem no.'}, status=400)

    qs = (CustomerCustodyLedger.objects
          .filter(store=store, process_no=process_no)
          .select_related('customer', 'product', 'created_by',
                          'received_by', 'delivered_by', 'parent')
          .order_by('created_on'))

    if not qs.exists():
        return JsonResponse({'result': True, 'entries': [], 'summary': {}})

    entries = []
    in_total = Decimal('0')
    out_total = Decimal('0')
    offset_total = Decimal('0')
    reversal_effect = Decimal('0')

    cust = None
    for r in qs:
        if cust is None:
            cust = r.customer
        if r.is_active and not r.is_deleted:
            if r.custody_type == CustomerCustodyLedger.CUSTODY_IN and not r.is_returned:
                in_total += r.amount_hs
            elif r.custody_type == CustomerCustodyLedger.CUSTODY_OUT:
                out_total += r.amount_hs
            elif r.custody_type == CustomerCustodyLedger.CUSTODY_OFFSET:
                offset_total += r.amount_hs
            elif r.custody_type == CustomerCustodyLedger.CUSTODY_REVERSAL:
                reversal_effect += r.signed_amount_hs

        entries.append({
            'id': r.id,
            'custody_type': r.custody_type,
            'custody_type_label': dict(
                CustomerCustodyLedger.CUSTODY_TYPE_CHOICES,
            ).get(r.custody_type, r.custody_type),
            'customer_full': f"{r.customer.first_name or ''} {r.customer.last_name or ''}".strip(),
            'customer_phone': getattr(r.customer, 'phone', '') or '',
            'product': r.product.name if r.product else '-',
            'quantity_piece': r.quantity_piece,
            'quantity_gram': f"{r.quantity_gram:.3f}",
            'amount_hs': f"{r.amount_hs:.3f}",
            'amount_eur': f"{r.amount_eur:.2f}",
            'is_returned': r.is_returned,
            'is_active': r.is_active,
            'is_deleted': r.is_deleted,
            'has_reversal': r.has_reversal,
            'description': r.description or '',
            'created_by': _staff_name(r.created_by),
            'created_on': r.created_on.strftime('%d/%m/%Y %H:%M'),
        })

    balance = (in_total - out_total - offset_total + reversal_effect).quantize(Decimal('0.001'))
    return JsonResponse({
        'result': True,
        'process_no': process_no,
        'entries': entries,
        'summary': {
            'in_total_hs': f"{in_total:.3f}",
            'out_total_hs': f"{out_total:.3f}",
            'offset_total_hs': f"{offset_total:.3f}",
            'reversal_effect_hs': f"{reversal_effect:.3f}",
            'balance_hs': f"{balance:.3f}",
            'row_count': len(entries),
        },
    })


# ──────────────────────────────────────────────────────────────────
# 9) MAHSUPLAŞMA KUR FARKI ÖNİZLEMESİ — B-8
# ──────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────
# 9.4) FAZ 24 — GEREKSİNİM-2: Emanetten Stoğa Transfer
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CHANGE_STATUS')
@require_POST
@transaction.atomic
def custody_to_stock_transfer(request):
    """Emanet IN kaydını (kısmen veya tamamen) serbest stoğa al.

    Akış (CustodyToStockService):
      1) CCL'ye STOCK kaydı yazılır (parent=IN)
      2) StockLedger'a CUSTODY_2_STK denetim kaydı (net etki: 0)
      3) CustomerLedger'a DEBT (Has) yazılır

    POST:
      id              int — IN kaydı id'si
      quantity_gram   Decimal | '' (boşsa kalanın tamamı)
      quantity_piece  int | ''
      description     str (opsiyonel)
      write_debt      'true' | 'false' (default: 'true')
    """
    obj_id = request.POST.get('id')
    if not obj_id:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt ID zorunludur.'}, status=400)

    try:
        custody_in = CustomerCustodyLedger.objects.select_for_update().get(
            id=obj_id, store=request.user.store,
            is_active=True, is_deleted=False,
        )
    except CustomerCustodyLedger.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt bulunamadı veya pasif.'}, status=404)

    qty_gram_raw = request.POST.get('quantity_gram', '').strip()
    qty_piece_raw = request.POST.get('quantity_piece', '').strip()
    description = (request.POST.get('description') or '').strip()
    write_debt = (request.POST.get('write_debt', 'true').lower() != 'false')

    qty_gram = _decimal_or_zero(qty_gram_raw) if qty_gram_raw else None
    try:
        qty_piece = int(qty_piece_raw) if qty_piece_raw else None
    except (ValueError, TypeError):
        qty_piece = None

    audit = extract_audit_context(request)
    try:
        result = CustodyToStockService.transfer(
            custody_in=custody_in,
            quantity_gram=qty_gram,
            quantity_piece=qty_piece,
            audit=audit,
            description=description,
            write_customer_debt=write_debt,
        )
    except InsufficientCustodyError as e:
        return JsonResponse({
            'result': False,
            'error_msg': f'Yetersiz emanet: {e.available} mevcut, {e.requested} talep.',
        }, status=400)
    except InvalidLedgerStateError as e:
        return JsonResponse({'result': False, 'error_msg': e.message}, status=400)
    except Exception as ex:
        logger.exception("custody to stock transfer failed")
        return JsonResponse({'result': False, 'error_msg': str(ex)}, status=400)

    return JsonResponse({
        'result': True,
        'custody_id': result.custody_entry.id,
        'transferred_hs': str(result.transferred_hs),
        'transferred_gram': str(result.transferred_gram),
        'transferred_piece': result.transferred_piece,
        'ledger_id': str(result.ledger_entry.id) if result.ledger_entry else None,
    })


# ──────────────────────────────────────────────────────────────────
# 9.4.b) FAZ 51 (R-07) — Emanetten Stoğa Transfer Geri Alma
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CHANGE_STATUS')
@require_POST
@transaction.atomic
def custody_to_stock_reverse(request):
    """Bir Emanet→Stok transfer kaydını atomik olarak geri al.

    POST:
      id      int — CUSTODY_STOCK_TRANSFER kaydı id'si
      reason  str — neden (zorunlu)

    Servis (CustodyToStockService.reverse_transfer) üç tabloyu birden
    geri sarar:
      1) CCL: REVERSAL kaydı yazılır + orijinal pasifleştirilir
      2) StockLedger: in/out bacaklarının her ikisi de cancel_stock_entry
         ile geri çekilir
      3) CustomerLedger: write_credit ile yazılan müşteri alacağı reverse
         edilir (propagate kasa hareketi tetiklemez — CREDIT kasa üretmemişti).

    Hata olursa @transaction.atomic tüm değişiklikleri geri alır.
    """
    obj_id = request.POST.get('id')
    reason = (request.POST.get('reason') or '').strip()

    if not obj_id:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt ID zorunludur.'}, status=400)
    if not reason:
        return JsonResponse({'result': False, 'error_msg': 'Geri alma nedeni zorunludur.'}, status=400)

    try:
        transfer_entry = CustomerCustodyLedger.objects.select_for_update().get(
            id=obj_id, store=request.user.store, is_deleted=False,
            custody_type=CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER,
        )
    except CustomerCustodyLedger.DoesNotExist:
        return JsonResponse({
            'result': False,
            'error_msg': 'Stoğa transfer kaydı bulunamadı.',
        }, status=404)

    audit = extract_audit_context(request)
    try:
        result = CustodyToStockService.reverse_transfer(
            transfer_entry=transfer_entry,
            audit=audit,
            reason=reason,
        )
    except InvalidLedgerStateError as e:
        return JsonResponse({'result': False, 'error_msg': e.message}, status=400)
    except Exception as ex:
        logger.exception("custody to stock reverse failed (id=%s)", obj_id)
        return JsonResponse({'result': False, 'error_msg': str(ex)}, status=400)

    return JsonResponse({
        'result': True,
        'transfer_id': str(transfer_entry.id),
        'custody_reversal_id': result.get('custody_reversal_id'),
        'ledger_reversal_id': result.get('ledger_reversal_id'),
        'stock_reverted': bool(result.get('stock_reverted')),
    })


# ──────────────────────────────────────────────────────────────────
# 9.45) FAZ 26 — Settlement FIFO Helper
# ──────────────────────────────────────────────────────────────────

def _consume_fifo_outs(
    *, customer, store, amount_hs, custody_out_type,
    process_no, description, audit, current_rate,
):
    """FIFO: aktif IN kayıtlarını HS bazında tüket, her IN için ayrı OUT yaz.

    FAZ 26 — Bug Fix:
      Önceki tasarımda settlement view tek bir CCL satırı yazıyordu
      (`parent=None`, `process_no='STL-...'`). Bu satır per-row
      `remaining_amount_hs` hesabına GİRMİYORDU; çünkü:
        - `parent=None` → `delivered_amount_hs` primary sorgusu görmüyor
        - `process_no='STL-...'` → `_legacy_outs_qs()` (aynı CUS process_no
          gerektiriyor) görmüyor
      Sonuç: settlement sonrası DataTable'da IN kaydının remaining'i hâlâ
      tam görünüyor → "Teslim Et" butonu yanlışlıkla aktif kalıyor →
      kullanıcı tıklayınca StockService InsufficientStockError fırlatıyor.

    Bu helper aktif IN kayıtlarını tarihe göre (en eski → en yeni) sırayla
    tüketir, her IN için kalanı kadarını yazıp `parent=in_row` bağlar.
    Böylece her settlement OUT'u doğru IN'in altına düşer ve `remaining_*`
    property'leri tutarlı kalır.

    Args:
        customer, store: Müşteri ve mağaza
        amount_hs: Toplam tüketilecek HS miktarı
        custody_out_type: CUSTODY_OUT veya CUSTODY_OFFSET
        process_no: Settlement process_no (genellikle 'STL-...')
        description: OUT satırlarına yazılacak açıklama
        audit: extract_audit_context() çıktısı
        current_rate: Anlık kur (1 gr Has = X TL)

    Returns:
        Yaratılan CCL satırlarının listesi (en az 1 eleman).

    Raises:
        InsufficientCustodyError: Aktif IN'lerin toplam kalan bakiyesi
            istenen miktardan azsa.
    """
    remaining = Decimal(amount_hs)
    created = []

    active_ins = (
        CustomerCustodyLedger.objects
        .filter(
            customer=customer, store=store,
            custody_type=CustomerCustodyLedger.CUSTODY_IN,
            is_active=True, is_deleted=False,
        )
        .order_by('created_on')
    )

    for in_row in active_ins:
        if remaining <= Decimal('0.0005'):
            break
        row_rem = in_row.remaining_amount_hs
        if row_rem <= Decimal('0.0005'):
            continue

        consume_hs = min(remaining, row_rem)
        if in_row.amount_hs and in_row.amount_hs > 0:
            ratio = consume_hs / in_row.amount_hs
        else:
            ratio = Decimal('1')

        consume_gram = (in_row.quantity_gram * ratio).quantize(Decimal('0.001'))
        try:
            consume_piece = int(round(float(in_row.quantity_piece) * float(ratio)))
        except (ValueError, TypeError):
            consume_piece = 0

        amount_eur = (
            (consume_hs * current_rate).quantize(Decimal('0.01'))
            if current_rate and current_rate > 0 else Decimal('0.00')
        )

        out = CustomerCustodyLedger.objects.create(
            customer=customer,
            store=store,
            product=in_row.product,
            custody_type=custody_out_type,
            quantity_piece=consume_piece,
            quantity_gram=consume_gram,
            amount_hs=consume_hs,
            exchange_rate_eur=current_rate or Decimal('0'),
            amount_eur=amount_eur,
            process_no=process_no,
            parent=in_row,
            description=(description or '')[:255],
            is_returned=True,
            is_active=True, is_deleted=False,
            created_by=audit.get('actor'),
            delivered_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=(audit.get('user_agent') or '')[:255],
        )
        created.append(out)
        remaining -= consume_hs

    if remaining > Decimal('0.0005'):
        raise InsufficientCustodyError(
            available=Decimal(amount_hs) - remaining,
            requested=Decimal(amount_hs),
        )

    if not created:
        raise InsufficientCustodyError(
            available=Decimal('0'),
            requested=Decimal(amount_hs),
        )

    return created


# ──────────────────────────────────────────────────────────────────
# 9.5) FAZ 24 — GEREKSİNİM-3: Multi-Method Settlement
# ──────────────────────────────────────────────────────────────────

@login_required
@role_required('CUSTODY_CHANGE_STATUS')
@require_POST
@transaction.atomic
def custody_settlement(request):
    """Emanet kapatma — çoklu yöntemli (ürün takası / döviz / nakit / cari).

    Bu endpoint gelen ödeme yönteminin türüne göre alt akışları çağırır.
    Şu anki kapsam:
      - method='offset'   → CustodyOffsetService.offset_custody_to_ledger
      - method='cash'     → CustomerLedger COLLECTION_TL + CashboxLedger
                            (mağaza kasasına nakit girer, müşterinin
                             cari hesabı CUSTODY_OFFSET olarak kapanır)
      - method='fx'       → CustomerLedger COLLECTION_FX + döviz kasası
      - method='product'  → emanet OUT (kısmi/tam) + farkı CustomerLedger'a
                            yansıt (DEBT veya CREDIT)

    POST:
      customer_id   UUID
      method        'offset' | 'cash' | 'fx' | 'product'
      amount_hs     Decimal — kapatılacak Has miktarı
      ...method'a özel ek parametreler (currency, fx_rate, product_id, ...)

    Bu endpoint atomik; herhangi bir adım fail olursa tümü geri sarılır.
    """
    method = (request.POST.get('method') or '').strip().lower()
    customer_id = request.POST.get('customer_id')
    amount_hs = _decimal_or_zero(request.POST.get('amount_hs', '0'))

    if not customer_id:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri zorunludur.'}, status=400)
    if amount_hs <= 0:
        return JsonResponse({'result': False, 'error_msg': 'amount_hs pozitif olmalı.'}, status=400)
    if method not in ('offset', 'cash', 'fx', 'product'):
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz yöntem.'}, status=400)

    try:
        customer = Customers.objects.get(pk=customer_id, store=request.user.store)
    except Customers.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri bulunamadı.'}, status=404)

    audit = extract_audit_context(request)
    description = (request.POST.get('description') or '').strip()

    try:
        if method == 'offset':
            res = CustodyOffsetService.offset_custody_to_ledger(
                customer=customer, store=request.user.store,
                amount_hs=amount_hs, audit=audit,
                description=description or 'Cari mahsuplaşma',
            )
            return JsonResponse({
                'result': True,
                'method': 'offset',
                'custody_id': res.custody_entry.id,
                'ledger_id': str(res.ledger_entry.id),
                'fx_diff_tl': str(res.fx_diff_tl),
                'fx_entry_id': str(res.fx_entry.id) if res.fx_entry else None,
                'new_custody_balance_hs': str(res.new_custody_balance_hs),
                'new_ledger_balance_hs': str(res.new_ledger_balance_hs),
            })

        # ── CASH / FX / PRODUCT ortak ön kontroller ─────────────
        if amount_hs > get_custody_balance_hs(customer):
            return JsonResponse({
                'result': False,
                'error_msg': 'Mahsuplaşma miktarı emanet bakiyesinden büyük.',
            }, status=400)

        # CUSTODY OUT yaz (ortak)
        try:
            hs_data = PriceService.get_price('GOLD_24K')
            current_rate = Decimal(str(hs_data.get('buy_tl', '0')))
        except Exception:
            current_rate = Decimal('0')
        amount_eur_today = (amount_hs * current_rate).quantize(Decimal('0.01')) if current_rate > 0 else Decimal('0.00')

        process_no = f'STL-{timezone.now().strftime("%Y%m%d%H%M%S")}'

        # FAZ 26 — BUG-FIX: Tek satırlı parent=None CUSTODY_OUT yerine FIFO
        # ile aktif IN kayıtlarını sırayla tüket. Her OUT satırı doğru IN'in
        # altına düşer (parent FK), böylece DataTable'daki per-row
        # remaining_amount_hs settlement sonrası doğru değerini gösterir.
        try:
            fifo_outs = _consume_fifo_outs(
                customer=customer,
                store=request.user.store,
                amount_hs=amount_hs,
                custody_out_type=CustomerCustodyLedger.CUSTODY_OUT,
                process_no=process_no,
                description=(description or f'Settlement — {method}')[:255],
                audit=audit,
                current_rate=current_rate,
            )
        except InsufficientCustodyError as ice:
            raise InvalidLedgerStateError(
                f'Yetersiz emanet bakiyesi: mevcut {ice.available} HS, '
                f'talep {ice.requested} HS.',
            )
        # Response için ilk satırın referansı (UI tarafında çoklu satır
        # listesi gerekirse fifo_outs'tan alınabilir).
        custody_out = fifo_outs[0]

        if method in ('cash', 'fx'):
            # Müşteri emanetini nakit (TL) veya döviz olarak alır.
            # CollectionService.collect_and_close akışı kullanılır;
            # bank_account zorunludur.
            from apps.banking.models import BankAccount
            from apps.customers.services.collection import CollectionService

            bank_account_id = request.POST.get('bank_account_id')
            if not bank_account_id:
                raise InvalidLedgerStateError('Kasa seçimi zorunlu.')
            try:
                bank_account = BankAccount.objects.get(
                    pk=bank_account_id, store=request.user.store,
                )
            except BankAccount.DoesNotExist:
                raise InvalidLedgerStateError('Kasa bulunamadı.')

            if method == 'cash':
                payment_currency = 'TRY'
                payment_amount = _decimal_or_zero(request.POST.get('cash_amount_eur', '0'))
                if payment_amount <= 0:
                    payment_amount = amount_eur_today
            else:
                payment_currency = (request.POST.get('currency') or 'USD').upper()
                payment_amount = _decimal_or_zero(request.POST.get('fx_amount', '0'))
                if payment_amount <= 0:
                    raise InvalidLedgerStateError('Döviz miktarı zorunlu.')

            try:
                col = CollectionService.collect_and_close(
                    customer=customer, store=request.user.store,
                    bank_account=bank_account,
                    payment_amount=payment_amount,
                    payment_currency=payment_currency,
                    audit=audit,
                    process_no=process_no,
                )
            except InvalidLedgerStateError:
                raise
            except Exception as ex:
                logger.exception("settlement %s failed", method)
                raise InvalidLedgerStateError(f'Tahsilat hatası: {ex}')

            return JsonResponse({
                'result': True, 'method': method,
                'custody_id': custody_out.id,
                'payment_id': str(col.payment.id) if col and getattr(col, 'payment', None) else None,
                'ledger_id': str(col.collection_entry.id) if col and getattr(col, 'collection_entry', None) else None,
            })

        elif method == 'product':
            # Müşteri emanetini başka bir ürün karşılığında almak istiyor.
            # Ürün satışı normal Process flow üzerinden yapılır; bu endpoint
            # sadece emanet OUT kaydını oluşturup farkı CustomerLedger'a
            # CREDIT olarak yazar (müşterinin alacağı).
            from apps.customers.services.ledger import LedgerService
            credit_entry = LedgerService.write_credit(
                customer=customer, store=request.user.store,
                amount_hs=amount_hs,
                process_no=process_no,
                audit=audit,
                description=(description or 'Emanet karşılığı satış öncesi alacak')[:255],
            )
            return JsonResponse({
                'result': True, 'method': 'product',
                'custody_id': custody_out.id,
                'credit_id': str(credit_entry.id),
                'note': (
                    'Müşteri alacağı oluşturuldu. Şimdi normal satış '
                    'akışını başlatın; satış borcu bu alacak ile mahsup '
                    'edilecektir.'
                ),
            })

    except InvalidLedgerStateError as e:
        return JsonResponse({'result': False, 'error_msg': e.message}, status=400)
    except Exception as ex:
        logger.exception("settlement failed")
        return JsonResponse({'result': False, 'error_msg': str(ex)}, status=500)


@login_required
@require_GET
def custody_settlement_preview(request):
    """Settlement öncesi özet bilgi — kalan emanet, açık borç, kur.

    Query:
      customer_id  UUID
      amount_hs    Decimal (opsiyonel; boşsa sadece bakiye döner)
    """
    customer_id = request.GET.get('customer_id')
    if not customer_id:
        return JsonResponse({'result': False, 'error_msg': 'customer_id gerekli.'}, status=400)
    try:
        customer = Customers.objects.get(pk=customer_id, store=request.user.store)
    except Customers.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri bulunamadı.'}, status=404)

    from apps.banking.exchange_rate_service import get_current_has_rate
    from apps.customers.services.ledger import LedgerService
    from apps.customers.services.custody_offset import preview_offset_fx_diff

    custody_balance = get_custody_balance_hs(customer)
    open_balance = LedgerService.get_open_balance_hs(customer)
    rate = get_current_has_rate(request.user.store) or Decimal('0')

    amount_hs_raw = request.GET.get('amount_hs', '').strip()
    fx_preview = None
    if amount_hs_raw:
        amt = _decimal_or_zero(amount_hs_raw)
        if amt > 0:
            fx_preview = preview_offset_fx_diff(customer, request.user.store, amt)

    return JsonResponse({
        'result': True,
        'customer': {
            'id': str(customer.id),
            'full_name': f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
        },
        'custody_balance_hs': str(custody_balance),
        'open_balance_hs': str(open_balance),
        'current_rate_tl': str(rate),
        'custody_balance_eur_today': str((custody_balance * rate).quantize(Decimal('0.01'))),
        'open_balance_eur_today': str((open_balance * rate).quantize(Decimal('0.01'))),
        'fx_preview': fx_preview,
    })


# ──────────────────────────────────────────────────────────────────
# 9.5) FAZ 24 — GEREKSİNİM-1: Mağaza geneli emanet özeti
# ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def custody_store_summary(request):
    """Mağazadaki tüm aktif emanetlerin agregat özeti.

    Response:
      totals: { customer_count, entry_count, total_hs, total_tl_today }
      by_category: [{ category, count, total_hs, total_tl_today }]
      top_customers: [{ customer_id, full_name, balance_hs }]
      oldest_entries: [{ id, customer, product, days_open, amount_hs }]
    """
    from django.db.models import Count
    from apps.banking.exchange_rate_service import get_current_has_rate

    store = request.user.store
    now = timezone.now()

    # Aktif IN kayıtları (kalan miktar > 0 olanlar — dinamik filtre değil,
    # is_returned=False yani FAZ 23 öncesi toggle pattern'i ile teslim
    # edilmiş kayıtlar dahil değil).
    base_qs = CustomerCustodyLedger.objects.filter(
        store=store,
        custody_type=CustomerCustodyLedger.CUSTODY_IN,
        is_active=True,
        is_deleted=False,
        is_returned=False,
    )

    # Brüt giriş toplamı (orijinal IN amount_hs sum'ı) — raporda
    # `gross_hs` olarak sunulur; net kalandan ayrı bir göstergedir.
    gross_agg = base_qs.aggregate(
        gross_total_hs=Coalesce(Sum('amount_hs'), Decimal('0')),
    )

    # FAZ 27 — BUG-FIX (Senaryo 1 / Hata 2):
    #   Append-only mimaride orijinal IN kaydının is_returned alanı
    #   teslim sonrası DA False kalır (mutasyon yasağı / A-3 kuralı).
    #   Eski kodda Count('id') agregasyonu fully-delivered IN'leri de
    #   "Aktif Emanet" olarak sayıyordu. Çözüm: aktif sayım ve müşteri
    #   sayısı, net kalan hesabıyla aynı tek-geçiş döngüsünden türetilir;
    #   yalnız remaining_amount_hs > 0.0005 (HS tolerance) olan IN'ler
    #   "aktif" kabul edilir. Brüt toplam (gross_hs) etkilenmez.
    active_count = 0
    net_remaining_hs = Decimal('0')
    active_customer_ids = set()
    for r in base_qs.iterator():
        rem = r.remaining_amount_hs
        if rem > Decimal('0.0005'):
            active_count += 1
            net_remaining_hs += rem
            active_customer_ids.add(r.customer_id)

    agg = {
        'entry_count': active_count,
        'total_hs': gross_agg['gross_total_hs'] or Decimal('0'),
    }

    customer_count = len(active_customer_ids)

    # Bugünkü kur (TL karşılığı)
    current_rate = get_current_has_rate(store) or Decimal('0')
    total_tl_today = (net_remaining_hs * current_rate).quantize(Decimal('0.01'))

    # Kategori kırılımı
    by_category = []
    cat_qs = (base_qs
              .values('product__category__name')
              .annotate(count=Count('id'),
                        total_hs=Coalesce(Sum('amount_hs'), Decimal('0')))
              .order_by('-total_hs'))
    for c in cat_qs:
        cat_name = c['product__category__name'] or 'Kategorisiz'
        cat_hs = c['total_hs'] or Decimal('0')
        by_category.append({
            'category': cat_name,
            'count': c['count'],
            'total_hs': f"{cat_hs:.3f}",
            'total_tl_today': f"{(cat_hs * current_rate).quantize(Decimal('0.01'))}",
        })

    # En yüksek bakiyeli ilk 5 müşteri
    top_customers_qs = (base_qs
                        .values('customer_id',
                                'customer__first_name',
                                'customer__last_name')
                        .annotate(balance_hs=Coalesce(Sum('amount_hs'),
                                                      Decimal('0')))
                        .order_by('-balance_hs')[:5])
    top_customers = []
    for c in top_customers_qs:
        full_name = f"{c['customer__first_name'] or ''} {c['customer__last_name'] or ''}".strip()
        top_customers.append({
            'customer_id': str(c['customer_id']),
            'full_name': full_name or '-',
            'balance_hs': f"{c['balance_hs']:.3f}",
        })

    # En uzun süredir bekleyen 5 emanet
    oldest_qs = (base_qs
                 .select_related('customer', 'product')
                 .order_by('created_on')[:5])
    oldest_entries = []
    for r in oldest_qs:
        delta_days = (now - r.created_on).days
        oldest_entries.append({
            'id': r.id,
            'customer': f"{r.customer.first_name or ''} {r.customer.last_name or ''}".strip(),
            'product': r.product.name if r.product else '-',
            'days_open': delta_days,
            'amount_hs': f"{r.amount_hs:.3f}",
            'created_on': r.created_on.strftime('%d/%m/%Y'),
        })

    return JsonResponse({
        'result': True,
        'totals': {
            'customer_count': customer_count,
            'entry_count': agg['entry_count'] or 0,
            'gross_hs': f"{agg['total_hs']:.3f}",
            'net_remaining_hs': f"{net_remaining_hs:.3f}",
            'current_rate_tl': f"{current_rate:.6f}",
            'total_tl_today': f"{total_tl_today:.2f}",
        },
        'by_category': by_category,
        'top_customers': top_customers,
        'oldest_entries': oldest_entries,
    })


@login_required
@require_GET
def custody_offset_preview(request, customer_id):
    """Mahsuplaşma öncesi kur farkı hesabı (yazma yapmadan).

    Query:
      amount_hs   Decimal — mahsuplaşılacak Has miktarı

    Response:
      custody_avg_rate, current_rate,
      amount_eur_at_custody, amount_eur_at_current,
      fx_diff_tl, fx_direction (GAIN | LOSS | FLAT)
    """
    from apps.customers.services.custody_offset import preview_offset_fx_diff
    store = request.user.store
    customer = get_object_or_404(Customers, pk=customer_id, store=store)
    amount_hs = _decimal_or_zero(request.GET.get('amount_hs', '0'))
    if amount_hs <= 0:
        return JsonResponse({'result': False, 'error_msg': 'amount_hs gerekli.'}, status=400)
    data = preview_offset_fx_diff(customer, store, amount_hs)
    return JsonResponse({'result': True, 'data': data})
