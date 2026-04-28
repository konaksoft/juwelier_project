# apps/bracelets/views.py
# ============================================================================
# BİLEZİK MODÜLÜ — ONARIM FAZLARI B-1 / B-2 / B-3 / B-4 / B-6 (2026-04-27)
# ----------------------------------------------------------------------------
# Hurda modülünde 10 fazlık onarım sürecinde elde edilen mimari standartlar
# bilezik modülüne aynen yansıtıldı. Kısa özet:
#
#   B-Faz 1: Havuz anahtarı `name + exact milyem` → SADECE `name`. Farklı
#            milyemler ağırlıklı ortalama ile aynı havuza akar.
#   B-Faz 2: Her giriş için BENZERSİZ `bp_process_no`; StockLedger.ref_id =
#            SupplierLedger.process_no = Process.process_no. İptal akışı
#            artık `cancel_stock_entry(ref_type='bracelet_add', ...)` ile
#            tek atomik olarak çalışır. Products.gram düşürmesinde
#            `Greatest(F('gram') - x, 0)` zemin koruması.
#   B-Faz 3: `update_bracelet_pool_weighted_mileage` (Hurda Faz 5 eşleniği)
#            yeni gram eklerken WAC milyem hesaplar ve atomic UPDATE ile
#            yazar. `bracelet_add` UPDATE dalı `p.save()` yerine
#            `Products.objects.filter().update()` kullanır → full_clean tuzağı
#            atlatılır.
#   B-Faz 4: `recalculate_bracelet_pool_mileage_after_cancel` (Hurda Faz 7
#            eşleniği) iptal sonrası StockLedger üzerinden geri sarma yapar.
#   B-Faz 6: Liste sorgusu `ever_sold` + `has_in_progress` annotate eder ve
#            hayalet kayıtları gizler; toptan IN_PROGRESS bilezikler görünür
#            kalır.
#
# Bu dosya tek başına bu fazları taşır; toptan tarafı (B-Faz 5) ayrı dosyada
# (`apps/process/wholesale_views.py`) güncellenir.
# ============================================================================

from decimal import Decimal, ROUND_HALF_UP
import logging

from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import (
    Q, OuterRef, Subquery, CharField, Exists, F, Sum, DecimalField,
)
from django.db.models.functions import Greatest
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.process.models import Process
from apps.helpers.numbers import parse_decimal_locale
from apps.roles.decorators import role_required

# DOĞRU MODELLER
from apps.products.models import Products
from apps.definitions.categories.models import Categories
from apps.bracelets.models import Bracelets

# --- StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.stock_management.services.cancel_service import cancel_stock_entry

# --- Tedarikçi/Cari entegrasyonu ---
from apps.suppliers.models import Suppliers, SupplierLedger
from apps.process.views import generate_process_no


logger = logging.getLogger(__name__)


# ============================================================================
# KÜÇÜK YARDIMCILAR
# ============================================================================

def d_quantize(val: Decimal, places: int) -> Decimal:
    if val is None:
        val = Decimal('0')
    q = Decimal('1').scaleb(-places) if places > 0 else Decimal('1')
    return Decimal(val).quantize(q, rounding=ROUND_HALF_UP)


def d_fmt(val: Decimal, max_places: int = 6) -> str:
    """ONARIM FAZI 8 — `f"{Decimal('590'):f}"` → "590" (ondalık nokta yok)
    olduğunda eski `rstrip('0')` 590 → "59" üretiyordu. Sıfır kırpma artık
    YALNIZCA ondalık nokta varlığında uygulanır.
    """
    if val is None:
        return ""
    v = d_quantize(Decimal(val), max_places)
    s = f"{v:f}"
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or "0"


# ============================================================================
# B-FAZ 1 — HAVUZ ANAHTARI: SADECE İSİM
# ============================================================================
# Eski tasarımda `find_bracelet_pool_by_name_milyem` `product_mileage=...`
# filtresi nedeniyle 916 ve 925 milyem "burma" girişleri AYRI havuz açıyordu.
# Yeni tasarım hurda Faz 9 paradigmasıyla aynı: havuz anahtarı SADECE isim;
# milyem ağırlıklı ortalama ile takip edilir. Geriye dönük uyumluluk için
# eski isim de alias olarak korunur.

def find_bracelet_pool_by_name(store, category, name):
    """
    Aynı isimli aktif bilezik havuzunu bulur. İsim case-insensitive ve
    `strip()` ile karşılaştırılır. Silinmiş / pasif (is_deleted=True veya
    is_active=False) havuzlar HARİÇ tutulur — pasif havuza yeniden giriş
    için revival reset akışı `bracelet_add` içinde ayrıca işler.

    Returns: Products instance veya None
    """
    if not name:
        return None
    norm_name = (name or '').strip().lower()
    if not norm_name:
        return None

    candidates = Products.objects.filter(
        store=store, category=category,
        is_deleted=False, is_active=True,
    ).order_by('created_on', 'id')

    for p in candidates:
        if ((p.name or '').strip().lower()) == norm_name:
            return p
    return None


def find_bracelet_pool_by_name_milyem(store, category, name, milyem=None):
    """DEPRECATED (B-Faz 1) — `find_bracelet_pool_by_name`'e delegate eder.
    Geriye dönük uyumluluk için bırakıldı; `milyem` parametresi yok sayılır.
    Yeni kodda `find_bracelet_pool_by_name` kullanılmalıdır.
    """
    return find_bracelet_pool_by_name(store=store, category=category, name=name)


# ============================================================================
# B-FAZ 3 — WAC MİLYEM GÜNCELLEME (Hurda Faz 5 eşleniği)
# ============================================================================

