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
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import (
    Q, OuterRef, Subquery, CharField, Exists, F, Sum, DecimalField,
    Case, When, Value, Window, Count, IntegerField, BooleanField,
)
from django.db.models.functions import Greatest, Coalesce
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
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

    UAT BULGU 1 (2026-04-29) — STALE INSTANCE FIX:
        Çoklu giriş döngüsünde her Process satırının `p.product` farklı
        bir Python instance olabilir. Önceki iter DB'yi UPDATE eder ama
        sadece kendi parametre instance'ına yazar; sonraki iter ise BAŞKA
        bir instance taşıdığı için stale `product_mileage` okuyup ELSE
        dalına düşer ve milyemi tek başına ezer (havuzun ağırlıklı
        ortalaması bozulur). Çözüm: current_mileage parametre instance
        üzerinden değil, DB'den taze okunur. Hurda eşleniği:
        apps/scraps/views.py:update_scrap_pool_weighted_mileage.

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
    # UAT BULGU 1 (2026-04-29): DB'den taze oku — stale Python instance koruması
    fresh_mileage_row = (
        Products.objects
        .filter(id=product.id)
        .values('product_mileage')
        .first()
    )
    current_mileage = Decimal(
        (fresh_mileage_row and fresh_mileage_row.get('product_mileage')) or 0
    )
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
    elif total_gram > 0:
        # FAZ 15 / WAC FALLBACK GUARD:
        # Aktif giriş gramı var ama unit_cost_hs hep 0 (legacy retail veri).
        # Hurda FAZ 15 ile aynı simetri — Products.product_mileage'da tutulan
        # mevcut WAC'ı koru, yanlışlıkla milyemi sıfırlama.
        _existing_mileage = Decimal(str(getattr(product, 'product_mileage', 0) or 0))
        if _existing_mileage > 0:
            new_mileage = _existing_mileage
            new_buy_price_hs = d_quantize(new_mileage / Decimal('1000'), 3)
        else:
            new_mileage = Decimal('0')
            new_buy_price_hs = Decimal('0.000')
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

    # ── FAZ C2 — WAC TUTARLILIK SANITY KONTROLÜ ──────────────────────
    # Yalnızca operasyonel uyarı (logger.warning); otomatik düzeltme YOK.
    try:
        fresh_snap = StockSnapshot.objects.filter(
            product=product, store=store,
        ).only('stock_gram', 'weighted_avg_cost_hs').first()
        if fresh_snap is not None:
            snap_gram = Decimal(str(fresh_snap.stock_gram or 0))
            tolerance = Decimal('0.001')
            if snap_gram > total_gram + tolerance:
                logger.warning(
                    "WAC_SANITY: stock_gram=%s > toplam_aktif_giris=%s "
                    "(product=%s store=%s) — phantom stock olabilir.",
                    snap_gram, total_gram, product.id, store.id,
                )
            if snap_gram > tolerance and new_buy_price_hs <= 0:
                logger.warning(
                    "WAC_SANITY: stock_gram=%s ama WAC=0 (product=%s store=%s) "
                    "— aktif girişler arasında unit_cost_hs eksik olabilir.",
                    snap_gram, product.id, store.id,
                )
    except Exception as _sanity_err:
        logger.error("WAC_SANITY bilezik: kontrol başarısız: %s", _sanity_err)

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

        # FAZ 19 / Bulgu E — StockSnapshot WAC senkronizasyonu.
        # Tek-kaynaklı havuz (active_purchase_count<=1, any_sale=False) garantisi
        # yukarıda doğrulandığı için yeni buy_price_hs doğrudan WAC'a yazılır.
        # Pool detay sayfası StockSnapshot.weighted_avg_cost_hs okur; Products
        # update'i sonrası burası senkronize edilmezse "milyem 900 ama HS/GR
        # 0.925" tutarsızlığı oluşur. StockLedger append-only kuralı korunur
        # (cache tablosu olan StockSnapshot dışında hiçbir hareket yazılmaz).
        try:
            with transaction.atomic():
                _snap_qs = StockSnapshot.objects.select_for_update().filter(
                    product=p, store=store
                )
                _snap_qs.update(weighted_avg_cost_hs=buy_price_hs_per_gram)
        except Exception:
            logger.exception(
                "bracelet_add UPDATE: StockSnapshot WAC senkronizasyonu başarısız "
                "(product_id=%s, store_id=%s)",
                getattr(p, 'id', None), getattr(store, 'id', None),
            )

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
    _hs_rate_eur = Decimal('0')
    try:
        hs_product = Products.objects.filter(name__icontains='Has Altın').first()
        if hs_product and hs_product.buy_price_eur:
            _hs_rate_eur = Decimal(str(hs_product.buy_price_eur))
    except Exception:
        pass

    unit_cost_eur = Decimal('0.00')
    if _hs_rate_eur > 0 and buy_price_hs_per_gram > 0:
        unit_cost_eur = (buy_price_hs_per_gram * _hs_rate_eur).quantize(Decimal('0.01'))

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
            unit_cost_eur=unit_cost_eur,
            hs_rate_eur=_hs_rate_eur,
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
                        unit_price=unit_cost_eur,
                        amount=(unit_cost_eur * total_gram).quantize(Decimal('0.01')),
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
                unit_price=unit_cost_eur,
                amount=(unit_cost_eur * total_gram).quantize(Decimal('0.01')),
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
        unit_cost_eur=unit_cost_eur,
        hs_rate_eur=_hs_rate_eur,
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
                    unit_price=unit_cost_eur,
                    amount=(unit_cost_eur * total_gram).quantize(Decimal('0.01')),
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
            unit_price=unit_cost_eur,
            amount=(unit_cost_eur * total_gram).quantize(Decimal('0.01')),
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
        # FAZ 50.1 — Custody-only havuz görünürlüğü:
        # Yalnız emanet (CUSTODY_IN) ile oluşturulmuş bilezik havuzlarında
        # stock_gram=0 kalır; ghost filter bu havuzu hatalı şekilde gizliyordu.
        # custody_gram subquery'si eklenerek "emanet var ama stoğa alınmamış"
        # bilezik havuzları da ghost filtresinden muaf tutulur.
        inv_custody_sq = snap_sq.values('custody_gram')[:1]

        # B-Faz 6 — IN_PROGRESS muafiyeti
        # UAT BULGU 4 (2026-04-29) DARALTMA:
        #   Muafiyet yalnızca toptan iki-faz commit penceresine aittir.
        #   `process_type='WHOLESALE'` filtresi olmadan, perakende sepete
        #   eklenmiş ama tamamlanmamış bilezik PURCHASE'ları ana listede
        #   "hayalet kayıt" olarak görünüyordu (sızıntı). Perakende sepetten
        #   ayrılırsa kayıt boşa düşmeli, ana listeye sızmamalı.
        has_in_progress_q = Process.objects.filter(
            store_id=store_id,
            product_id=OuterRef('product_id'),
            transaction_type='PURCHASE',
            process_type='WHOLESALE',
            is_status='IN_PROGRESS',
            is_deleted=False,
        )

        qs = qs.annotate(
            ever_sold=Exists(ever_sold_q),
            last_sale_process_no=Subquery(last_sale_sq, output_field=CharField()),
            inv_stock_weight=Subquery(inv_weight_sq),
            inv_custody_weight=Subquery(inv_custody_sq),
            has_in_progress=Exists(has_in_progress_q),
        )

        # B-Faz 6 — Ghost filter:
        # ever_sold=False AND has_in_progress=False AND (stok<=0 veya null)
        # Bu kayıtlar tamamen iptal edilmiş + satılmamış + toptan IN_PROGRESS
        # değil → listede gizlenir. Satış geçmişi olanlar (ever_sold=True)
        # tarihsel veri olarak kalır.
        # FAZ 50.1 — Emanet (custody_gram > 0) olanlar da listede tutulur.
        qs = qs.exclude(
            Q(ever_sold=False)
            & Q(has_in_progress=False)
            & (Q(inv_stock_weight__lte=0) | Q(inv_stock_weight__isnull=True))
            & (Q(inv_custody_weight__lte=0) | Q(inv_custody_weight__isnull=True))
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

            # FAZ 50.1 — Custody (emanet) gram bilgisi
            inv_custody = getattr(r, 'inv_custody_weight', None) or Decimal('0')
            try:
                inv_custody_dec = Decimal(str(inv_custody))
            except Exception:
                inv_custody_dec = Decimal('0')

            # FAZ 50.1 — is_completed kararı: stock_gram=0 olsa bile custody varsa
            # "Satıldı/Stoğu yok" görüntüsü vermeyiz; havuz canlı sayılır.
            _has_any_stock = (inv_wgt_dec > 0) or (inv_custody_dec > Decimal('0.0005'))
            is_completed = bool(getattr(p, 'is_completed', False) or not _has_any_stock)

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

                # FAZ 50.1 — Emanet (custody) gramı: bilezik havuzu yalnızca
                # emanetten oluşmuşsa (stock_gram=0) frontend turuncu badge ile
                # "Emanet X gr" gösterir; "Stoğa Al" akışına yönlendirme.
                'custody_gram': d_fmt(inv_custody_dec, 3),
                'has_custody': inv_custody_dec > Decimal('0.0005'),
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
            _gram_val = float(p.gram or 0)
            _price_hs_val = float(p.price_hs or 0)
            _unit_hs = (_price_hs_val / _gram_val) if _gram_val > 0 else 0.0
            proc_list.append({
                'process_no': p.process_no or '',
                'supplier_id': str(p.supplier_id) if p.supplier_id else None,
                'supplier_name': (p.supplier.company_name if p.supplier_id and p.supplier else 'Tedarikçisiz'),
                'gram': _gram_val,
                'price_hs': _price_hs_val,
                'price_hs_per_gram': round(_unit_hs, 3),
                'date': p.date.strftime('%d.%m.%Y %H:%M') if p.date else '',
                'date_short': p.date.strftime('%d %b %Y') if p.date else '',
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
        sum_total_price_hs = Decimal('0')
        for proc in procs:
            pg = Decimal(str(proc.gram or 0))
            ph = Decimal(str(proc.price_hs or 0))
            sum_active_proc_gram += pg
            sum_total_price_hs += ph
            s_name = (proc.supplier.company_name if proc.supplier_id and proc.supplier else None)
            unit_hs = (ph / pg) if pg > 0 else Decimal('0')
            sources.append({
                'supplier_name': s_name,
                'supplier_id': str(proc.supplier_id) if proc.supplier_id else None,
                'gram': float(pg),
                'price_hs': float(ph),
                'price_hs_per_gram': float(unit_hs.quantize(Decimal('0.001'))),
                'process_no': proc.process_no or '',
                'date': proc.date.strftime('%d.%m.%Y %H:%M') if proc.date else '',
                'date_short': proc.date.strftime('%d %b %Y') if proc.date else '',
                'mileage': int(proc.process_mileage or 0) if proc.process_mileage else None,
                'label': (s_name if s_name else 'Tedarikçisiz'),
            })

        no_supplier_gram = total_snap_gram - sum_active_proc_gram
        if no_supplier_gram > Decimal('0.0009'):
            try:
                snap_wac = Decimal(str(snap.weighted_avg_cost_hs)) if snap and snap.weighted_avg_cost_hs else Decimal('0')
            except Exception:
                snap_wac = Decimal('0')
            est_unit = snap_wac
            est_total = (no_supplier_gram * est_unit) if est_unit > 0 else Decimal('0')
            sources.append({
                'supplier_name': None,
                'supplier_id': None,
                'gram': float(no_supplier_gram),
                'price_hs': float(est_total.quantize(Decimal('0.001'))) if est_total else 0.0,
                'price_hs_per_gram': float(est_unit.quantize(Decimal('0.001'))) if est_unit else 0.0,
                'process_no': None,
                'date': None,
                'date_short': None,
                'mileage': None,
                'label': 'Tedarikçisiz (Açılış Stoğu)',
                'is_estimated': True,
            })

        try:
            pool_wac = Decimal(str(snap.weighted_avg_cost_hs)) if snap and snap.weighted_avg_cost_hs else Decimal('0')
        except Exception:
            pool_wac = Decimal('0')

        return JsonResponse({
            'result': True,
            'pool_name': p.name,
            'pool_milyem': int(p.product_mileage or 0),
            'total_gram': float(total_snap_gram),
            'total_price_hs': float(sum_total_price_hs.quantize(Decimal('0.001'))),
            'pool_wac_hs_per_gram': float(pool_wac.quantize(Decimal('0.001'))) if pool_wac else 0.0,
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

    REFACTOR (Bilezik İptal SSOT):
        _cancel_bracelet_process yerine merkezi cancel_bracelet_purchase
        kullanılır. Hurda SSOT (apps/scraps/services/cancel_scrap_service)
        ile birebir simetrik mimari.
    """
    from apps.bracelets.services.cancel_bracelet_service import (
        cancel_bracelet_purchase,
        CancelNotAllowedError,
        CancelBraceletError,
    )

    ids = request.POST.getlist('ids[]') or []
    selected_process_no = (request.POST.get('selected_process_no') or '').strip()
    selected_ledger_id = (request.POST.get('selected_ledger_id') or '').strip()
    force = (request.POST.get('force') or '').lower() in ('1', 'true', 'yes')
    store = request.user.store

    # FAZ 13.1 — Tedarikçisiz/Legacy iptal fix:
    # Detay sayfasından gelen ledger_id Process tablosunda eşleşme bulamazsa
    # (örn. eski tedarikçisiz açılış stoğu), ref_id'yi StockLedger satırından
    # türetip selected_process_no'ya basarız. MOD A akışı sonra StockLedger
    # ref_type/ref_id ile cancel_stock_entry çağırıp legacy satırı reverse eder.
    if not selected_process_no and selected_ledger_id:
        try:
            _row = StockLedger.objects.get(id=selected_ledger_id, store=store)
            if _row.ref_id:
                selected_process_no = _row.ref_id.strip()
        except StockLedger.DoesNotExist:
            pass

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
                    # FAZ 13.1 — Legacy/Process'siz iptal fallback
                    if selected_ledger_id:
                        try:
                            _row = StockLedger.objects.get(
                                id=selected_ledger_id, store=store, product=p,
                            )
                            cancel_stock_entry(
                                ref_type=_row.ref_type or 'bracelet_add',
                                ref_id=_row.ref_id,
                                user=request.user,
                                reverse_supplier_ledger=False,
                                notes=f"Bilezik legacy iptal: {p.name} (ledger {_row.id})",
                                raise_if_not_found=False,
                            )
                            try:
                                recalculate_bracelet_pool_mileage_after_cancel(
                                    product=p, store=store
                                )
                            except Exception as _e:
                                logger.error(
                                    f"recalculate after legacy cancel failed: {_e}"
                                )
                        except StockLedger.DoesNotExist:
                            return JsonResponse({
                                'result': False, 'error': True,
                                'error_msg': f"Ledger satırı bulunamadı: {selected_ledger_id}",
                            }, status=400)
                    else:
                        return JsonResponse({
                            'result': False, 'error': True,
                            'error_msg': f"Seçilen işlem bulunamadı: {selected_process_no}",
                        }, status=400)
                else:
                    # Merkezi servis: stok + cari + process status atomik
                    cancel_bracelet_purchase(
                        process_id=target_proc.id,
                        user=request.user,
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
                    cancel_bracelet_purchase(
                        process_id=proc.id,
                        user=request.user,
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
    except (CancelNotAllowedError, CancelBraceletError) as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


def _cancel_bracelet_process(*, proc, product, store, user, skip_recalculate=False):
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

    FAZ C1 — Toplu iptal optimizasyonu:
        skip_recalculate=True ise WAC/milyem geri hesaplama atlanır;
        çağıran (pool_bulk_cancel) döngü sonunda toplu çağırır.
        Default False → tekil iptal davranışı korunur.
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
    # FAZ C1: skip_recalculate=True ise toplu iptal akışı sonda tek çağrı yapar.
    if not skip_recalculate:
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


# ═════════════════════════════════════════════════════════════════════════════
# B-FAZ 7 — Bilezik Havuz Detay Sayfası (Pool Detail & Ledger Page)
# ═════════════════════════════════════════════════════════════════════════════
# Hurda FAZ 12 ile birebir paralel: her bilezik havuzu için tam sayfa
# detay görünümü + audit trail tablosu + satır içi iptal akışı + toplu iptal.
# URL bazlı (deep-link, bookmark, multi-tab uyumlu); 3 yıllık binlerce
# satırlık ledger için ölçeklenir.
# ═════════════════════════════════════════════════════════════════════════════


@login_required(login_url='login')
def pool_detail(request, bracelet_id):
    """
    Bilezik havuzunun tam sayfa detay görünümü.

    URL: /bracelets/pool/<uuid:bracelet_id>/
    Template: management/bracelets/pool_detail.html

    UX FAZ A1 — Son işlem iptal edildiğinde 404 yerine nazik yönlendirme:
        Havuz boşaldığında (Bracelets.is_deleted=True veya Products.is_active=False)
        404 göstermek yerine kullanıcı bilezik ana listesine yönlendirilir;
        messages framework'ü base.html'deki SweetAlert bildirimini tetikler.
        Audit trail dokunulmaz; havuz DB'de korunur.
    """
    store = request.user.store
    bracelet = (Bracelets.objects
                .select_related('product')
                .filter(id=bracelet_id, store=store)
                .first())

    if (bracelet is None
            or bracelet.is_deleted
            or bracelet.product is None
            or not bracelet.product.is_active):
        if bracelet is None:
            messages.warning(
                request,
                "Aradığınız bilezik havuzu bulunamadı. Ana listeye yönlendirildiniz."
            )
        else:
            messages.info(
                request,
                "Bu havuzda iptal edilmemiş işlem kalmadığı için ana listeye "
                "yönlendirildiniz."
            )
        return redirect('bracelets:index')

    p = bracelet.product

    snap = StockSnapshot.objects.filter(product=p, store=store).first()

    current_gram = Decimal(str(snap.stock_gram)) if snap and snap.stock_gram else Decimal('0')
    wac_hs = Decimal(str(snap.weighted_avg_cost_hs)) if snap and snap.weighted_avg_cost_hs else Decimal('0')
    total_value_hs = (current_gram * wac_hs).quantize(Decimal('0.001'))

    ledger_count = StockLedger.objects.filter(product=p, store=store).count()

    last_entry = (StockLedger.objects
                  .filter(product=p, store=store)
                  .order_by('-created_on').first())
    last_activity_date = last_entry.created_on if last_entry else None

    total_sold_hs_data = (StockLedger.objects
                          .filter(product=p, store=store, reason=StockLedger.Reason.SALE)
                          .aggregate(total=Coalesce(Sum(F('quantity_gram') * F('unit_cost_hs')),
                                                    Value(Decimal('0')),
                                                    output_field=DecimalField())))
    total_sold_hs = total_sold_hs_data.get('total') or Decimal('0')

    supplier_ids = (Process.objects
                    .filter(product=p, store=store,
                            transaction_type='PURCHASE',
                            is_status='COMPLETED', is_deleted=False,
                            supplier__isnull=False)
                    .values_list('supplier_id', flat=True).distinct())
    suppliers = list(Suppliers.objects.filter(id__in=supplier_ids)
                     .order_by('company_name')
                     .values('id', 'company_name'))

    active_purchase_count = Process.objects.filter(
        product=p, store=store,
        transaction_type='PURCHASE',
        is_status='COMPLETED',
        is_deleted=False,
    ).count()

    from datetime import date, timedelta
    today_iso = date.today().isoformat()
    default_date_from = (date.today() - timedelta(days=30)).isoformat()

    context = {
        'page_heading': f'{p.name} Havuzu',
        'bracelet_id': str(bracelet.id),
        'product_id': str(p.id),
        'pool_name': p.name,
        'pool_milyem': int(p.product_mileage or 0),
        'is_active': bool(p.is_active),
        'is_completed': bool(p.is_completed),

        'current_gram': d_fmt(current_gram, 3),
        'wac_hs': d_fmt(wac_hs, 3),
        'total_value_hs': d_fmt(total_value_hs, 3),
        'ledger_count': ledger_count,
        'total_sold_hs': d_fmt(total_sold_hs, 3),
        'last_activity_date': last_activity_date.strftime('%d %b %Y · %H:%M') if last_activity_date else '-',

        'suppliers': suppliers,
        'back_url': reverse('bracelets:index'),
        'back_label': 'Bilezik Listesi',

        'active_purchase_count': active_purchase_count,
        'default_date_from': default_date_from,
        'default_date_to': today_iso,

        # FAZ 22 — Active Stock Origin View
        'pool_is_empty': (current_gram <= Decimal('0')),
    }
    return render(request, 'management/bracelets/pool_detail.html', context)


@login_required(login_url='login')
@require_http_methods(["GET"])
def pool_process_detail(request, bracelet_id, process_no):
    """
    FAZ 22 — Tek bir işlemin detay JSON'u (Bilezik havuzu).

    Hurda pool_process_detail ile birebir aynı sözleşme. URL üzerinden gelen
    process_no'ya ait Process kaydı + tüm StockLedger satırları (orijinal + reversal)
    döner. Modal içinde stok giriş/çıkışının bağlamı görüntülenir.
    """
    store = request.user.store
    try:
        bracelet = Bracelets.objects.select_related('product').get(
            id=bracelet_id, store=store, is_deleted=False,
        )
    except Bracelets.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Havuz bulunamadı.'}, status=404)

    product = bracelet.product
    if not product:
        return JsonResponse({'error': True, 'error_msg': 'Havuza bağlı ürün yok.'}, status=404)

    process_no = (process_no or '').strip()
    if not process_no:
        return JsonResponse({'error': True, 'error_msg': 'İşlem numarası eksik.'}, status=400)

    proc = (Process.objects
            .select_related('supplier', 'customer', 'created_by')
            .filter(product=product, store=store, process_no=process_no)
            .first())

    ledger_rows = list(
        StockLedger.objects
        .filter(product=product, store=store, ref_id=process_no)
        .select_related('created_by')
        .order_by('created_on', 'id')
    )

    if not proc and not ledger_rows:
        return JsonResponse({
            'error': True,
            'error_msg': 'Bu işleme ait kayıt bulunamadı.',
        }, status=404)

    process_info = None
    if proc:
        if proc.supplier_id and proc.supplier:
            _source_name = proc.supplier.company_name
            _source_kind = 'supplier'
        elif proc.customer_id and proc.customer:
            _source_name = (
                f"{proc.customer.first_name or ''} {proc.customer.last_name or ''}"
            ).strip() or 'Müşteri'
            _source_kind = 'customer'
        else:
            _source_name = 'Tedarikçisiz'
            _source_kind = 'none'

        _iscilik = None
        try:
            _ism = getattr(proc, 'iscilik_milyem', None)
            if _ism is not None and Decimal(str(_ism)) > 0:
                _iscilik = d_fmt(Decimal(str(_ism)), 2)
        except Exception:
            _iscilik = None

        process_info = {
            'process_no': proc.process_no,
            'transaction_type': proc.transaction_type or '-',
            'date': proc.date.strftime('%d %b %Y · %H:%M') if proc.date else '-',
            'date_iso': proc.date.isoformat() if proc.date else None,
            'source_name': _source_name,
            'source_kind': _source_kind,
            'gram': d_fmt(Decimal(str(proc.gram or 0)), 3),
            'milyem': int(getattr(proc, 'process_mileage', 0) or 0),
            'iscilik_milyem': _iscilik,
            'is_status': proc.is_status or '-',
            'is_deleted': bool(proc.is_deleted),
            'created_by': (proc.created_by.get_full_name() or proc.created_by.username
                           if proc.created_by_id and proc.created_by else '-'),
        }

    REASON_LABELS = {
        'PURCHASE': 'Tedarikçi Alışı', 'SALE': 'Satış',
        'RETURN_IN': 'Satış İadesi', 'RETURN_OUT': 'Alış İadesi',
        'CONV_OUT': 'Dönüşüm Çıkışı', 'CONV_IN': 'Dönüşüm Girişi',
        'XFER_OUT': 'Transfer Çıkış', 'XFER_IN': 'Transfer Giriş',
        'ADJ_PLUS': 'Düzeltme (+)', 'ADJ_MINUS': 'Düzeltme (-)',
        'INITIAL': 'Açılış Stoğu', 'SCRAP_MELT': 'Eritme/Fire',
        'REPAIR_IN': 'Tamir Giriş', 'REPAIR_OUT': 'Tamir Çıkış',
    }
    ledger_payload = []
    has_reversal = False
    for lr in ledger_rows:
        _rt = lr.ref_type or ''
        _is_rev = _rt.endswith('_cancel')
        if _is_rev:
            has_reversal = True
        _g = Decimal(str(lr.quantity_gram or 0))
        _u = Decimal(str(lr.unit_cost_hs or 0))
        ledger_payload.append({
            'id': str(lr.id),
            'date': lr.created_on.strftime('%d %b %Y · %H:%M') if lr.created_on else '-',
            'direction': lr.direction,
            'reason': lr.reason,
            'reason_label': 'İPTAL ('+REASON_LABELS.get(lr.reason, lr.reason)+')' if _is_rev
                            else REASON_LABELS.get(lr.reason, lr.reason),
            'is_reversal': _is_rev,
            'gram': d_fmt(_g, 3),
            'unit_hs': d_fmt(_u, 3) if _u > 0 else '-',
            'total_hs': d_fmt((_g * _u).quantize(Decimal('0.001')), 3) if _u > 0 else '-',
            'ref_type': _rt or '-',
            'notes': (lr.notes or '')[:240],
            'created_by': (lr.created_by.get_full_name() or lr.created_by.username
                           if lr.created_by_id and lr.created_by else '-'),
        })

    return JsonResponse({
        'result': True,
        'process_no': process_no,
        'process': process_info,
        'ledger_rows': ledger_payload,
        'has_reversal': has_reversal,
        'pool_name': product.name,
    })


@login_required(login_url='login')
@require_http_methods(["GET"])
def pool_ledger(request, bracelet_id):
    """
    Bilezik havuzuna ait StockLedger satırlarını DataTables formatında döner.
    Hurda pool_ledger ile birebir aynı sözleşme.
    """
    store = request.user.store
    try:
        bracelet = Bracelets.objects.select_related('product').get(
            id=bracelet_id, store=store, is_deleted=False,
        )
    except Bracelets.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Havuz bulunamadı.'}, status=404)

    product = bracelet.product
    if not product:
        return JsonResponse({'error': True, 'error_msg': 'Havuza bağlı ürün yok.'}, status=404)

    try:
        draw = int(request.GET.get('draw', 1))
        length = int(request.GET.get('length', 50))
        start = int(request.GET.get('start', 0))
    except ValueError:
        draw, length, start = 1, 50, 0

    base_qs = StockLedger.objects.filter(product=product, store=store)
    total_records = base_qs.count()

    annotated = base_qs.annotate(
        signed_gram=Case(
            When(direction='IN', then=F('quantity_gram')),
            default=-F('quantity_gram'),
            output_field=DecimalField(max_digits=14, decimal_places=4),
        ),
    ).annotate(
        running_balance=Window(
            expression=Sum('signed_gram'),
            order_by=F('created_on').asc(),
        ),
    )

    date_from = (request.GET.get('date_from') or '').strip()
    date_to = (request.GET.get('date_to') or '').strip()
    direction_f = (request.GET.get('direction') or '').strip().upper()
    reason_f = (request.GET.get('reason') or '').strip().upper()
    supplier_id_f = (request.GET.get('supplier_id') or '').strip()
    hide_cancelled = (request.GET.get('hide_cancelled') or '').strip() in ('1', 'true', 'yes')
    # FAZ 22 — Active Stock Origin View (Hurda ile birebir simetri)
    # show_consumed: FIFO ile tüketilmiş IN satırlarını ekrandan gizle/göster.
    # Varsayılan gizli; "Tüketilmişi Göster" toggle'ı ile listeye eklenir.
    # Veritabanından hiçbir şey silinmez.
    show_consumed = (request.GET.get('show_consumed') or '').strip() in ('1', 'true', 'yes')
    search_value = (request.GET.get('search[value]') or request.GET.get('search') or '').strip()

    filtered_ids_qs = base_qs.all()
    if date_from:
        filtered_ids_qs = filtered_ids_qs.filter(created_on__date__gte=date_from)
    if date_to:
        filtered_ids_qs = filtered_ids_qs.filter(created_on__date__lte=date_to)
    if direction_f in ('IN', 'OUT'):
        filtered_ids_qs = filtered_ids_qs.filter(direction=direction_f)
    if reason_f:
        filtered_ids_qs = filtered_ids_qs.filter(reason=reason_f)
    if supplier_id_f:
        proc_nos = Process.objects.filter(
            product=product, store=store,
            supplier_id=supplier_id_f,
        ).values_list('process_no', flat=True)
        filtered_ids_qs = filtered_ids_qs.filter(ref_id__in=list(proc_nos))
    if search_value:
        proc_nos_match = Process.objects.filter(
            product=product, store=store,
            supplier__company_name__icontains=search_value,
        ).values_list('process_no', flat=True)
        filtered_ids_qs = filtered_ids_qs.filter(
            Q(ref_id__icontains=search_value) |
            Q(notes__icontains=search_value) |
            Q(ref_id__in=list(proc_nos_match))
        )

    # ─── İPTAL TESPİTİ — SAĞLAM REF_TYPE TABANLI (FAZ 15) ───
    # cancel_service.py kuralı: reversal kayıtları DAİMA
    # ref_type=f"{orig_ref_type}_cancel" ile yazılır ve ref_id ORİJİNAL ile
    # aynı kalır. Eski `notes__icontains='IPTAL'` tespiti legacy kayıtlarda
    # tutarsızdı → zaten iptal edilmiş satırlar 'Aktif' görünüp İptal butonu
    # çift-iptal hatası üretiyordu (InsufficientStockError). Hurda FAZ 15
    # ile birebir simetri.
    reversal_pairs_qs = base_qs.filter(
        ref_type__endswith='_cancel'
    ).values_list('ref_type', 'ref_id')
    reversed_originals = set()
    reversal_ref_ids = set()
    for _rt, _rid in reversal_pairs_qs:
        if _rt and _rt.endswith('_cancel'):
            _orig_rt = _rt[:-len('_cancel')]
            reversed_originals.add((_orig_rt, _rid))
            if _rid:
                reversal_ref_ids.add(_rid)

    if hide_cancelled:
        # 1) Reversal satırlarının kendisini gizle
        filtered_ids_qs = filtered_ids_qs.exclude(ref_type__endswith='_cancel')
        # 2) Reverse edilmiş orijinal satırları (ref_type, ref_id) çifti üzerinden gizle
        if reversed_originals:
            _candidate_ids = list(
                filtered_ids_qs.filter(
                    direction='IN',
                    ref_id__in=list(reversal_ref_ids),
                ).values_list('id', flat=True)
            )
            if _candidate_ids:
                _all_match_rows = list(
                    StockLedger.objects.filter(id__in=_candidate_ids)
                    .values('id', 'ref_type', 'ref_id')
                )
                _drop_ids = [
                    r['id'] for r in _all_match_rows
                    if (r['ref_type'], r['ref_id']) in reversed_originals
                ]
                if _drop_ids:
                    filtered_ids_qs = filtered_ids_qs.exclude(id__in=_drop_ids)

    # ─── FAZ 22 — FIFO TÜKETİM HESABI (Hurda ile birebir simetri) ───
    # Detaylı açıklama için scraps/views.py:pool_ledger içindeki yorum bloğuna bakın.
    pool_remaining_map = {}
    pool_original_map = {}
    consumed_in_ids = set()

    _all_in_rows = list(
        base_qs.filter(direction='IN')
        .order_by('created_on', 'id')
        .values('id', 'ref_type', 'ref_id', 'quantity_gram')
    )

    _net_out_agg = (
        base_qs.filter(direction='OUT')
        .exclude(ref_type__endswith='_cancel')
        .aggregate(total=Coalesce(Sum('quantity_gram'),
                                   Value(Decimal('0')),
                                   output_field=DecimalField()))
    )
    _remaining_pool_out = Decimal(str(_net_out_agg.get('total') or 0))

    for _in in _all_in_rows:
        _in_id = _in['id']
        _in_rt = _in['ref_type'] or ''
        _in_rid = _in['ref_id']
        _in_gram = Decimal(str(_in['quantity_gram'] or 0))
        pool_original_map[_in_id] = _in_gram

        if _in_rt.endswith('_cancel'):
            pool_remaining_map[_in_id] = Decimal('0')
            consumed_in_ids.add(_in_id)
            continue
        if (_in_rt, _in_rid) in reversed_originals:
            pool_remaining_map[_in_id] = Decimal('0')
            consumed_in_ids.add(_in_id)
            continue

        if _remaining_pool_out >= _in_gram:
            _remaining_pool_out -= _in_gram
            pool_remaining_map[_in_id] = Decimal('0')
            consumed_in_ids.add(_in_id)
        elif _remaining_pool_out > 0:
            _remaining = _in_gram - _remaining_pool_out
            _remaining_pool_out = Decimal('0')
            pool_remaining_map[_in_id] = _remaining
        else:
            pool_remaining_map[_in_id] = _in_gram

    if not show_consumed and consumed_in_ids:
        filtered_ids_qs = filtered_ids_qs.exclude(id__in=list(consumed_in_ids))

    filtered_ids = list(filtered_ids_qs.values_list('id', flat=True))
    filtered_records = len(filtered_ids)

    rows_qs = annotated.filter(id__in=filtered_ids).order_by('-created_on', '-id')

    if length != -1:
        rows_qs = rows_qs[start:start + length]

    page_ref_ids = [r.ref_id for r in rows_qs if r.ref_id]
    proc_map = {}
    if page_ref_ids:
        procs = Process.objects.filter(
            product=product, store=store,
            process_no__in=page_ref_ids,
        ).select_related('supplier', 'customer')
        for pr in procs:
            proc_map[pr.process_no] = pr

    REASON_LABELS = {
        'PURCHASE': ('Tedarikçi Alışı', 'success'),
        'SALE': ('Satış', 'primary'),
        'RETURN_IN': ('Satış İadesi', 'info'),
        'RETURN_OUT': ('Alış İadesi', 'warning'),
        'CONV_OUT': ('Dönüşüm Çıkışı', 'warning'),
        'CONV_IN': ('Dönüşüm Girişi', 'info'),
        'XFER_OUT': ('Transfer Çıkış', 'warning'),
        'XFER_IN': ('Transfer Giriş', 'info'),
        'ADJ_PLUS': ('Düzeltme (+)', 'info'),
        'ADJ_MINUS': ('Düzeltme (-)', 'warning'),
        'INITIAL': ('Açılış Stoğu', 'secondary'),
        'SCRAP_MELT': ('Eritme/Fire', 'danger'),
        'REPAIR_IN': ('Tamir Giriş', 'info'),
        'REPAIR_OUT': ('Tamir Çıkış', 'warning'),
    }

    data = []
    for row in rows_qs:
        gram = Decimal(str(row.quantity_gram or 0))
        unit_hs = Decimal(str(row.unit_cost_hs or 0))
        total_hs = (gram * unit_hs).quantize(Decimal('0.001'))
        signed_gram = gram if row.direction == 'IN' else -gram
        # FAZ 15 — Sağlam tespit (Hurda ile simetri):
        _row_ref_type = row.ref_type or ''
        is_cancellation = _row_ref_type.endswith('_cancel')
        is_cancelled = (
            (not is_cancellation)
            and ((_row_ref_type, row.ref_id) in reversed_originals)
        )

        proc = proc_map.get(row.ref_id) if row.ref_id else None
        supplier_name = ''
        source_kind = ''
        if proc:
            if proc.supplier_id and proc.supplier:
                supplier_name = proc.supplier.company_name
                source_kind = 'supplier'
            elif proc.customer_id and proc.customer:
                supplier_name = (
                    f"{proc.customer.first_name or ''} {proc.customer.last_name or ''}"
                ).strip() or 'Müşteri'
                source_kind = 'customer'
            else:
                supplier_name = 'Tedarikçisiz'
                source_kind = 'none'
        else:
            if row.ref_type == 'initial':
                supplier_name = 'Açılış Stoğu'
                source_kind = 'initial'
            elif row.ref_type == 'adjustment':
                supplier_name = 'Manuel Düzeltme'
                source_kind = 'adjustment'
            elif row.ref_type == 'conversion':
                supplier_name = 'Dönüşüm İşlemi'
                source_kind = 'conversion'
            else:
                supplier_name = '-'
                source_kind = 'none'

        if is_cancellation:
            type_label = 'İPTAL'
            type_color = 'danger'
        else:
            type_label, type_color = REASON_LABELS.get(
                row.reason, (row.reason, 'secondary')
            )

        # ─── FAZ 22 — FIFO Tüketim Bilgisi (Hurda ile birebir simetri) ───
        _orig_gram = pool_original_map.get(row.id, gram)
        _remaining = pool_remaining_map.get(row.id)
        if _remaining is None:
            remaining_gram_val = None
            is_consumed = False
            is_partial = False
        else:
            remaining_gram_val = _remaining
            is_consumed = (_remaining <= Decimal('0'))
            is_partial = (_remaining > Decimal('0')) and (_remaining < _orig_gram)

        # FAZ 22 — Sadece havuzda HÂLÂ AKTİF GRAMI BULUNAN PURCHASE'lar iptal edilebilir.
        # Tüketilmiş satırlar için cancel_service "Insufficient stock" hatası üretirdi;
        # buton göstererek kullanıcıyı yanlış yönlendirmek yerine gizliyoruz.
        can_cancel = (
            row.reason == 'PURCHASE'
            and row.direction == 'IN'
            and not is_cancelled
            and not is_cancellation
            and bool(row.ref_id)
            and remaining_gram_val is not None
            and remaining_gram_val > Decimal('0')
        )

        running_bal = Decimal(str(row.running_balance or 0))

        # FAZ 19 / Bulgu B — İlgili Process'in işçilik milyemini ledger satırına ekle
        _iscilik_milyem_val = None
        if proc is not None:
            try:
                _ism = getattr(proc, 'iscilik_milyem', None)
                if _ism is not None and Decimal(str(_ism)) > 0:
                    _iscilik_milyem_val = d_fmt(Decimal(str(_ism)), 2)
            except Exception:
                _iscilik_milyem_val = None

        data.append({
            'id': str(row.id),
            'ledger_id': str(row.id),
            'date_iso': row.created_on.isoformat() if row.created_on else None,
            'date_short': row.created_on.strftime('%d %b %Y · %H:%M') if row.created_on else '-',
            'direction': row.direction,
            'reason': row.reason,
            'type_label': type_label,
            'type_color': type_color,
            'is_cancellation': is_cancellation,
            'is_cancelled': is_cancelled,
            'source_name': supplier_name,
            'source_kind': source_kind,
            'gram_signed': d_fmt(signed_gram, 3),
            'gram_abs': d_fmt(gram, 3),
            'unit_hs': d_fmt(unit_hs, 3) if unit_hs > 0 else '-',
            'total_hs': d_fmt(total_hs, 3) if total_hs > 0 else '-',
            'running_balance': d_fmt(running_bal, 3),
            'ref_no': row.ref_id or '-',
            'ref_no_short': (row.ref_id[-6:] if row.ref_id else '-'),
            'ref_type': row.ref_type or '-',
            'notes': (row.notes or '')[:120],
            'can_cancel': can_cancel,
            'process_no': proc.process_no if proc else None,
            'iscilik_milyem': _iscilik_milyem_val,
            # FAZ 22 — FIFO tüketim bilgisi (Active Stock Origin View)
            'original_gram': d_fmt(_orig_gram, 3) if remaining_gram_val is not None else None,
            'remaining_gram': d_fmt(remaining_gram_val, 3) if remaining_gram_val is not None else None,
            'is_consumed': is_consumed,
            'is_partial': is_partial,
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': filtered_records,
        'data': data,
    })


@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('BRACELETS_DELETE')
@transaction.atomic
def pool_bulk_cancel(request, bracelet_id):
    """
    Havuzdaki tüm aktif PURCHASE Process kayıtlarını tek seferde iptal eder.

    REFACTOR (Bilezik İptal SSOT):
        Her bir kayıt için merkezi `cancel_bracelet_purchase` servisi çağrılır.
        @transaction.atomic: Döngüdeki herhangi bir Process iptali başarısız
        olursa TÜM işlem geri alınır — yarı iptal durumu oluşmaz.

        Eski `failed_count/errors` per-iter except mantığı KALDIRILDI:
        @transaction.atomic içinde IntegrityError yakalandığında transaction
        "broken" duruma düşer ve sonraki SQL TransactionManagementError fırlatır.
        Atomik bütünlük + per-iter recover birbiriyle çelişir.

    Audit trail TAM korunur: HİÇBİR satır silinmez.
    """
    from apps.bracelets.services.cancel_bracelet_service import (
        cancel_bracelet_purchase,
        CancelNotAllowedError,
        CancelBraceletError,
    )

    store = request.user.store
    try:
        bracelet = Bracelets.objects.select_related('product').get(
            id=bracelet_id, store=store, is_deleted=False,
        )
    except Bracelets.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Havuz bulunamadı.'}, status=404)

    product = bracelet.product
    if not product:
        return JsonResponse({'result': False, 'error_msg': 'Havuza bağlı ürün yok.'}, status=404)

    active_procs = list(
        Process.objects.filter(
            product=product, store=store,
            transaction_type='PURCHASE',
            is_status='COMPLETED',
            is_deleted=False,
        ).order_by('-date')
    )

    if not active_procs:
        return JsonResponse({
            'result': False,
            'error_msg': 'Bu havuzda iptal edilebilecek aktif işlem yok.',
        })

    cancelled_count = 0
    total_cancelled_gram = Decimal('0')

    # skip_recalculate=True: döngü içinde N kez değil, sonda 1 kez recalculate.
    for proc in active_procs:
        cancel_bracelet_purchase(
            process_id=proc.id,
            user=request.user,
            skip_recalculate=True,
        )
        cancelled_count += 1
        total_cancelled_gram += Decimal(str(proc.gram or 0))

    # Toplu iptal sonrası WAC ve milyem'i TEK seferde recalculate et.
    try:
        recalculate_bracelet_pool_mileage_after_cancel(product=product, store=store)
    except Exception as e:
        logger.error(f"bracelet_pool_bulk_cancel: final recalculate failed: {e}")

    snap = StockSnapshot.objects.filter(product=product, store=store).first()
    pool_archived = False
    if snap and snap.stock_gram <= Decimal('0'):
        Products.objects.filter(id=product.id).update(is_active=False)
        bracelet.is_deleted = True
        bracelet.is_active = False
        bracelet.save(update_fields=['is_deleted', 'is_active'])
        pool_archived = True

    return JsonResponse({
        'result': True,
        'cancelled_count': cancelled_count,
        'failed_count': 0,
        'errors': [],
        'total_cancelled_gram': str(total_cancelled_gram.quantize(Decimal('0.001'))),
        'pool_archived': pool_archived,
    })