def update_bracelet_pool_weighted_mileage(product, store, new_gram: Decimal, new_mileage: Decimal):
    """
    Mevcut bilezik havuzuna yeni gramaj eklenirken milyemi AĞIRLIKLI
    ORTALAMA formülüyle günceller ve `Products` tablosundaki maliyet
    alanlarını senkronize eder.

    Formül:
        yeni_milyem = ((mevcut_gram * mevcut_milyem) + (yeni_gram * yeni_milyem)) / toplam_gram

    Bu fonksiyon yalnızca meta-veri (milyem + birim has) günceller; gram
    birikimi (snapshot WAC HS dahil) çağıran tarafta `StockService.record_entry`
    ile yapılır.

    full_clean() bypass — Products.objects.filter().update() ile atomic SQL
    UPDATE; instance dirty olmaz, `Products.gram` negatif kalsa bile patlamaz
    (Hurda Onarım Fazı 5 / ADIM A deseni).

    select_for_update() — StockSnapshot satırı row-lock; eş zamanlı bilezik
    girişlerinde WAC race-condition'a düşmez.

    Returns: (new_mileage_int, new_buy_price_hs)
    """
    try:
        new_gram = Decimal(str(new_gram or 0))
        new_mileage = Decimal(str(new_mileage or 0))
    except Exception:
        return None, None

    if new_gram <= 0 or new_mileage <= 0 or product is None:
        return None, None

    snap = (
        StockSnapshot.objects
        .select_for_update()
        .filter(product=product, store=store)
        .first()
    )
    current_gram = (
        Decimal(str(snap.stock_gram))
        if (snap and snap.stock_gram is not None)
        else Decimal('0')
    )
    current_mileage = Decimal(product.product_mileage or 0)
    total_gram_after = current_gram + new_gram

    if total_gram_after > 0 and current_gram > 0 and current_mileage > 0:
        weighted = (
            (current_gram * current_mileage) + (new_gram * new_mileage)
        ) / total_gram_after
        result_mileage = Decimal(int(
            weighted.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        ))
    else:
        # İlk giriş veya snapshot henüz oluşmamış → yeni giren tek belirleyici
        result_mileage = Decimal(int(new_mileage))

    new_buy_price_hs = d_quantize(result_mileage / Decimal('1000'), 3)

    Products.objects.filter(id=product.id).update(
        product_mileage=result_mileage,
        buy_price_hs=new_buy_price_hs,
    )
    # Instance senkronu (çağıran taraf güncel okuyabilsin)
    product.product_mileage = result_mileage
    product.buy_price_hs = new_buy_price_hs

    return int(result_mileage), new_buy_price_hs


# ============================================================================
# B-FAZ 4 — İPTAL SONRASI WAC GERİ SARMA (Hurda Faz 7 eşleniği)
# ============================================================================

def recalculate_bracelet_pool_mileage_after_cancel(product, store):
    """
    Bir bilezik havuzu girişinin iptali sonrasında havuzun ağırlıklı
    ortalama milyemini StockLedger üzerindeki HÂLÂ AKTİF (iptal edilmemiş)
    giriş kayıtlarından yeniden hesaplar ve Products + StockSnapshot
    alanlarını günceller.

    `cancel_stock_entry` yalnızca StockSnapshot.stock_gram'ı düşürür; WAC
    "çıkışta sabit kalır" prensibi gereği korunur. Bu yüzden iptal akışında
    milyem ELLE geri sarılmalıdır.

    Algoritma (Hurda Faz 7 ile aynı):
        1. StockLedger IN giriş kayıtlarını topla (PURCHASE/INITIAL/ADJUSTMENT_PLUS),
           ref_type '_cancel' ile bitenler hariç (orijinaller).
        2. Aynı (ref_type, ref_id) çiftine sahip OUT/'_cancel' reversal varsa
           o orijinal girişi atla.
        3. Geriye kalan girişlerden ağırlıklı ortalama hesapla.
        4. Atomic UPDATE ile alanları güncelle (full_clean bypass).

    Returns: dict (yeni değerler) veya None (geçersiz parametreler).
    """
    if product is None or store is None:
        return None

    entries = list(
        StockLedger.objects
        .filter(
            product=product,
            store=store,
            direction=StockLedger.Direction.IN,
            reason__in=[
                StockLedger.Reason.PURCHASE,
                StockLedger.Reason.INITIAL,
                StockLedger.Reason.ADJUSTMENT_PLUS,
            ],
        )
        .exclude(ref_type__endswith='_cancel')
        .values('ref_type', 'ref_id', 'quantity_gram', 'unit_cost_hs')
    )

    reversed_pairs = set()
    rev_qs = StockLedger.objects.filter(
        product=product,
        store=store,
        direction=StockLedger.Direction.OUT,
        ref_type__endswith='_cancel',
    ).values_list('ref_type', 'ref_id')
    for rev_ref_type, rev_ref_id in rev_qs:
        original_ref_type = (
            rev_ref_type[:-len('_cancel')]
            if rev_ref_type.endswith('_cancel') else rev_ref_type
        )
        reversed_pairs.add((original_ref_type, rev_ref_id))

    total_gram = Decimal('0')
    weighted_sum_hs = Decimal('0')
    active_count = 0
    for e in entries:
        key = (e['ref_type'], e['ref_id'])
        if key in reversed_pairs:
            continue
        qty = Decimal(str(e['quantity_gram'] or 0))
        cost = Decimal(str(e['unit_cost_hs'] or 0))
        if qty <= 0:
            continue
        total_gram += qty
        weighted_sum_hs += qty * cost
        active_count += 1

    if total_gram > 0 and weighted_sum_hs > 0:
        avg_hs = weighted_sum_hs / total_gram
        new_mileage = Decimal(int(
            (avg_hs * Decimal('1000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        ))
        new_buy_price_hs = d_quantize(new_mileage / Decimal('1000'), 3)
    else:
        new_mileage = Decimal('0')
        new_buy_price_hs = Decimal('0.000')

    Products.objects.filter(id=product.id).update(
        product_mileage=new_mileage,
        buy_price_hs=new_buy_price_hs,
    )
    StockSnapshot.objects.filter(product=product, store=store).update(
        weighted_avg_cost_hs=new_buy_price_hs,
    )

    product.product_mileage = new_mileage
    product.buy_price_hs = new_buy_price_hs

    return {
        'new_mileage': int(new_mileage),
        'new_buy_price_hs': new_buy_price_hs,
        'active_entry_count': active_count,
        'total_gram': total_gram,
    }


# ============================================================================
# SAYFA
# ============================================================================

@login_required(login_url='login')
@role_required('BRACELETS_BRACELET_INDEX')
def bracelet_index(request):
    return render(request, 'management/bracelets/index.html', {
        'title': 'Bilezik Yönetimi'
    })


# ============================================================================
# EKLE / GÜNCELLE
# ============================================================================
# B-Faz 1 + B-Faz 2 + B-Faz 3
#
#   Yeni kayıt akışı:
#     1) Havuzu sadece İSME göre ara (find_bracelet_pool_by_name).
#     2) Her giriş için BENZERSİZ bp_process_no üret.
#        StockLedger.ref_id = SupplierLedger.process_no = Process.process_no.
#     3) Havuz pasif/silinmişse `was_revival` tespit edilir; stok kalıntısı
#        varsa StockService.adjustment(actual_gram=0) ile sıfırlanır ve
#        Products.gram / product_mileage atomic update ile temizlenir.
#     4) StockService.record_entry(ref_type='bracelet_add', ref_id=bp_process_no).
#     5) update_bracelet_pool_weighted_mileage ile WAC milyem hesaplanır.
#     6) Products.gram legacy alanı `Greatest(F('gram') + total_gram, 0)` ile
#        güncellenir (zemin koruması — düşürme yok ama tutarlı API).
#     7) Tedarikçi varsa SupplierLedger + Process kaydı bp_process_no ile yazılır.
#
#   Güncelle akışı (bracelet_id):
#     - Products.objects.filter(id=p.id).update(...) ile full_clean bypass.

@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
@role_required('BRACELETS_BRACELET_ADD')
def bracelet_add(request):
    """
    Bilezik ekle/güncelle (GRAM BAZLI):
      - total_gram: toplam stok gramı (3 ondalık)
      - product_mileage: milyem (tam sayı, 1-1000)
      - buy_price_hs: GRAM başına has (= milyem/1000)

    HAVUZ BİRLEŞTİRME (B-Faz 1):
      Yeni kayıt dalında AYNI İSİMLİ aktif Products varsa yeni kayıt
      açılmaz; mevcut havuza gram eklenir. Milyem ağırlıklı ortalama ile
      güncellenir. Mevcut Bracelets satırı varsa is_deleted=False'a çekilir;
      yoksa açılır. Her giriş yeni bir Process + SupplierLedger oluşturur
      (audit izlenebilirliği için).
    """
    store = request.user.store
    try:
        category = Categories.objects.get(name__icontains='Bilezik')
    except Categories.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Bilezik kategorisi bulunamadı!'}, status=400)

    bracelet_id = (request.POST.get('bracelet_id') or '').strip()
    name = (request.POST.get('bracelet_name') or "").strip()

    # TOPLAM GRAM
    total_gram = parse_decimal_locale(
        request.POST.get('total_gram') or request.POST.get('gram'),
        default="0", places=3,
    )
    total_gram = d_quantize(total_gram, 3)
    if total_gram <= 0:
        return JsonResponse({'error': True, 'error_msg': 'Toplam gram 0\u2019dan büyük olmalı!'}, status=400)

    # milyem (tam sayı)
    raw_mileage = parse_decimal_locale(request.POST.get('product_mileage'), default="0")
    try:
        product_mileage = Decimal(int(raw_mileage))
    except Exception:
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz milyem!'}, status=400)
    if product_mileage <= 0:
        return JsonResponse({'error': True, 'error_msg': 'Milyem 0\u2019dan büyük olmalı!'}, status=400)

    # GRAM BAŞINA HAS (giriş başına; havuz WAC'ı update_..._weighted_mileage'da)
    buy_price_hs_per_gram = d_quantize(product_mileage / Decimal('1000'), 3)

    image_file = request.FILES.get('image')

    # ============================================================
    # GÜNCELLE dalı (B-Faz 3 — full_clean bypass)
    # ============================================================
    if bracelet_id:
        try:
            row = Bracelets.objects.select_related('product').get(id=bracelet_id, store=store)
        except Bracelets.DoesNotExist:
            return JsonResponse({'error': True, 'error_msg': 'Kayıt bulunamadı!'}, status=404)

        p = row.product

        # GUARD — Çoklu kaynaklı havuzlarda meta güncelleme reddedilir; hurda
        # BUG 3 deseni: ağırlıklı ortalama bozulmasın diye kullanıcı
        # "İşlemler > İptal" + yeni giriş akışına yönlendirilir.
        active_purchase_count = Process.objects.filter(
            product=p, store=store,
            transaction_type='PURCHASE',
            is_status='COMPLETED',
            is_deleted=False,
        ).count()
        any_sale = Process.objects.filter(
            product=p, store=store,
            transaction_type='SALE',
            is_status='COMPLETED',
            is_deleted=False,
        ).exists()
        if active_purchase_count > 1 or any_sale:
            return JsonResponse({
                'error': True,
                'error_code': 'MULTI_SOURCE_POOL',
                'error_msg': (
                    f"'{p.name}' havuzunda birden fazla aktif giriş veya satış geçmişi "
                    f"var. Doğrudan güncellenemez — lütfen 'İşlemler > İptal' üzerinden "
                    f"ilgili girişi geri sarın ve yeni giriş yapın."
                ),
            }, status=409)

        # Atomic UPDATE — full_clean() bariyeri tetiklenmez, gram alanı dokunulmaz
        update_kwargs = {
            'name': name,
            'product_mileage': product_mileage,
            'buy_price_hs': buy_price_hs_per_gram,
        }
        if hasattr(Products, 'is_gram_bullion'):
            update_kwargs['is_gram_bullion'] = True
        Products.objects.filter(id=p.id).update(**update_kwargs)

        # Resim varsa save() ile yaz (FileField update() ile pratik değil)
        if image_file:
            p.image = image_file
            p.save(update_fields=['image'])

        # Stok seviyesini kullanıcı talebine göre fiili değere çek
        StockService.adjustment(
            product=p,
            store=store,
            actual_gram=total_gram,
            actual_pieces=0,
            ref_id=f"bracelet_update_{bracelet_id}",
            user=request.user,
            notes=f"Bilezik güncelleme: {name}",
        )

        return JsonResponse({'result': True})

    # ============================================================
    # YENİ KAYIT dalı — HAVUZ ARAMA (B-Faz 1)
    # ============================================================
    # Has Altın TL kuru
    _hs_rate_tl = Decimal('0')
    try:
        hs_product = Products.objects.filter(name__icontains='Has Altın').first()
        if hs_product and hs_product.buy_price_tl:
            _hs_rate_tl = Decimal(str(hs_product.buy_price_tl))
    except Exception:
        pass

    unit_cost_tl = Decimal('0.00')
    if _hs_rate_tl > 0 and buy_price_hs_per_gram > 0:
        unit_cost_tl = (buy_price_hs_per_gram * _hs_rate_tl).quantize(Decimal('0.01'))

    supplier_id = (request.POST.get('supplier_id') or '').strip()
    _stock_reason = StockLedger.Reason.PURCHASE if supplier_id else StockLedger.Reason.INITIAL
    _stock_ref_type = 'bracelet_add'

    # B-Faz 2 — Her giriş için BENZERSİZ process_no
    bp_process_no = generate_process_no()

    # HAVUZ ARAMA: SADECE isim
    existing_pool = find_bracelet_pool_by_name(
        store=store, category=category, name=name,
    )

    # Aktif havuz bulunamadıysa, AYNI isimli pasif/silinmiş bir havuza
    # yeniden giriş yapılıyor olabilir (revival). Önce pasifleri de tarayalım.
    revival_pool = None
    if existing_pool is None and name:
        norm_name = name.strip().lower()
        candidates = Products.objects.filter(
            store=store, category=category,
        ).filter(Q(is_deleted=True) | Q(is_active=False))
        for cp in candidates:
            if ((cp.name or '').strip().lower()) == norm_name:
                revival_pool = cp
                break

    if existing_pool is not None or revival_pool is not None:
        p = existing_pool or revival_pool
        was_revival = (existing_pool is None)  # Aktif değil ama bulundu → revival

        # ------------------------------------------------------------------
        # B-Faz 5 paraleli — Bracelets + Products bayrak resetleri
        # ------------------------------------------------------------------
        bracelet_row = Bracelets.objects.filter(product=p, store=store).first()
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
            bracelet_row = Bracelets.objects.create(
                store=store, product=p, created_by=request.user
            )

        if p.is_active is False or p.is_deleted is True:
            Products.objects.filter(id=p.id).update(is_active=True, is_deleted=False)
            p.is_active = True
            p.is_deleted = False

        # ------------------------------------------------------------------
        # REVIVAL RESET (Hurda Faz 6 BUG 6 / Faz 7 ADIM 3 deseni)
        # ------------------------------------------------------------------
        # Pasif havuza yeni giriş geldiğinde önceki silme/iptal akışı stok
        # kalıntısı bırakmış olabilir. Yeni giriş tek belirleyici sayılır:
        # snapshot 0'a çekilir, legacy alanlar atomic temizlenir.
        if was_revival:
            _stale_gram = Decimal('0')
            _stale_pieces = 0
            try:
                _stale_snap = (
                    StockSnapshot.objects
                    .select_for_update()
                    .filter(product=p, store=store)
                    .first()
                )
                _stale_gram = (
                    Decimal(str(_stale_snap.stock_gram))
                    if (_stale_snap and _stale_snap.stock_gram is not None)
                    else Decimal('0')
                )
                _stale_pieces = (
                    int(_stale_snap.stock_pieces or 0) if _stale_snap else 0
                )
                if _stale_gram > 0 or _stale_pieces > 0:
                    StockService.adjustment(
                        product=p, store=store,
                        actual_gram=Decimal('0'),
                        actual_pieces=0,
                        ref_id=f"bracelet_revival_{p.id}_{bp_process_no}",
                        user=request.user,
                        notes=(
                            "Bilezik havuzu yeniden açılışı: önceki silme/iptal "
                            "sonrası kalan stok temizlendi"
                        ),
                    )
            except Exception as exc:
                logger.error(
                    "Bilezik revival adjustment başarısız: product=%s store=%s err=%s",
                    p.id, store.id, exc,
                )

            if _stale_gram > 0 or _stale_pieces > 0:
                Products.objects.filter(id=p.id).update(
                    gram=Decimal('0'),
                    product_mileage=Decimal('0'),
                )
                p.gram = Decimal('0')
                p.product_mileage = Decimal('0')

        # Resim güncelleme (opsiyonel)
        if image_file:
            p.image = image_file
            p.save(update_fields=['image'])

        # ------------------------------------------------------------------
        # B-Faz 2 — Stok girişi (her giriş benzersiz ref_id)
        # ------------------------------------------------------------------
        StockService.record_entry(
            product=p, store=store,
            quantity_gram=total_gram, quantity_pieces=0,
            reason=_stock_reason,
            ref_type=_stock_ref_type,
            ref_id=bp_process_no,
            unit_cost_hs=buy_price_hs_per_gram,
            unit_cost_tl=unit_cost_tl,
            hs_rate_tl=_hs_rate_tl,
            user=request.user,
            notes=f"Bilezik havuz girişi: {name} ({int(product_mileage)} milyem)",
        )

        # ------------------------------------------------------------------
        # B-Faz 3 — WAC milyem güncelle (havuz farklı milyemli ise ortalama)
        # ------------------------------------------------------------------
        update_bracelet_pool_weighted_mileage(
            product=p, store=store,
            new_gram=total_gram,
            new_mileage=product_mileage,
        )

        # Legacy Products.gram birikimi (zemin korumalı toplama)
        Products.objects.filter(id=p.id).update(
            gram=Greatest(F('gram') + total_gram, Decimal('0'))
        )

        # ------------------------------------------------------------------
        # Tedarikçi cari + Process (her giriş için bp_process_no)
        # ------------------------------------------------------------------
        if supplier_id:
            try:
                supplier = Suppliers.objects.get(id=supplier_id, store=store)
                total_has_value = d_quantize(buy_price_hs_per_gram * total_gram, 3)
                if total_has_value > 0:
                    SupplierLedger.objects.create(
                        supplier=supplier,
                        product=p,
                        transaction_type=SupplierLedger.ENTRY,
                        quantity_piece=0,
                        quantity_gram=total_gram,
                        amount_value=total_has_value,
                        currency='HS',
                        process_no=bp_process_no,
                        description=f"Bilezik havuz alımı: {name}",
                        is_active=True,
                    )
                    Process.objects.create(
                        store=store,
                        process_no=bp_process_no,
                        process_type='WHOLESALE',
                        transaction_type='PURCHASE',
                        product=p,
                        supplier=supplier,
                        employee=request.user,
                        piece=0,
                        gram=total_gram,
                        process_mileage=str(int(product_mileage)),
                        price_hs=total_has_value,
                        unit_price=unit_cost_tl,
                        amount=(unit_cost_tl * total_gram).quantize(Decimal('0.01')),
                        is_status='COMPLETED',
                        is_deleted=False,
                    )
            except Suppliers.DoesNotExist:
                pass
        else:
            # Tedarikçisiz giriş — yine de Process oluştur (izlenebilirlik için)
            total_has_value = d_quantize(buy_price_hs_per_gram * total_gram, 3)
            Process.objects.create(
                store=store,
                process_no=bp_process_no,
                process_type='WHOLESALE',
                transaction_type='PURCHASE',
                product=p,
                supplier=None,
                employee=request.user,
                piece=0,
                gram=total_gram,
                process_mileage=str(int(product_mileage)),
                price_hs=total_has_value,
                unit_price=unit_cost_tl,
                amount=(unit_cost_tl * total_gram).quantize(Decimal('0.01')),
                is_status='COMPLETED',
                is_deleted=False,
            )

        return JsonResponse({'result': True, 'pooled': True, 'process_no': bp_process_no})

    # ============================================================
    # HAVUZ YOK — İlk kayıt
    # ============================================================
    p = Products.objects.create(
        store=store,
        category=category,
        name=name,
        gram=Decimal('0'),
        product_mileage=product_mileage,
        buy_price_hs=buy_price_hs_per_gram,
        image=image_file if image_file else None,
        **({'is_gram_bullion': True} if hasattr(Products, 'is_gram_bullion') else {})
    )
    Bracelets.objects.create(store=store, product=p, created_by=request.user)

    StockService.record_entry(
        product=p, store=store,
        quantity_gram=total_gram, quantity_pieces=0,
        reason=_stock_reason,
        ref_type=_stock_ref_type,
        ref_id=bp_process_no,
        unit_cost_hs=buy_price_hs_per_gram,
        unit_cost_tl=unit_cost_tl,
        hs_rate_tl=_hs_rate_tl,
        user=request.user,
        notes=f"Yeni bilezik: {name}",
    )

    # Legacy Products.gram birikimi (zemin koruması)
    Products.objects.filter(id=p.id).update(
        gram=Greatest(F('gram') + total_gram, Decimal('0'))
    )

    # Tedarikçi cari + Process
    if supplier_id:
        try:
            supplier = Suppliers.objects.get(id=supplier_id, store=store)
            total_has_value = d_quantize(buy_price_hs_per_gram * total_gram, 3)
            if total_has_value > 0:
                SupplierLedger.objects.create(
                    supplier=supplier,
                    product=p,
                    transaction_type=SupplierLedger.ENTRY,
                    quantity_piece=0,
                    quantity_gram=total_gram,
                    amount_value=total_has_value,
                    currency='HS',
                    process_no=bp_process_no,
                    description=f"Bilezik alımı: {name}",
                    is_active=True,
                )
                Process.objects.create(
                    store=store,
                    process_no=bp_process_no,
                    process_type='WHOLESALE',
                    transaction_type='PURCHASE',
                    product=p,
                    supplier=supplier,
                    employee=request.user,
                    piece=0,
                    gram=total_gram,
                    process_mileage=str(int(product_mileage)),
                    price_hs=total_has_value,
                    unit_price=unit_cost_tl,
                    amount=(unit_cost_tl * total_gram).quantize(Decimal('0.01')),
                    is_status='COMPLETED',
                    is_deleted=False,
                )
        except Suppliers.DoesNotExist:
            pass
    else:
        total_has_value = d_quantize(buy_price_hs_per_gram * total_gram, 3)
        Process.objects.create(
            store=store,
            process_no=bp_process_no,
            process_type='WHOLESALE',
            transaction_type='PURCHASE',
            product=p,
            supplier=None,
            employee=request.user,
            piece=0,
            gram=total_gram,
            process_mileage=str(int(product_mileage)),
            price_hs=total_has_value,
            unit_price=unit_cost_tl,
            amount=(unit_cost_tl * total_gram).quantize(Decimal('0.01')),
            is_status='COMPLETED',
            is_deleted=False,
        )

    return JsonResponse({'result': True, 'pooled': False, 'process_no': bp_process_no})


# ============================================================================
# LİSTELEME (DataTables server-side) — B-Faz 6
# ============================================================================
# - has_in_progress annotate: toptan IN_PROGRESS bilezikler ghost filtresine
#   takılmasın, listede görünür kalsın.
# - Ghost filter: ever_sold=False AND has_in_progress=False AND stok yok →
#   tamamen iptal edilmiş + satılmamış havuzlar gizlenir.

@login_required(login_url='login')
@role_required('BRACELETS_GET_ALL')
def get_all(request):
    try:
        draw = int(request.GET.get('draw', 1))
        length = int(request.GET.get('length', 10))
        start = int(request.GET.get('start', 0))
        search_value = (request.GET.get('search[value]', '') or '').strip()
        order_column_index = request.GET.get('order[0][column]', '0')
        order_dir = request.GET.get('order[0][dir]', 'asc')
        store_id = request.user.store_id

        gold_rate_param = (request.GET.get('gold_rate') or '').strip()

        qs = (Bracelets.objects
              .filter(is_deleted=False, store_id=store_id)
              .select_related('product'))

        # UAT-1A — `is_deleted=False` filtresi: iptal edilmiş satışlar artık
        # `ever_sold=True` üretmiyor. Aksi halde "stok 0 + iptal edilmiş satış"
        # senaryosunda ghost filter kaydı gizleyemiyordu.
        ever_sold_q = Process.objects.filter(
            store_id=store_id,
            product_id=OuterRef('product_id'),
            transaction_type='SALE',
            is_status='COMPLETED',
            is_deleted=False,
        )

        last_sale_sq = (
            Process.objects
            .filter(
                store_id=store_id,
                product_id=OuterRef('product_id'),
                transaction_type='SALE',
                is_status='COMPLETED',
                is_deleted=False,
            )
            .order_by('-date', '-id')
            .values('process_no')[:1]
        )

        snap_sq = StockSnapshot.objects.filter(
            product_id=OuterRef('product_id'),
            store_id=store_id
        )
        inv_weight_sq = snap_sq.values('stock_gram')[:1]

        # B-Faz 6 — IN_PROGRESS muafiyeti
        has_in_progress_q = Process.objects.filter(
            store_id=store_id,
            product_id=OuterRef('product_id'),
            transaction_type='PURCHASE',
            is_status='IN_PROGRESS',
            is_deleted=False,
        )

        qs = qs.annotate(
            ever_sold=Exists(ever_sold_q),
            last_sale_process_no=Subquery(last_sale_sq, output_field=CharField()),
            inv_stock_weight=Subquery(inv_weight_sq),
            has_in_progress=Exists(has_in_progress_q),
        )

        # B-Faz 6 — Ghost filter:
        # ever_sold=False AND has_in_progress=False AND (stok<=0 veya null)
        # Bu kayıtlar tamamen iptal edilmiş + satılmamış + toptan IN_PROGRESS
        # değil → listede gizlenir. Satış geçmişi olanlar (ever_sold=True)
        # tarihsel veri olarak kalır.
        qs = qs.exclude(
            Q(ever_sold=False)
            & Q(has_in_progress=False)
            & (Q(inv_stock_weight__lte=0) | Q(inv_stock_weight__isnull=True))
        )

        total_records = qs.count()

        if gold_rate_param:
            try:
                val = int(Decimal(str(gold_rate_param)))
                if val > 0:
                    qs = qs.filter(product__product_mileage=val)
            except Exception:
                pass

        if search_value:
            qs = qs.filter(
                Q(product__name__icontains=search_value) |
                Q(product__product_mileage__icontains=search_value) |
                Q(product__barcode__icontains=search_value)
            )

        filtered_records = qs.count()

        columns_map = {
            '0': 'id',
            '1': 'product__name',
            '2': 'product__is_completed',
            '3': 'inv_stock_weight',
            '4': 'product__product_mileage',
            '5': 'product__buy_price_hs',
            '6': 'product__sale_price_hs',
            '7': 'created_on',
            '8': 'product__is_active',
            '9': 'id',
        }
        order_field = columns_map.get(str(order_column_index), 'created_on')
        if order_dir == 'desc':
            order_field = '-' + order_field

        qs = qs.order_by('product__is_completed', order_field)

        if length != -1:
            qs = qs[start:start + length]

        page_rows = list(qs)
        page_product_ids = [r.product_id for r in page_rows if r.product_id]

        # Tedarikçi isimleri + Process sayısı (N+1 önlemi)
        # UAT-1B — `active_purchase_count` (Process satır sayısı): "İşlem sayısı
        # > 1" sinyali aynı tedarikçiden gelen 2+ ayrı girişi de yakalar; bu
        # `suppliers_count` (tedarikçi entity sayısı) ile aynı şey değildir.
        # Frontend kalem ikonunu bu sinyalle gizler/gösterir.
        supplier_map = {}
        if page_product_ids:
            procs = (Process.objects
                     .filter(product_id__in=page_product_ids,
                             store_id=store_id,
                             transaction_type='PURCHASE',
                             is_status='COMPLETED',
                             is_deleted=False)
                     .select_related('supplier')
                     .only('product_id', 'supplier_id', 'supplier__company_name'))
            for proc in procs:
                entry = supplier_map.setdefault(
                    proc.product_id,
                    {"names": [], "has_no_supplier": False, "process_count": 0},
                )
                entry["process_count"] += 1
                if proc.supplier_id:
                    sname = proc.supplier.company_name if proc.supplier else None
                    if sname and sname not in entry["names"]:
                        entry["names"].append(sname)
                else:
                    entry["has_no_supplier"] = True

        data = []
        for r in page_rows:
            p = r.product

            inv_wgt = r.inv_stock_weight if getattr(r, 'inv_stock_weight', None) is not None else Decimal('0')
            try:
                inv_wgt_dec = Decimal(str(inv_wgt))
            except Exception:
                inv_wgt_dec = Decimal('0')

            is_completed = bool(getattr(p, 'is_completed', False) or (inv_wgt_dec <= 0))

            detail_url = None
            if getattr(r, 'ever_sold', False) and r.last_sale_process_no:
                try:
                    detail_url = reverse('process:detail', args=[r.last_sale_process_no])
                except Exception:
                    detail_url = f"/process/detail/{r.last_sale_process_no}"

            img_url = None
            try:
                if p.image and hasattr(p.image, 'url'):
                    img_url = p.image.url
            except Exception:
                img_url = None

            try:
                buy_hs_per_gram = Decimal(str(getattr(p, 'buy_price_hs') or '0'))
            except Exception:
                buy_hs_per_gram = Decimal('0')
            stock_total_hs = d_quantize(buy_hs_per_gram * inv_wgt_dec, 3)

            sale_hs = getattr(p, 'sale_price_hs', None)

            sup_entry = supplier_map.get(
                r.product_id,
                {"names": [], "has_no_supplier": False, "process_count": 0},
            )
            uniq_suppliers = sup_entry["names"]
            has_no_supplier = sup_entry["has_no_supplier"]
            total_sources = len(uniq_suppliers) + (1 if has_no_supplier else 0)
            active_purchase_count = sup_entry.get("process_count", 0)
            is_multi_source_pool = active_purchase_count > 1

            if uniq_suppliers:
                base = uniq_suppliers[0]
                extras = total_sources - 1
                if extras > 0:
                    suppliers_display = f"{base} + {extras} kaynak"
                else:
                    suppliers_display = base
            else:
                suppliers_display = "Tedarikçisiz"

            data.append({
                'id': str(r.id),
                'product__name': p.name,
                'inventory_stock_weight': d_fmt(inv_wgt_dec, 3),
                # Defansif: milyem her zaman tam sayı; trailing-zero strip riski elenir
                'product__product_mileage': (
                    str(int(p.product_mileage)) if p.product_mileage is not None else '0'
                ),
                'product__buy_price_hs': d_fmt(p.buy_price_hs, 3),
                'product__sale_price_hs': (d_fmt(sale_hs, 3) if sale_hs is not None else None),
                'stock_total_hs': d_fmt(stock_total_hs, 3),
                'product__is_active': bool(p.is_active),
                'product__is_completed': is_completed,
                'product__ever_sold': bool(getattr(r, 'ever_sold', False)),
                'product__has_in_progress': bool(getattr(r, 'has_in_progress', False)),
                'product__image_url': img_url,
                'detail_url': detail_url,
                'created_on': (r.created_on.isoformat() if r.created_on else None),

                # Tedarikçi sütunu
                'suppliers_display': suppliers_display,
                'suppliers_count': total_sources,
                'pool_has_no_supplier': has_no_supplier,
                'supplier_names': uniq_suppliers,

                # UAT-1B / UAT-2 — Çoklu kaynak (havuz) sinyali
                # Frontend bu flag'a göre kalem ikonunu gizler ve havuz
                # bilgi ikonu gösterir; 409 MULTI_SOURCE_POOL hatası kullanıcıya
                # hiç gösterilmez.
                'active_purchase_count': active_purchase_count,
                'is_multi_source_pool': is_multi_source_pool,
            })

        return JsonResponse({
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data
        })
    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


# ============================================================================
# HAVUZ KAYNAKLARI (multi-source silme analizi)
# ============================================================================

@login_required(login_url='login')
@require_http_methods(["GET"])
@role_required('BRACELETS_DELETE')
def get_pool_sources(request):
    """
    Seçilen bilezik(ler)in havuzuna bağlı aktif PURCHASE Process kayıtlarını
    döner. Scraps.get_pool_sources ile aynı sözleşme.
    """
    bracelet_ids = request.GET.getlist('ids[]') or []
    store = request.user.store
    if not bracelet_ids:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'})

    try:
        rows = Bracelets.objects.filter(id__in=bracelet_ids, store=store)
        product_ids = [r.product_id for r in rows if r.product_id]

        if not product_ids:
            return JsonResponse({'result': True, 'multi_source': False, 'processes': []})

        procs = (
            Process.objects
            .filter(
                product_id__in=product_ids,
                store=store,
                transaction_type='PURCHASE',
                is_deleted=False,
                is_status='COMPLETED',  # R-Faz 7: IN_PROGRESS taslakları gizle
            )
            .select_related('supplier', 'product')
            .order_by('-date')
        )

        bracelet_by_product = {str(r.product_id): str(r.id) for r in rows if r.product_id}

        proc_list = []
        for p in procs:
            proc_list.append({
                'process_no': p.process_no or '',
                'supplier_id': str(p.supplier_id) if p.supplier_id else None,
                'supplier_name': (p.supplier.company_name if p.supplier_id and p.supplier else 'Tedarikçisiz'),
                'gram': float(p.gram or 0),
                'price_hs': float(p.price_hs or 0),
                'date': p.date.strftime('%d.%m.%Y %H:%M') if p.date else '',
                'product_id': str(p.product_id),
                'bracelet_id': bracelet_by_product.get(str(p.product_id), ''),
            })

        multi_source = len(proc_list) > 1

        return JsonResponse({
            'result': True,
            'multi_source': multi_source,
            'processes': proc_list,
        })
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


# ============================================================================
# HAVUZ İÇERİK DETAYI (info modal için)
# ============================================================================

@login_required(login_url='login')
@require_http_methods(["GET"])
@role_required('BRACELETS_GET_ALL')
def get_pool_contents(request):
    """
    Seçilen bilezik(ler)in havuzundaki stoğun kaynak bazlı kırılımı.
    """
    bracelet_ids = request.GET.getlist('ids[]') or []
    store = request.user.store
    if not bracelet_ids:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'})

    try:
        row = (Bracelets.objects
               .filter(id__in=bracelet_ids, store=store)
               .select_related('product')
               .first())
        if not row or not row.product:
            return JsonResponse({'result': False, 'error_msg': 'Havuz bulunamadı.'})

        p = row.product

        snap = StockSnapshot.objects.filter(product=p, store=store).first()
        total_snap_gram = (
            Decimal(str(snap.stock_gram))
            if (snap and snap.stock_gram is not None) else Decimal('0')
        )

        procs = (Process.objects
                 .filter(product=p, store=store,
                         transaction_type='PURCHASE', is_deleted=False,
                         is_status='COMPLETED')  # R-Faz 7: taslak satırları gizle
                 .select_related('supplier')
                 .order_by('-date'))

        sources = []
        sum_active_proc_gram = Decimal('0')
        for proc in procs:
            pg = Decimal(str(proc.gram or 0))
            sum_active_proc_gram += pg
            s_name = (proc.supplier.company_name if proc.supplier_id and proc.supplier else None)
            sources.append({
                'supplier_name': s_name,
                'supplier_id': str(proc.supplier_id) if proc.supplier_id else None,
                'gram': float(pg),
                'process_no': proc.process_no or '',
                'date': proc.date.strftime('%d.%m.%Y %H:%M') if proc.date else '',
                'mileage': int(proc.process_mileage or 0) if proc.process_mileage else None,
                'label': (s_name if s_name else 'Tedarikçisiz'),
            })

        no_supplier_gram = total_snap_gram - sum_active_proc_gram
        if no_supplier_gram > Decimal('0.0009'):
            sources.append({
                'supplier_name': None,
                'supplier_id': None,
                'gram': float(no_supplier_gram),
                'process_no': None,
                'date': None,
                'mileage': None,
                'label': 'Tedarikçisiz (Açılış Stoğu)',
            })

        return JsonResponse({
            'result': True,
            'pool_name': p.name,
            'pool_milyem': int(p.product_mileage or 0),
            'total_gram': float(total_snap_gram),
            'sources': sources,
        })
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


# ============================================================================
# SİL (multi-source koruması ile) — B-Faz 2 / B-Faz 4
# ============================================================================

@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
@role_required('BRACELETS_DELETE')
def delete(request):
    """
    Bilezik silme akışı — havuz (pool) güvenlik kontrolü dahil.

    MOD A (selected_process_no verilmiş):
        → Sadece o Process iptal edilir, havuzun kalan bölümü korunur.
    MOD B (multi-source ve force=False):
        → 409 MULTI_SOURCE_POOL hata döner; frontend işlem seçici modalı açar.
    MOD C (single-source veya force=True):
        → Tüm aktif Process'ler iptal edilir, Products soft-delete,
          Bracelets soft-delete.

    Tüm dallarda StockLedger iz bırakılır (hard-delete YOK). İptal sonrası
    her zaman `recalculate_bracelet_pool_mileage_after_cancel` çağrılır.
    """
    ids = request.POST.getlist('ids[]') or []
    selected_process_no = (request.POST.get('selected_process_no') or '').strip()
    force = (request.POST.get('force') or '').lower() in ('1', 'true', 'yes')
    store = request.user.store

    try:
        rows = Bracelets.objects.filter(id__in=ids, store=store).select_related('product')

        for r in rows:
            p = r.product
            if not p:
                r.is_deleted = True
                r.is_active = False
                r.save(update_fields=['is_deleted', 'is_active'])
                continue

            # R-Faz 7: IN_PROGRESS taslak satırları iptal listesinde gösterme.
            linked_procs_qs = Process.objects.filter(
                product=p, store=store,
                transaction_type='PURCHASE',
                is_deleted=False,
                is_status='COMPLETED',
            )
            linked_procs = list(linked_procs_qs)
            multi_source = len(linked_procs) > 1

            # ── MOD A: Belirli bir Process iptal ──
            if selected_process_no:
                target_proc = next(
                    (pr for pr in linked_procs if pr.process_no == selected_process_no),
                    None
                )
                if target_proc is None:
                    return JsonResponse({
                        'result': False, 'error': True,
                        'error_msg': f"Seçilen işlem bulunamadı: {selected_process_no}",
                    }, status=400)

                _cancel_bracelet_process(
                    proc=target_proc, product=p, store=store, user=request.user,
                )

                # Havuz tamamen boşaldıysa Products + Bracelets soft-delete
                remaining_snap = StockSnapshot.objects.filter(product=p, store=store).first()
                if remaining_snap and (remaining_snap.stock_gram or Decimal('0')) <= Decimal('0'):
                    Products.objects.filter(id=p.id).update(is_active=False)
                    r.is_deleted = True
                    r.is_active = False
                    r.save(update_fields=['is_deleted', 'is_active'])

            # ── MOD B/C: Klasik silme akışı ──
            else:
                if multi_source and not force:
                    return JsonResponse({
                        'result': False,
                        'error': 'MULTI_SOURCE_POOL',
                        'error_msg': (
                            f"'{p.name}' havuzuna {len(linked_procs)} farklı alış kaydı "
                            f"eklenmiş. Doğrudan silinemez — lütfen iptal edilecek işlemi seçin."
                        ),
                        'product_id': str(p.id),
                        'bracelet_id': str(r.id),
                        'process_count': len(linked_procs),
                    }, status=409)

                # Tek kaynak veya force=True → tüm Process'leri iptal et
                for proc in linked_procs:
                    _cancel_bracelet_process(
                        proc=proc, product=p, store=store, user=request.user,
                    )

                Products.objects.filter(id=p.id).update(is_active=False)
                r.is_deleted = True
                r.is_active = False
                r.save(update_fields=['is_deleted', 'is_active'])

        # Dashboard cache invalidation
        try:
            from django.core.cache import cache as _cache
            _cache.delete(f"dashboard_assets_summary:{store.id}")
        except Exception:
            pass

        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


def _cancel_bracelet_process(*, proc, product, store, user):
    """
    Tek bir PURCHASE Process kaydını güvenle iptal eder.

    B-Faz 2 (Hurda Faz 4 deseni):
      `cancel_stock_entry()` ile atomik olarak:
        - StockLedger reversal satırı (REVERSAL_REASON_MAP)
        - SupplierLedger soft-disable (is_active=False)
        - frozen rate kopyalama
      ref_type fallback ('bracelet_add', 'process') — perakende ve toptan
      kaynaklı girişler tek iptal akışında handle edilir.

    B-Faz 4: İptal sonrası `recalculate_bracelet_pool_mileage_after_cancel`
    çağrılır → WAC milyem geri sarılır.

    B-Faz 2: Products.gram düşürmesi `Greatest(F('gram') - x, 0)` zemin
    koruması ile yapılır; legacy alan asla negatife düşemez.
    """
    if not proc.process_no:
        # process_no yoksa cancel_stock_entry çalışamaz; yine de Process pasif
        proc.is_status = 'CANCELED'
        proc.is_deleted = True
        proc.save(update_fields=['is_status', 'is_deleted'])
        return

    # ref_type fallback — perakende ('bracelet_add') ve toptan ('process')
    cancelled_any = False
    last_result = None
    for _ref_type in ('bracelet_add', 'process'):
        try:
            res = cancel_stock_entry(
                ref_type=_ref_type,
                ref_id=proc.process_no,
                user=user,
                reverse_supplier_ledger=True,
                notes=f"Bilezik alım iptali: {product.name} (proc {proc.process_no})",
                raise_if_not_found=False,
            )
        except Exception as exc:
            logger.error(
                "cancel_stock_entry başarısız: ref_type=%s ref_id=%s err=%s",
                _ref_type, proc.process_no, exc,
            )
            continue

        last_result = res
        if (res.get('cancelled_stock_count', 0) > 0
                or res.get('deactivated_supplier_ledgers', 0) > 0):
            cancelled_any = True
            break

    if not cancelled_any:
        logger.warning(
            "Bilezik iptal: hiçbir StockLedger/SupplierLedger kaydı bulunamadı "
            "(proc %s)", proc.process_no,
        )

    # Legacy Products.gram zemin korumalı düşürme
    try:
        gram_to_remove = Decimal(str(proc.gram or 0))
        if gram_to_remove > 0:
            Products.objects.filter(id=product.id).update(
                gram=Greatest(F('gram') - gram_to_remove, Decimal('0'))
            )
    except Exception as exc:
        logger.error(
            "Bilezik Products.gram düşürme başarısız: product=%s err=%s",
            product.id, exc,
        )

    # Process pasif
    proc.is_status = 'CANCELED'
    proc.is_deleted = True
    proc.save(update_fields=['is_status', 'is_deleted'])

    # B-Faz 4 — WAC milyem geri sar (her zaman çağrılır; başarısızsa log düşer)
    try:
        recalculate_bracelet_pool_mileage_after_cancel(product=product, store=store)
    except Exception as exc:
        logger.error(
            "recalculate_bracelet_pool_mileage_after_cancel başarısız: "
            "product=%s store=%s err=%s",
            product.id, store.id, exc,
        )


# ============================================================================
# DURUM DEĞİŞTİR
# ============================================================================

@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
@role_required('BRACELETS_CHANGE_STATUS')
def change_status(request):
    ids = request.POST.getlist('ids[]') or []
    try:
        rows = Bracelets.objects.filter(id__in=ids).select_related('product')
        for r in rows:
            new_state = not r.is_active
            r.is_active = new_state
            r.save(update_fields=['is_active'])
            if r.product_id:
                Products.objects.filter(id=r.product_id).update(is_active=new_state)
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)
