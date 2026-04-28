# apps/scraps/views.py
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.db.models import (
    Q, OuterRef, Subquery, CharField, DecimalField, IntegerField,
    Exists, F, Count,
)
from django.db.models.functions import Greatest, Coalesce

logger = logging.getLogger(__name__)

from apps.helpers.numbers import parse_decimal_locale
from apps.products.models import Products
from apps.scraps.models import Scraps
from apps.definitions.categories.models import Categories
from apps.process.models import Process
from apps.roles.decorators import role_required

# --- FAZ 3: StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService

# --- ONARIM FAZI 4 / ADIM 3 — Cari sıfırlanma bug fix ---
# Manuel SupplierLedger.update(is_active=False) yerine evrensel
# cancel_stock_entry kullanılır; reversal kaydı + soft-disable atomik.
from apps.stock_management.services.cancel_service import cancel_stock_entry

# --- Tedarikçi/Cari entegrasyonu ---
from apps.suppliers.models import Suppliers, SupplierLedger
from apps.process.models import Process
from apps.process.views import generate_process_no


# ----------------- yardımcılar -----------------

def d_quantize(val: Decimal, places: int) -> Decimal:
    """Decimal'ı verilen ondalık sayıda yuvarla (ROUND_HALF_UP)."""
    if places <= 0:
        q = Decimal('1')
    else:
        q = Decimal('1').scaleb(-places)  # 3 → 0.001
    return (val or Decimal('0')).quantize(q, rounding=ROUND_HALF_UP)


def d_fmt(val: Decimal, max_places: int = 6) -> str:
    """Decimal → gereksiz sıfırlar atılmış string. 585.000 → '585', 1.230 → '1.23'.

    ONARIM FAZI 8 — Yapısal kusur düzeltmesi:
    Önceki sürüm `f"{Decimal('590'):f}"` → "590" çıktısında ondalık nokta
    olmadığından `rstrip('0')` tam sayı kısmındaki sondaki sıfırları da
    kırpıyor ve 590 → "59" üretiyordu. Sıfır kırpma artık YALNIZCA
    ondalık nokta varlığında uygulanır.
    """
    if val is None:
        return ""
    v = d_quantize(Decimal(val), max_places)
    s = f"{v:f}"
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or "0"


def karat_from_mileage(mileage) -> int:
    """
    Milyem (0-1000) değerinden ayar (karat, 0-24) hesaplar.
    585 → 14, 750 → 18, 916 → 22, 995 → 24.

    ONARIM FAZI 9 — FLOOR semantiği:
        Eski kod ROUND_HALF_UP kullanıyordu; bu, 605 milyem → 14.52 → 15
        sonucu üretiyor ve "14 ayar" olarak girilen 605 milyemli hurdayı
        15 karat sınıfına atıyordu. Kuyum sektörü konvansiyonunda ayar
        sınıfı "taban" mantığıyla çalışır: 14 ayar = [560, 624] milyem
        aralığı (her ikisi dahil). int() truncation (floor) bu davranışı
        tam karşılar:
            14.04 → 14, 14.28 → 14, 14.52 → 14, 14.99 → 14, 15.00 → 15

        DİKKAT: Hurda havuz birleştirmesi artık `karat_from_mileage` yerine
        kullanıcının seçtiği ayar adı (`scrap_name = "14 Ayar"`) üzerinden
        yapılır (`find_scrap_pool_by_selected_karat`). Bu fonksiyon yalnız
        eski kod yolları ve fallback için bırakılmıştır.
    """
    try:
        m = Decimal(str(mileage))
    except Exception:
        return 0
    if m <= 0:
        return 0
    return int(m / Decimal('1000') * Decimal('24'))


# ============================================================================
# ONARIM FAZI 9 — KULLANICI-SEÇİMLİ AYAR HAVUZLAMASI
# ============================================================================
#
# Kuyumcu hurda havuzlama kuralının tek doğru kaynağı kullanıcının seçtiği
# AYAR (örn. "14 Ayar") olmalıdır — milyem değerinden TÜRETİLMEZ.
#
# Örnek (UAT bulgusu):
#   - Kullanıcı "14 Ayar" seçer, milyem 595 girer  → 14 Ayar havuzu
#   - Kullanıcı "14 Ayar" seçer, milyem 605 girer  → 14 Ayar havuzu (AYNI)
#   - Kullanıcı "14 Ayar" seçer, milyem 995 girer  → 14 Ayar havuzu (AYNI)
#
# Eski `karat_from_mileage` tabanlı eşleştirme 605 milyemi 15 karat'a
# atayıp birleşmeyi engelliyordu. Yeni mantık `Products.name` üzerinden
# canonical "X Ayar" etiketiyle eşleşir.
# ============================================================================

# Kuyumcu konvansiyonunda kabul edilen ayar sınıfları → canonical etiket
SCRAP_KARAT_LABELS = {
    8:  '8 Ayar',
    10: '10 Ayar',
    14: '14 Ayar',
    18: '18 Ayar',
    21: '21 Ayar',
    22: '22 Ayar',
    24: '24 Ayar',
}

# "14 Ayar", "14 ayar", "14   AYAR", "14Ayar Hurda", "Hurda 14 Ayar" → 14
_KARAT_LABEL_RE = re.compile(r'(\d{1,2})\s*[Aa][Yy][Aa][Rr]')


def extract_scrap_karat_label(scrap_name=None, fallback_mileage=None, material_type='GOLD'):
    """
    Hurda havuz anahtarı: kullanıcının seçtiği ayar adından canonical
    "X Ayar" etiketi türetir.

    Öncelik sırası:
        1. `scrap_name` "X Ayar" kalıbına uyuyorsa ve X SCRAP_KARAT_LABELS'ta
           tanımlıysa → canonical etiket döner ("14 Ayar")
        2. `scrap_name` özel/kullanıcı tanımlı bir isimse (örn. "Eski Yüzük
           Hurdası") → kullanıcının yazdığı stripped haliyle döner. Bu kayıt
           kendi havuzuna gider; başka havuzla birleşmez (kasıtlı izolasyon).
        3. `scrap_name` boş/None ise `fallback_mileage`'tan karat hesaplanır
           (floor) ve canonical etiket döner.
        4. Hiçbiri sonuç vermezse None.

    Args:
        scrap_name: Form'dan gelen ayar adı (örn. "14 Ayar"). Boş olabilir.
        fallback_mileage: scrap_name yoksa karat türetimi için kullanılır.
        material_type: GOLD/SILVER. Şu an etiketleme tek havuz kümesi için
            yapılıyor (SILVER ayrı izolasyon name + material_type filter ile).

    Returns:
        str | None: Canonical etiket veya None.
    """
    if scrap_name:
        s = str(scrap_name).strip()
        if s:
            m = _KARAT_LABEL_RE.search(s)
            if m:
                try:
                    k = int(m.group(1))
                    if k in SCRAP_KARAT_LABELS:
                        return SCRAP_KARAT_LABELS[k]
                except (ValueError, TypeError):
                    pass
            # Standart kalıba uymayan özel isim — kullanıcı kasıtlı yazdı
            # sayılır, kendi havuzuna gider.
            return s
    if fallback_mileage is not None:
        try:
            mil = Decimal(str(fallback_mileage))
            if mil > 0:
                k = int(mil / Decimal('1000') * Decimal('24'))  # floor
                if k in SCRAP_KARAT_LABELS:
                    return SCRAP_KARAT_LABELS[k]
        except Exception:
            pass
    return None


def find_scrap_pool_by_selected_karat(store, category, scrap_name=None,
                                       fallback_mileage=None, is_scrap=True,
                                       material_type='GOLD'):
    """
    ONARIM FAZI 9 — Hurda havuz bulma fonksiyonu (yeni temel).

    Aynı `store + category + material_type + canonical karat etiketi`
    grubundaki AKTİF havuzu döner. Eşleşme `Products.name` (case-insensitive
    exact) üzerindendir. Milyem değerinden BAĞIMSIZ — kullanıcı "14 Ayar"
    seçtiyse 595 milyem de 605 milyem de 995 milyem de aynı havuza düşer.

    Args:
        scrap_name: Form'dan gelen ayar adı. Tercih edilen anahtar.
        fallback_mileage: scrap_name yoksa karat türetimi için kullanılır
            (geriye dönük uyumluluk).

    Returns:
        Products | None
    """
    mat = (material_type or '').upper()
    if mat not in ('GOLD', 'SILVER'):
        raise ValueError(
            f"find_scrap_pool_by_selected_karat: gecersiz material_type="
            f"'{material_type}'. Sadece 'GOLD' veya 'SILVER' kabul edilir."
        )

    karat_label = extract_scrap_karat_label(scrap_name, fallback_mileage, mat)
    if not karat_label:
        return None

    return Products.objects.filter(
        store=store, category=category, is_scrap=is_scrap,
        is_deleted=False,
        material_type=mat,
        name__iexact=karat_label,
    ).order_by('created_on', 'id').first()


def find_scrap_pool_by_karat(store, category, new_mileage, is_scrap=True, material_type='GOLD'):
    """
    DEPRECATED — Geriye dönük uyumluluk için saklanır. Yeni kod
    `find_scrap_pool_by_selected_karat` kullanmalı (kullanıcının seçtiği
    ayar adıyla doğru havuzu bulur). Bu wrapper milyem'den karat türeten
    eski davranışı sürdürür ama floor semantiğine geçmiş `karat_from_mileage`
    sayesinde eski ROUND_HALF_UP sınır kayması ortadan kalkmıştır.

    ONARIM FAZI 4 / ADIM 4 — STRICT IZOLASYON: Geçersiz material_type artık
    sessizce GOLD'a düşmez; explicit ValueError fırlatılır.
    """
    return find_scrap_pool_by_selected_karat(
        store=store, category=category,
        scrap_name=None, fallback_mileage=new_mileage,
        is_scrap=is_scrap, material_type=material_type,
    )


def update_scrap_pool_weighted_mileage(product, store, new_gram: Decimal, new_mileage: Decimal):
    """
    Mevcut hurda havuzunun üzerine yeni gramaj eklenirken milyemi AĞIRLIKLI
    ORTALAMA formülüyle güncelle ve Products tablosundaki maliyet alanlarını
    senkronize et.

    Formül:
        yeni_milyem = ((mevcut_gram * mevcut_milyem) + (yeni_gram * yeni_milyem)) / toplam_gram

    Bu fonksiyon, stok hareketinden BAĞIMSIZ şekilde yalnızca meta-veri
    (milyem + birim has maliyeti) günceller. Asıl stok hareketi/gram birikimi
    StockService.record_entry (snapshot WAC) ve ayrıca Products.gram'ı
    yöneten çağrı sahibi tarafından yapılır.

    ONARIM FAZI 5 / ADIM A — full_clean() bariyerinin ATLATILMASI:
        Eski kod: instance.save(update_fields=[...]) → Products.save() override'ı
        full_clean() çağırıyor → clean() instance'ın TÜM alanlarını
        (özellikle gram'ı) doğruluyor. Eğer Products.gram veritabanında
        negatif kalmışsa (bkz. ADIM B) ValidationError fırlatıyor:
            {'gram': ['Gram negatif olamaz.']}
        Yeni kod: Products.objects.filter(id=...).update(...) ile atomic
        SQL UPDATE — instance dirty olmaz, full_clean() tetiklenmez.
        scrap_add satır 395'teki F('gram') + gram deseniyle tutarlıdır.

    ONARIM FAZI 5 / ADIM C — select_for_update():
        StockSnapshot satırı row-lock ile okunur. Aynı havuza eş zamanlı
        gelen iki hurda girişi aynı current_gram'ı okuyup birbirinin
        WAC'ını ezemez. scrap_add zaten @transaction.atomic içinde olduğu
        için select_for_update anlamlıdır.

    ONARIM FAZI 5 / ADIM F — stock_gram null guard:
        Decimal('0') falsy olduğu için 'if snap.stock_gram' eski kontrolü
        'snapshot var ama stok 0' ile 'snapshot yok' arasında ayrım
        yapamıyordu. 'is not None' ile bu ayrım netleştirildi.

    Returns: (new_mileage_int, new_buy_price_hs) tuple — ileride loglama için.
    """
    try:
        new_gram = Decimal(str(new_gram or 0))
        new_mileage = Decimal(str(new_mileage or 0))
    except Exception:
        return None, None

    if new_gram <= 0 or new_mileage <= 0 or product is None:
        return None, None

    # ADIM C: select_for_update — race condition koruması
    snap = (
        StockSnapshot.objects
        .select_for_update()
        .filter(product=product, store=store)
        .first()
    )
    # ADIM F: Decimal('0') is not None → snapshot var ama stok=0 doğru ayrılır
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
        # İlk giriş veya snapshot henüz oluşmamış → yeni giren tamamen belirleyici
        result_mileage = Decimal(int(new_mileage))

    new_buy_price_hs = d_quantize(result_mileage / Decimal('1000'), 3)

    # ADIM A: save() yerine atomic SQL UPDATE — full_clean() bariyerini atla.
    # Products.gram (legacy alan) negatif kalsa bile bu güncelleme patlamaz;
    # gram alanı bu UPDATE'te hiç dokunulmuyor.
    Products.objects.filter(id=product.id).update(
        product_mileage=result_mileage,
        buy_price_hs=new_buy_price_hs,
        sale_price_hs=new_buy_price_hs,
    )
    # Çağıran taraf güncel değerleri instance üzerinden okumak isteyebilir
    product.product_mileage = result_mileage
    product.buy_price_hs = new_buy_price_hs
    product.sale_price_hs = new_buy_price_hs

    return int(result_mileage), new_buy_price_hs


def recalculate_scrap_pool_mileage_after_cancel(product, store):
    """
    ONARIM FAZI 7 / BULGU 1 — İptal Sonrası WAC Geri Hesaplama
    ============================================================================
    Bir hurda havuzu girişinin iptali sonrasında havuzun ağırlıklı ortalama
    milyemini StockLedger üzerindeki HÂLÂ AKTİF (iptal edilmemiş) giriş
    kayıtlarından yeniden hesaplar ve `Products.product_mileage` +
    `Products.buy_price_hs` + `Products.sale_price_hs` +
    `StockSnapshot.weighted_avg_cost_hs` alanlarını günceller.

    Neden gerekli?
        `cancel_stock_entry` → `StockService.record_exit` çağırır ve sadece
        `StockSnapshot.stock_gram` alanını düşürür. WAC alanı (çıkışta sabit
        kalan tasarım ilkesi gereği) korunur. Sonuç: 10g@585 + 10g@595 →
        WAC=590, sonra 10g@595 iptal edilince stok 10g'a iner ama Products.
        product_mileage = 590 olarak KALIR (gerçek 585 olmalı).

    Algoritma:
        1. Aktif giriş kayıtlarını topla:
           StockLedger(product, store, direction=IN, reason∈{PURCHASE, INITIAL})
           ve ref_type '_cancel' ile bitmesin (reversal değil, orijinal giriş)
        2. Aynı (ref_type, ref_id) çiftine sahip bir reversal varsa
           (direction=OUT, reason=RETURN_OUT, ref_type='*_cancel'),
           bu giriş tamamen iptal edilmiş demektir → toplama dahil etme
        3. Geriye kalan girişler için:
              total_gram   = SUM(quantity_gram)
              weighted_sum = SUM(quantity_gram × unit_cost_hs)
              new_avg_hs   = weighted_sum / total_gram
              new_mileage  = round(new_avg_hs × 1000)
        4. total_gram = 0 ise (her şey iptal edilmiş) milyem = 0, has = 0
        5. Atomic UPDATE ile alanları güncelle (full_clean bypass)

    Args:
        product: Products instance (havuz ürünü)
        store:   Stores instance

    Returns:
        dict: {
            'new_mileage': int,
            'new_buy_price_hs': Decimal,
            'active_entry_count': int,
            'total_gram': Decimal,
        }
    """
    if product is None or store is None:
        return None

    # 1. Aktif (orijinal, iptal edilmemiş) giriş kayıtlarını topla
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

    # 2. Hangi (ref_type, ref_id) çiftleri reversal görmüş?
    #    Reversal kayıtları: direction=OUT, ref_type='*_cancel'
    #    `cancel_stock_entry` reversal'ı `ref_type=f"{orig_ref_type}_cancel"` ile yazar
    #    ve `ref_id` ORIJİNAL ref_id'yi korur.
    reversed_pairs = set()
    rev_qs = StockLedger.objects.filter(
        product=product,
        store=store,
        direction=StockLedger.Direction.OUT,
        ref_type__endswith='_cancel',
    ).values_list('ref_type', 'ref_id')
    for rev_ref_type, rev_ref_id in rev_qs:
        # 'scrap_add_cancel' → 'scrap_add' eşleşsin diye '_cancel' soyalım
        original_ref_type = rev_ref_type[:-len('_cancel')] if rev_ref_type.endswith('_cancel') else rev_ref_type
        reversed_pairs.add((original_ref_type, rev_ref_id))

    # 3. Ağırlıklı ortalama hesapla (reversal görmemiş girişler)
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

    # 4. Sonuç
    if total_gram > 0 and weighted_sum_hs > 0:
        avg_hs = weighted_sum_hs / total_gram
        # Milyem = avg_hs * 1000 (ör: 0.585 → 585)
        new_mileage = Decimal(int(
            (avg_hs * Decimal('1000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        ))
        new_buy_price_hs = d_quantize(new_mileage / Decimal('1000'), 3)
    else:
        # Tüm girişler iptal edilmiş veya giriş yok → milyem 0, fiyat 0
        new_mileage = Decimal('0')
        new_buy_price_hs = Decimal('0.000')

    # 5. Atomic UPDATE — full_clean() bypass (Products.gram negatif kalsa bile patlamaz)
    Products.objects.filter(id=product.id).update(
        product_mileage=new_mileage,
        buy_price_hs=new_buy_price_hs,
        sale_price_hs=new_buy_price_hs,
    )
    # StockSnapshot.weighted_avg_cost_hs'i de hizala — listede ve raporlarda
    # tutarlı görünmesi için. Snapshot çıkışta WAC'ı değiştirmediği için
    # iptal sonrası burada manuel düzeltilir.
    StockSnapshot.objects.filter(product=product, store=store).update(
        weighted_avg_cost_hs=new_buy_price_hs,
    )

    # Instance'ı da güncelle — çağıran taraf güncel değerleri okuyabilsin
    product.product_mileage = new_mileage
    product.buy_price_hs = new_buy_price_hs
    product.sale_price_hs = new_buy_price_hs

    return {
        'new_mileage': int(new_mileage),
        'new_buy_price_hs': new_buy_price_hs,
        'active_entry_count': active_count,
        'total_gram': total_gram,
    }


# ============================================================================
# ONARIM FAZI 9 — DUPLICATE HAVUZ BİRLEŞTİRME (UAT TEMİZLİĞİ)
# ============================================================================

def _canonical_pool_key(product):
    """Bir Products kaydının canonical havuz anahtarını döner.
    name "X Ayar" kalıbına uyuyorsa onu, uymuyorsa milyem-türevli karat
    etiketini, hiçbiri yoksa raw stripped name'i döner."""
    return extract_scrap_karat_label(
        scrap_name=getattr(product, 'name', None),
        fallback_mileage=getattr(product, 'product_mileage', None),
        material_type=getattr(product, 'material_type', 'GOLD') or 'GOLD',
    )


def merge_scrap_pool_duplicates(store, category=None, material_type='GOLD'):
    """
    Aynı `(store + material_type + canonical karat etiketi)` grubundaki
    birden fazla aktif `Products` kaydını TEK PRIMARY altında birleştirir.

    Algoritma:
        1. Aktif scrap havuzlarını canonical etikete göre grupla.
        2. Her grupta birden fazla varsa:
           - En eskisi (created_on ASC) → PRIMARY
           - Diğerleri → DUPLICATES
        3. Her duplicate için atomic transaction içinde:
           a. `Process.product` PRIMARY'ye taşınır
           b. `StockLedger.product` PRIMARY'ye taşınır (audit trail KORUNUR;
              tek değişiklik product foreign key'inin yeniden hedeflenmesi)
           c. `StockSnapshot` PRIMARY'ye birleştirilir:
                stock_gram   = sum(primary, duplicate)
                stock_pieces = sum(primary, duplicate)
                weighted_avg_cost_hs = (g1*c1 + g2*c2)/(g1+g2)
              Duplicate'in StockSnapshot satırı silinir.
           d. `Scraps.product` PRIMARY'ye taşınır VEYA mevcut Scraps satırı
              soft-delete edilir (primary'de zaten Scraps satırı varsa).
           e. Duplicate `Products` soft-delete:
                is_deleted=True, is_active=False
        4. PRIMARY'nin `product_mileage / buy_price_hs / sale_price_hs` ve
           `StockSnapshot.weighted_avg_cost_hs` alanları
           `recalculate_scrap_pool_mileage_after_cancel` ile yeniden tutarlı
           hale getirilir (aktif StockLedger girişlerinden ağırlıklı ortalama).

    Args:
        store: Stores instance — birleştirme tek mağaza içinde yapılır.
        category: Categories instance | None. None ise tüm Hurda kategorileri
            tarinanır.
        material_type: 'GOLD' veya 'SILVER'.

    Returns:
        dict: {
            'merged_groups': N,    # birden fazla havuzu olan grup sayısı
            'merged_pools': N,     # birleştirilen (kapatılan) duplicate sayısı
            'details': [
                {'karat_label': '14 Ayar', 'primary_id': '...',
                 'duplicates': ['<id1>', '<id2>']},
                ...
            ],
        }
    """
    from collections import defaultdict
    from apps.stock_management.models import StockLedger as _SL

    mat = (material_type or '').upper()
    if mat not in ('GOLD', 'SILVER'):
        raise ValueError(
            f"merge_scrap_pool_duplicates: gecersiz material_type='{material_type}'."
        )

    qs = Products.objects.filter(
        store=store,
        is_scrap=True,
        is_deleted=False,
        material_type=mat,
    ).order_by('created_on', 'id')
    if category is not None:
        qs = qs.filter(category=category)

    groups = defaultdict(list)
    for p in qs:
        key = _canonical_pool_key(p)
        if not key:
            continue
        groups[key].append(p)

    merged_groups = 0
    merged_pools = 0
    details = []

    for karat_label, members in groups.items():
        if len(members) < 2:
            continue
        merged_groups += 1
        primary = members[0]
        duplicates = members[1:]

        with transaction.atomic():
            for dup in duplicates:
                # a) Process kayıtlarını taşı
                Process.objects.filter(
                    store=store, product=dup
                ).update(product=primary)

                # b) StockLedger kayıtlarını taşı (append-only audit korunur,
                #    yalnızca product FK yeniden hedeflenir)
                _SL.objects.filter(
                    store=store, product=dup
                ).update(product=primary)

                # c) StockSnapshot birleştir
                p_snap = StockSnapshot.objects.filter(
                    store=store, product=primary
                ).first()
                d_snap = StockSnapshot.objects.filter(
                    store=store, product=dup
                ).first()
                if d_snap is not None:
                    d_gram = Decimal(str(d_snap.stock_gram or 0))
                    d_pcs = int(d_snap.stock_pieces or 0)
                    d_wac = Decimal(str(d_snap.weighted_avg_cost_hs or 0))
                    if p_snap is None:
                        StockSnapshot.objects.create(
                            store=store, product=primary,
                            stock_gram=d_gram, stock_pieces=d_pcs,
                            weighted_avg_cost_hs=d_wac,
                            weighted_avg_cost_tl=Decimal(str(d_snap.weighted_avg_cost_tl or 0)),
                        )
                    else:
                        p_gram = Decimal(str(p_snap.stock_gram or 0))
                        p_pcs = int(p_snap.stock_pieces or 0)
                        p_wac = Decimal(str(p_snap.weighted_avg_cost_hs or 0))
                        total_gram = p_gram + d_gram
                        if total_gram > 0:
                            new_wac = (
                                (p_gram * p_wac) + (d_gram * d_wac)
                            ) / total_gram
                        else:
                            new_wac = p_wac
                        StockSnapshot.objects.filter(
                            id=p_snap.id
                        ).update(
                            stock_gram=total_gram,
                            stock_pieces=p_pcs + d_pcs,
                            weighted_avg_cost_hs=new_wac,
                        )
                    d_snap.delete()

                # d) Scraps satırı: primary'de yoksa taşı, varsa duplicate'i
                #    soft-delete et
                p_scrap = Scraps.objects.filter(
                    store=store, product=primary
                ).first()
                if p_scrap is None:
                    Scraps.objects.filter(
                        store=store, product=dup
                    ).update(product=primary)
                else:
                    Scraps.objects.filter(
                        store=store, product=dup
                    ).update(is_deleted=True, is_active=False)

                # e) Duplicate Products soft-delete (atomic UPDATE — full_clean bypass)
                Products.objects.filter(id=dup.id).update(
                    is_deleted=True, is_active=False,
                )
                merged_pools += 1

            # PRIMARY'nin meta alanlarını StockLedger üzerinden tutarlı hale getir
            try:
                recalculate_scrap_pool_mileage_after_cancel(primary, store)
            except Exception as _recalc_err:
                logger.error(
                    "merge_scrap_pool_duplicates: recalc başarısız "
                    "(primary=%s, label=%s): %s",
                    primary.id, karat_label, _recalc_err,
                )

        details.append({
            'karat_label': karat_label,
            'primary_id': str(primary.id),
            'duplicates': [str(d.id) for d in duplicates],
        })

    return {
        'merged_groups': merged_groups,
        'merged_pools': merged_pools,
        'details': details,
    }


@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('SCRAPS_SCRAP_ADD')
@transaction.atomic
def merge_scrap_duplicates_view(request):
    """
    UAT temizlik endpoint'i: aynı ayar sınıfında oluşmuş duplicate hurda
    havuzlarını tek primary altında birleştirir.

    POST parametreleri:
        material_type: 'GOLD' (default) | 'SILVER'

    Body:
        {'result': True, 'merged_groups': N, 'merged_pools': N, 'details': [...]}
    """
    store = request.user.store
    mat = (request.POST.get('material_type') or 'GOLD').upper()
    if mat not in ('GOLD', 'SILVER'):
        mat = 'GOLD'

    try:
        result = merge_scrap_pool_duplicates(store=store, category=None, material_type=mat)
    except Exception as e:
        logger.exception("merge_scrap_duplicates_view: hata")
        return JsonResponse({'result': False, 'error': str(e)}, status=500)

    return JsonResponse({
        'result': True,
        'merged_groups': result['merged_groups'],
        'merged_pools': result['merged_pools'],
        'details': result['details'],
    })


def _resolve_view_material_type(request, default='GOLD'):
    """
    ONARIM FAZI 4 / ADIM 4 — Sayfa bağlamından material_type türetir.

    URL view bağlamında (silver_index → 'SILVER') sabittir. Bu fonksiyon
    POST/GET'ten gelen material_type değerini görmezden gelir; yalnızca
    sayfanın ait olduğu sabit material_type'ı döner. Böylece kullanıcı
    yanlışlıkla altın sayfasında SILVER POST gönderse bile veri izolasyonu
    kırılmaz.
    """
    mt = (default or 'GOLD').upper()
    if mt not in ('GOLD', 'SILVER'):
        mt = 'GOLD'
    return mt


# ----------------- sayfa -----------------

@login_required(login_url='login')
@role_required('SCRAPS_SCRAP_INDEX')
def scrap_index(request):
    """Altın hurda yönetim sayfası (default)."""
    return render(request, 'management/scraps/index.html', {
        'title': 'Hurda Yönetimi',
        'view_material_type': 'GOLD',
        'page_heading': 'Hurda',
    })


@login_required(login_url='login')
@role_required('SCRAPS_SCRAP_INDEX')
def silver_index(request):
    """
    ONARIM FAZI 4 / ADIM 4 — İzole gümüş yönetim sayfası.

    Aynı template'i kullanır ancak `view_material_type='SILVER'` gönderir;
    böylece tüm POST'lar (scrap_add, get_all, vs.) gümüşe sabitlenir.
    Altın havuzları bu sayfada görünmez/oluşmaz.
    """
    return render(request, 'management/scraps/index.html', {
        'title': 'Gümüş Yönetimi',
        'view_material_type': 'SILVER',
        'page_heading': 'Gümüş',
    })


# ----------------- ekle/güncelle -----------------

@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('SCRAPS_SCRAP_ADD')
@transaction.atomic
def scrap_add(request):
    """
    - gram: 3 ondalık
    - product_mileage (milyem): tam sayı
    - Aynı mağazada ve aynı ayar sınıfında hurda varsa AYNI havuza eklenir.

    ONARIM FAZI 4 / ADIM 1 (BUG A FIX):
        Tedarikçili VE tedarikçisiz tüm girişler için artık benzersiz bir
        sp_process_no üretilir. StockLedger.ref_id = sp_process_no olur,
        SupplierLedger.process_no = sp_process_no, Process.process_no = sp_process_no.
        Üç tablo aynı referansla bağlanır → cancel_stock_entry tek çağrıyla
        STOK + CARİ + Process'i atomik geri alabilir.

        Önceki davranış (HATALI): ref_id=f"scrap_{product.id}" — havuza eklenen
        HER giriş aynı ref_id'yi paylaşıyordu, "İşlemler > İptal" sayfasında
        bir girişin iptali tüm havuzu hedef alıyordu.
    """
    store = request.user.store

    try:
        category = Categories.objects.get(name__icontains='Hurda')
    except Categories.DoesNotExist:
        return JsonResponse({'result': False, 'error': 'Hurda kategorisi bulunamadı!'}, status=400)

    scrap_id = request.POST.get('scrap_id')

    gram = parse_decimal_locale(request.POST.get('gram'), default="0", places=3)
    gram = d_quantize(gram, 3)

    raw_mileage = parse_decimal_locale(request.POST.get('product_mileage'), default="0")
    product_mileage = Decimal(int(raw_mileage))

    raw_scrap_name = request.POST.get('scrap_name') or ''
    # ONARIM FAZI 9 — Havuz anahtarı kullanıcının seçtiği ayar etiketidir.
    # Canonical etiket (ör. "14 Ayar") çıkarılamazsa kullanıcının raw input'una
    # düşülür; o da yoksa milyem-türevli "X Milyem Hurda" fallback'i kullanılır.
    canonical_karat_label = extract_scrap_karat_label(
        scrap_name=raw_scrap_name,
        fallback_mileage=product_mileage,
        material_type='GOLD',
    )
    name = (
        canonical_karat_label
        or (raw_scrap_name.strip() if raw_scrap_name else '')
        or f"{int(product_mileage)} Milyem Hurda"
    )

    # Yeni giren hurdanın has maliyeti
    buy_price_hs = d_quantize(product_mileage / Decimal('1000'), 3)
    sale_price_hs = buy_price_hs

    # ------------------------------------------------------------------
    # ONARIM FAZI 4 / ADIM 4 — MATERIAL_TYPE: SAYFA BAĞLAMI ZORUNLU
    # ------------------------------------------------------------------
    # Frontend formundan gelen `material_type` (GOLD/SILVER) doğrudan
    # kabul edilir. Sayfa bağlamı (view) altın veya gümüş sayfasıdır;
    # template hidden input ile sabit gönderir, kullanıcı manipüle edemez.
    # Geçersiz değer → fallback GOLD.
    # ------------------------------------------------------------------
    scrap_material_type = (request.POST.get('material_type') or 'GOLD').upper()
    if scrap_material_type not in ('GOLD', 'SILVER'):
        scrap_material_type = 'GOLD'

    if gram <= 0 or product_mileage <= 0:
        return JsonResponse({'result': False, 'error': 'Geçersiz değerler girildi!'}, status=400)

    # -----------------------------------------
    # 1. GÜNCELLEME (Mevcut bir kaydı UI'dan düzenleme)
    # -----------------------------------------
    if scrap_id:
        try:
            scrap = Scraps.objects.select_related('product').get(id=scrap_id, store=store)
        except Scraps.DoesNotExist:
            return JsonResponse({'result': False, 'error': 'Kayıt bulunamadı!'}, status=404)

        p = scrap.product

        # ------------------------------------------------------------------
        # ONARIM FAZI 6 / BUG 3 — UPDATE YOLU GÜVENLIK BARIYERLERI
        # ------------------------------------------------------------------
        # Aynı havuza birden fazla kaynaktan giriş yapılmış olabilir
        # (WAC milyem). Tek bir kaydı UI'dan düzenleyince milyem/gram'ı
        # ezmek havuzun ağırlıklı ortalamasını ve diğer girişlerin
        # bütünlüğünü bozar. Aynı şekilde satış geçmişi varsa fiyat
        # kalemlerini güncellemek tarihsel kar/zarar raporunu çarpıtır.
        # ------------------------------------------------------------------
        active_entry_count = Process.objects.filter(
            store=store,
            product=p,
            transaction_type='PURCHASE',
            is_status='COMPLETED',
            is_deleted=False,
        ).count()
        ever_sold_for_pool = Process.objects.filter(
            store=store,
            product=p,
            transaction_type='SALE',
            is_status='COMPLETED',
        ).exists()

        if active_entry_count > 1 or ever_sold_for_pool:
            return JsonResponse({
                'result': False,
                'error': (
                    'Bu havuz birden fazla giriş kaydından oluşuyor veya satış '
                    'geçmişine sahip; doğrudan düzenlenemez. Lütfen ilgili '
                    'işlemi "İşlemler > İptal" üzerinden iptal edip yeni bir '
                    'giriş yapın.'
                ),
            }, status=409)

        # Tek kaynaklı, satılmamış havuz → güvenli güncelleme.
        # ONARIM FAZI 5 deseni: Products.save() yerine atomic UPDATE ile
        # full_clean() bariyerini bypass et (legacy gram alanı tetiklemesin).
        Products.objects.filter(id=p.id).update(
            name=name,
            product_mileage=product_mileage,
            buy_price_hs=buy_price_hs,
            sale_price_hs=sale_price_hs,
        )

        # FAZ 3: StockService.adjustment ile stok düzeltme
        StockService.adjustment(
            product=p,
            store=store,
            actual_gram=gram,
            actual_pieces=0,
            ref_id=f"scrap_update_{scrap_id}",
            user=request.user,
            notes=f"Hurda güncelleme: {name}",
        )
        return JsonResponse({'result': True})

    # -----------------------------------------
    # 2. YENİ KAYIT veya VAR OLAN HAVUZA EKLEME
    # -----------------------------------------
    # HAVUZLAMA STRATEJİSİ (ONARIM FAZI 9):
    # Aynı ayar sınıfındaki (14/18/22/24) tüm hurdalar TEK Products
    # kaydı altında birleştirilir. Havuz anahtarı KULLANICININ SEÇTİĞİ
    # ayar adıdır (`scrap_name`), milyem'den TÜRETİLMEZ. Kullanıcı
    # "14 Ayar" seçip 595/605/995 milyem girse de 14 Ayar havuzunda
    # toplanır; milyem AĞIRLIKLI ORTALAMA ile pool seviyesinde güncellenir.
    existing_product = find_scrap_pool_by_selected_karat(
        store=store, category=category,
        scrap_name=raw_scrap_name,
        fallback_mileage=product_mileage,
        is_scrap=True,
        material_type=scrap_material_type,
    )

    # ------------------------------------------------------------------
    # ONARIM FAZI 3 / ADIM 2 — LEDGER CURRENCY (HS / HG)
    # ------------------------------------------------------------------
    # Gumus hurdada SupplierLedger'a HG (Has Gumus) birimiyle yazmaliyiz.
    # ------------------------------------------------------------------
    def _resolve_ledger_currency(_product):
        try:
            from apps.suppliers.services import get_ledger_currency
            return get_ledger_currency(_product, fiat_currency='TRY')
        except Exception:
            try:
                mt = getattr(_product, 'material_type', 'GOLD') or 'GOLD'
                return 'HG' if mt == 'SILVER' else 'HS'
            except Exception:
                return 'HS'

    # FAZ 3: Has Altın kurunu Products tablosundan güvenli şekilde al
    _hs_rate_tl = Decimal('0')
    try:
        hs_product = Products.objects.filter(name__icontains='Has Altın').first()
        if hs_product and hs_product.buy_price_tl:
            _hs_rate_tl = Decimal(str(hs_product.buy_price_tl))
    except Exception:
        pass

    # TL maliyet hesapla
    unit_cost_tl = Decimal('0.00')
    if _hs_rate_tl > 0 and buy_price_hs > 0:
        unit_cost_tl = (buy_price_hs * _hs_rate_tl).quantize(Decimal('0.01'))

    # ------------------------------------------------------------------
    # ONARIM FAZI 4 / ADIM 1 — BENZERSIZ sp_process_no (TEDARIKCISIZ DAHIL)
    # ------------------------------------------------------------------
    # Önceki davranış: tedarikçisiz girişlerde process_no üretilmiyor,
    # ref_id=f"scrap_{product.id}" → havuza eklenen tüm girişler aynı
    # ref_id'yi paylaşıyor → cancel zincirleme tüm havuzu hedefliyor.
    #
    # Yeni davranış: HER giriş için generate_process_no() üretilir, hem
    # tedarikçili hem tedarikçisiz Process kaydı oluşturulur (tedarikçisizde
    # supplier=None). Böylece her giriş bağımsız iptal edilebilir.
    # ------------------------------------------------------------------
    supplier_id = (request.POST.get('supplier_id') or '').strip()
    sp_process_no = generate_process_no()
    _stock_reason = StockLedger.Reason.PURCHASE if supplier_id else StockLedger.Reason.INITIAL
    _stock_ref_type = 'scrap_add'  # tek tip — tedarikçisiz/tedarikçili ayrımı reason ile yapılır

    if existing_product:
        # HAVUZ ZATEN VAR: Ağırlıklı ortalama hesapla ve stoğu artır
        scrap_record, s_created = Scraps.objects.get_or_create(
            product=existing_product,
            store=store,
            defaults={'created_by': request.user}
        )

        # ------------------------------------------------------------------
        # ONARIM FAZI 6 / BUG 6 — REVIVAL RESET (Tertemiz Yeniden Açılış)
        # ------------------------------------------------------------------
        # Önceki silme işlemi (delete view veya "İşlemler > İptal") çeşitli
        # nedenlerle stok kalıntısı bırakabilir:
        #   - cancel_stock_entry() exception fırlatıp logger.error'a düşmüş
        #     olabilir (FAZ 5 / ADIM E sonrası sessiz değil ama yine de
        #     stok geride kalır)
        #   - Process'siz legacy havuzlarda RETURN_OUT yetersiz stok hatasıyla
        #     atlanmış olabilir (delete view satır ~1060: except pass)
        #   - Soft-delete yapılmış ama stok hareketi hiç tetiklenmemiş olabilir
        # Sonuç: Kullanıcı "tüm havuzu sildim" zannederken StockSnapshot.
        # stock_gram > 0 kalıyor → bir sonraki giriş eski stoğun üzerine
        # eklendiği için WAC milyemi yanlış hesaplanıyor (örn: 13g x 590 +
        # 10g x 585 → 23g, 587 milyem).
        #
        # Çözüm: Havuz "logically deleted" durumdaysa (Scraps.is_deleted=True
        # VEYA Scraps.is_active=False VEYA Products.is_active=False), yeni
        # giriş tamamen taze sayılır:
        #   1. StockSnapshot.stock_gram > 0 ise StockService.adjustment() ile
        #      0'a çekilir → ADJUSTMENT_MINUS audit satırı oluşur
        #   2. Products.gram (legacy) atomic update ile 0'a çekilir
        #   3. Products.product_mileage 0'a çekilir → WAC hesabı else dalına
        #      düşüp result_mileage = new_mileage olur
        # Audit trail tam korunur (StockLedger append-only, ADJUSTMENT_MINUS
        # satırı görünür); sadece "kullanıcı niyeti" semantiği netleştirilir.
        # ------------------------------------------------------------------
        was_revival = (
            scrap_record.is_deleted is True
            or scrap_record.is_active is False
            or existing_product.is_active is False
        )

        # ------------------------------------------------------------------
        # ONARIM FAZI 6 / BUG 1 — PASIF HAVUZA YENIDEN GIRIS RESET (BAYRAKLAR)
        # ------------------------------------------------------------------
        _scrap_reset_fields = []
        if scrap_record.is_deleted:
            scrap_record.is_deleted = False
            _scrap_reset_fields.append('is_deleted')
        if scrap_record.is_active is False:
            scrap_record.is_active = True
            _scrap_reset_fields.append('is_active')
        if _scrap_reset_fields:
            scrap_record.save(update_fields=_scrap_reset_fields)

        if existing_product.is_active is False:
            Products.objects.filter(id=existing_product.id).update(is_active=True)
            existing_product.is_active = True

        # ------------------------------------------------------------------
        # ONARIM FAZI 6 / BUG 6 — REVIVAL: Stok kalıntısını sıfırla
        # ------------------------------------------------------------------
        if was_revival:
            try:
                _stale_snap = (
                    StockSnapshot.objects
                    .select_for_update()
                    .filter(product=existing_product, store=store)
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
                        product=existing_product,
                        store=store,
                        actual_gram=Decimal('0'),
                        actual_pieces=0,
                        ref_id=f"scrap_revival_{existing_product.id}_{sp_process_no}",
                        user=request.user,
                        notes=(
                            "Hurda havuzu yeniden açılışı: önceki silme/iptal "
                            "sonrası kalan stok temizlendi"
                        ),
                    )
            except Exception as _revival_err:
                logger.error(
                    "scrap_revival_reset: snapshot sıfırlama başarısız "
                    "(product_id=%s, sp_process_no=%s): %s",
                    existing_product.id, sp_process_no, _revival_err,
                )
            # Legacy Products.gram + product_mileage'i de sıfırla:
            # update_scrap_pool_weighted_mileage current_gram=0 ve
            # current_mileage=0 görünce ELSE dalına düşüp result_mileage =
            # new_mileage atayacak (yeni hurda tek belirleyici).
            Products.objects.filter(id=existing_product.id).update(
                gram=Decimal('0'),
                product_mileage=Decimal('0'),
            )
            existing_product.gram = Decimal('0')
            existing_product.product_mileage = Decimal('0')

        # ── AĞIRLIKLI ORTALAMA MİLYEM GÜNCELLEMESİ (Products meta) ─────
        new_pool_mileage, _new_pool_hs = update_scrap_pool_weighted_mileage(
            product=existing_product, store=store,
            new_gram=gram, new_mileage=product_mileage,
        )
        log_pool_mileage = int(new_pool_mileage) if new_pool_mileage else int(product_mileage)

        # FAZ 3: StockService ile stok girişi (Snapshot WAC otomatik hesaplanır)
        # ONARIM FAZI 4 / ADIM 1: ref_id=sp_process_no (HER GIRIS BENZERSIZ)
        StockService.record_entry(
            product=existing_product,
            store=store,
            quantity_gram=gram,
            quantity_pieces=0,
            reason=_stock_reason,
            ref_type=_stock_ref_type,
            ref_id=sp_process_no,
            unit_cost_hs=buy_price_hs,
            unit_cost_tl=unit_cost_tl,
            hs_rate_tl=_hs_rate_tl,
            user=request.user,
            notes=f"Hurda stok girişi: {int(product_mileage)} milyem → havuz milyemi {log_pool_mileage}",
        )

        # Products.gram birikimi (snapshot'tan bağımsız legacy alan)
        # ONARIM FAZI 4 / ADIM 5: Products.save() otomatik full_clean() çağırır
        # ve clean() içinde `if self.gram < Decimal('0')` kontrolü vardır.
        # F('gram') + gram bir CombinedExpression döner; Python'da Decimal ile
        # karşılaştırılamaz → TypeError. Atomic SQL UPDATE ile bypass edilir.
        Products.objects.filter(id=existing_product.id).update(gram=F('gram') + gram)

        target_product = existing_product

    else:
        # HAVUZ YOK: İlk defa ekleniyor
        # ONARIM FAZI 3 / ADIM 2: Yeni havuz olusturulurken material_type
        # set edilir — ileride find_scrap_pool_by_karat() dogru havuzu bulur.
        p = Products.objects.create(
            store=store,
            category=category,
            name=name,
            gram=gram,
            product_mileage=product_mileage,
            buy_price_hs=buy_price_hs,
            sale_price_hs=sale_price_hs,
            is_scrap=True,
            material_type=scrap_material_type,
        )
        Scraps.objects.create(store=store, product=p, created_by=request.user)

        # FAZ 3: StockService ile ilk stok girişi
        # ONARIM FAZI 4 / ADIM 1: ref_id=sp_process_no
        StockService.record_entry(
            product=p,
            store=store,
            quantity_gram=gram,
            quantity_pieces=0,
            reason=_stock_reason,
            ref_type=_stock_ref_type,
            ref_id=sp_process_no,
            unit_cost_hs=buy_price_hs,
            unit_cost_tl=unit_cost_tl,
            hs_rate_tl=_hs_rate_tl,
            user=request.user,
            notes=f"Yeni hurda havuzu: {name}",
        )

        target_product = p

    # ------------------------------------------------------------------
    # CARİ + PROCESS KAYDI (TEDARIKCILI VE TEDARIKCISIZ)
    # ------------------------------------------------------------------
    # Tedarikçili: SupplierLedger ENTRY + Process(supplier=...) oluşur.
    # Tedarikçisiz: Sadece Process(supplier=None) oluşur — cari yazılmaz
    #               ama "İşlemler" listesinde görünür ve cancel_stock_entry
    #               ile tek başına iptal edilebilir.
    # ------------------------------------------------------------------
    total_has_value = d_quantize(buy_price_hs * gram, 3)

    if supplier_id:
        try:
            supplier = Suppliers.objects.get(id=supplier_id, store=store)
            if total_has_value > 0:
                _ledger_currency = _resolve_ledger_currency(target_product)

                SupplierLedger.objects.create(
                    supplier=supplier,
                    product=target_product,
                    transaction_type=SupplierLedger.ENTRY,
                    quantity_piece=0,
                    quantity_gram=gram,
                    amount_value=total_has_value,
                    currency=_ledger_currency,
                    process_no=sp_process_no,
                    description=f"Hurda alımı: {int(product_mileage)} milyem",
                    is_active=True,
                )

                Process.objects.create(
                    store=store,
                    process_no=sp_process_no,
                    process_type='WHOLESALE',
                    transaction_type='PURCHASE',
                    product=target_product,
                    supplier=supplier,
                    employee=request.user,
                    piece=0,
                    gram=gram,
                    price_hs=total_has_value,
                    unit_price=unit_cost_tl,
                    amount=(unit_cost_tl * gram).quantize(Decimal('0.01')),
                    is_status='COMPLETED',
                    is_deleted=False,
                )
        except Suppliers.DoesNotExist:
            # Tedarikçi bulunamadıysa tedarikçisiz akışa düş
            Process.objects.create(
                store=store,
                process_no=sp_process_no,
                process_type='WHOLESALE',
                transaction_type='PURCHASE',
                product=target_product,
                supplier=None,
                employee=request.user,
                piece=0,
                gram=gram,
                price_hs=total_has_value,
                unit_price=unit_cost_tl,
                amount=(unit_cost_tl * gram).quantize(Decimal('0.01')),
                is_status='COMPLETED',
                is_deleted=False,
            )
    else:
        # ONARIM FAZI 4 / ADIM 1 — TEDARIKCISIZ AKIS
        # Process kaydı supplier=None olarak oluşur. SupplierLedger yazılmaz.
        Process.objects.create(
            store=store,
            process_no=sp_process_no,
            process_type='WHOLESALE',
            transaction_type='PURCHASE',
            product=target_product,
            supplier=None,
            employee=request.user,
            piece=0,
            gram=gram,
            price_hs=total_has_value,
            unit_price=unit_cost_tl,
            amount=(unit_cost_tl * gram).quantize(Decimal('0.01')),
            is_status='COMPLETED',
            is_deleted=False,
        )

    return JsonResponse({'result': True})


# ----------------- listeleme (DataTables server-side) -----------------

@login_required(login_url='login')
def get_all(request):
    """
    Hurda havuzlarını DataTables formatında listeler.

    ONARIM FAZI 4 / ADIM 4 — material_type filtresi:
        ?material_type=SILVER → sadece gümüş havuzları
        ?material_type=GOLD   → sadece altın havuzları (default)
        Geçersiz veya boş → GOLD davranışı.
    """
    try:
        draw = int(request.GET.get('draw', 1))
        length = int(request.GET.get('length', 10))
        start = int(request.GET.get('start', 0))
        search_value = (request.GET.get('search[value]', '') or '').strip()
        order_column_index = request.GET.get('order[0][column]', '0')
        order_dir = request.GET.get('order[0][dir]', 'asc')
        store_id = request.user.store_id
        gold_rate_param = (request.GET.get('gold_rate') or '').strip()

        # ONARIM FAZI 4 / ADIM 4 — material_type filtresi
        view_material_type = (request.GET.get('material_type') or 'GOLD').upper()
        if view_material_type not in ('GOLD', 'SILVER'):
            view_material_type = 'GOLD'

        qs = (Scraps.objects
              .filter(is_deleted=False, product__is_scrap=True, store_id=store_id,
                      product__material_type=view_material_type)
              .select_related('product'))

        total_records = qs.count()

        if gold_rate_param:
            val = int(Decimal(str(gold_rate_param)))
            if val > 0:
                qs = qs.filter(product__product_mileage=val)

        if search_value:
            qs = qs.filter(
                Q(product__name__icontains=search_value) |
                Q(product__gram__icontains=search_value) |
                Q(product__product_mileage__icontains=search_value)
            )

        # ---- EVER SOLD (iade olsa bile) ----
        # UAT-1A — `is_deleted=False` filtresi: iptal edilmiş satışlar artık
        # `ever_sold=True` üretmiyor. Aksi halde "stok 0 + iptal edilmiş satış"
        # senaryosunda ghost filter kaydı gizleyemiyordu (Bilezik UAT-1A eşleniği).
        ever_sold_q = Process.objects.filter(
            store_id=store_id,
            product_id=OuterRef('product_id'),
            transaction_type='SALE',
            is_status='COMPLETED',
            is_deleted=False,
        )

        # Son tamamlanmış satışın process_no'su (Detay linki için)
        last_sale_sq = (
            Process.objects
            .filter(store_id=store_id,
                    product_id=OuterRef('product_id'),
                    transaction_type='SALE',
                    is_status='COMPLETED',
                    is_deleted=False)
            .order_by('-date', '-id')
            .values('process_no')[:1]
        )

        # UAT-1B — Aktif PURCHASE Process sayısı: çoklu kaynak (havuz) sinyali.
        # Aynı tedarikçiden 2+ ayrı giriş gelse de yakalanır; frontend kalem
        # ikonunu bu sinyalle gizler ve havuz detay modalına yönlendirir.
        active_purchase_count_sq = (
            Process.objects
            .filter(
                store_id=store_id,
                product_id=OuterRef('product_id'),
                transaction_type='PURCHASE',
                is_status='COMPLETED',
                is_deleted=False,
            )
            .values('product_id')
            .annotate(c=Count('id'))
            .values('c')[:1]
        )

        # FAZ 6: StockSnapshot'tan stok gram okuma (Products.gram yerine)
        snap_sq = StockSnapshot.objects.filter(
            product_id=OuterRef('product_id'),
            store_id=store_id
        )
        inv_weight_sq = snap_sq.values('stock_gram')[:1]

        # ------------------------------------------------------------------
        # ONARIM FAZI 7 / ADIM 4 — IN_PROGRESS MUAFİYETİ (Toptan-Hurda Köprüsü)
        # ------------------------------------------------------------------
        # Toptan modülünde `add_scrap_to_wholesale_process` IN_PROGRESS Process
        # kaydı oluşturur ama stok hareketi `complete_process_wholesale`'a
        # ertelenir. Bu pencere içinde StockSnapshot.stock_gram=0 + ever_sold=
        # False olur ve ghost filtresi havuzu listeden gizler → kullanıcı
        # eklediği hurdayı kaybolmuş zannediyor (Bulgu 2). Aktif IN_PROGRESS
        # Process'i bulunan havuzlar ghost filtresinden muaf tutulur.
        # ------------------------------------------------------------------
        in_progress_q = Process.objects.filter(
            store_id=store_id,
            product_id=OuterRef('product_id'),
            transaction_type='PURCHASE',
            is_status='IN_PROGRESS',
            is_deleted=False,
        )

        qs = qs.annotate(
            ever_sold=Exists(ever_sold_q),
            has_in_progress=Exists(in_progress_q),
            last_sale_process_no=Subquery(last_sale_sq, output_field=CharField()),
            inv_stock_gram=Subquery(inv_weight_sq, output_field=DecimalField()),
            active_purchase_count=Coalesce(
                Subquery(active_purchase_count_sq, output_field=IntegerField()),
                0,
            ),
        )

        # ------------------------------------------------------------------
        # ONARIM FAZI 6 / BUG 4 — HAYALET KAYIT (GHOST SCRAP) GIZLEME
        # ------------------------------------------------------------------
        # "İşlemler > İptal" akışı `Scraps.is_deleted=True` yapmadığı için
        # tamamen iptal edilmiş havuzlarda Scraps satırı listede kalıyor
        # (stok=0, milyem=0). Bu kayıtlar için üç durum vardır:
        #   - Hiç satış olmamış + stok=0 + IN_PROGRESS yok → HAYALET → GIZLE
        #   - Satış geçmişi var (ever_sold=True) → tarihsel bilgi → GOSTER
        #   - IN_PROGRESS toptan kaydı var (FAZ 7 / ADIM 4) → GOSTER
        # Aynı havuza yeni hurda eklendiğinde stock_gram > 0 olur ve
        # otomatik tekrar görünür hale gelir (BUG 1 reset ile birlikte).
        # ------------------------------------------------------------------
        qs = qs.exclude(
            Q(ever_sold=False)
            & Q(has_in_progress=False)
            & (Q(inv_stock_gram__lte=Decimal('0')) | Q(inv_stock_gram__isnull=True))
        )

        filtered_records = qs.count()

        columns_map = {
            '1': 'product__name',
            '2': 'product__is_completed',
            '3': 'inv_stock_gram',
            '4': 'product__product_mileage',
            '5': 'product__buy_price_hs',
            '6': 'product__sale_price_hs',
            '7': 'created_on',
            '8': 'product__is_active',
        }
        order_field = columns_map.get(str(order_column_index), 'created_on')
        if order_dir == 'desc':
            order_field = '-' + order_field

        qs = qs.order_by(order_field, 'id')

        if length != -1:
            qs = qs[start:start + length]

        data = []
        for s in qs:
            p = s.product

            # Satış olduysa (iade olsa bile) Detay linki üret (son tamamlanmış satış)
            detail_url = None
            if s.ever_sold and s.last_sale_process_no:
                try:
                    detail_url = reverse('process:detail', args=[s.last_sale_process_no])
                except Exception:
                    detail_url = f"/process/detail/{s.last_sale_process_no}"

            inv_gram = s.inv_stock_gram if getattr(s, 'inv_stock_gram', None) is not None else Decimal('0')
            try:
                inv_gram_dec = Decimal(str(inv_gram))
            except Exception:
                inv_gram_dec = Decimal('0')

            # UAT-1B / UAT-2 — Çoklu kaynak (havuz) sinyali
            _active_count = int(getattr(s, 'active_purchase_count', 0) or 0)
            data.append({
                'id': s.id,
                'product__name': p.name,
                'product__is_completed': bool(p.is_completed),
                'product__gram': d_fmt(inv_gram_dec, 3),
                # ONARIM FAZI 6 / BUG 2A: d_fmt(value, 0) tam sayılarda
                # trailing-zero strip yaptığı için 590 → "59" üretiyordu.
                # Milyem her zaman tam sayıdır; doğrudan int() kullanılır.
                'product__product_mileage': str(int(p.product_mileage)) if p.product_mileage is not None else '0',
                'product__buy_price_hs': d_fmt(p.buy_price_hs, 3),
                'product__sale_price_hs': d_fmt(getattr(p, 'sale_price_hs', None), 3),
                'product__is_active': bool(p.is_active),
                'created_on': (s.created_on.isoformat() if s.created_on else None),
                'product__ever_sold': bool(s.ever_sold),
                'detail_url': detail_url,

                # Çoklu kaynak (havuz) — frontend bu flag'a göre kalem ikonunu
                # gizler, kullanıcıyı havuz detay modalına yönlendirir.
                'active_purchase_count': _active_count,
                'is_multi_source_pool': _active_count > 1,
            })

        return JsonResponse({
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data
        })

    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


# ----------------- havuz kaynakları analizi -----------------

@login_required(login_url='login')
@require_http_methods(["GET"])
@role_required('SCRAPS_DELETE')
def get_pool_sources(request):
    """
    Hurda havuzunun kaç farklı alış kaydından oluştuğunu döner.

    ONARIM FAZI 4 / ADIM 1: Tedarikçisiz girişler de Process kaydına sahip;
    bu liste artık her giriş için tek satır içerir (tedarikçili+tedarikçisiz).
    """
    scrap_ids = request.GET.getlist('ids[]') or []
    store = request.user.store
    if not scrap_ids:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'})

    try:
        scraps = Scraps.objects.filter(id__in=scrap_ids, store=store)
        product_ids = [s.product_id for s in scraps if s.product_id]

        if not product_ids:
            return JsonResponse({
                'result': True, 'multi_source': False, 'processes': []
            })

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

        # scrap_id -> product_id eşleşmesi
        scrap_by_product = {str(s.product_id): str(s.id) for s in scraps if s.product_id}

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
                'scrap_id': scrap_by_product.get(str(p.product_id), ''),
            })

        multi_source = len(proc_list) > 1

        return JsonResponse({
            'result': True,
            'multi_source': multi_source,
            'processes': proc_list,
        })
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


# ----------------- havuz içerik detayı (info modal için) -----------------

@login_required(login_url='login')
@require_http_methods(["GET"])
def get_pool_contents(request):
    """
    Seçilen hurda(lar)ın havuzundaki stoğun kaynak bazlı kırılımı.
    Müşteriye gösterilecek şeffaflık modalı için kullanılır.

    ONARIM FAZI 4 / ADIM 1: Tedarikçisiz girişler de Process kaydına sahip
    olduğundan artık "Açılış Stoğu" suni kaynağı gerekmez — her giriş
    Process listesinde net gösterilir. Geriye dönük (eski tedarikçisiz
    kayıtlar Process'siz oluşturulmuş olabilir) için fark hesabı korunur.
    """
    scrap_ids = request.GET.getlist('ids[]') or []
    store = request.user.store
    if not scrap_ids:
        return JsonResponse({'result': False, 'error_msg': 'Kayıt seçilmedi.'})

    try:
        row = (Scraps.objects
               .filter(id__in=scrap_ids, store=store)
               .select_related('product')
               .first())
        if not row or not row.product:
            return JsonResponse({'result': False, 'error_msg': 'Havuz bulunamadı.'})

        p = row.product

        snap = StockSnapshot.objects.filter(product=p, store=store).first()
        total_snap_gram = Decimal(str(snap.stock_gram)) if snap and snap.stock_gram else Decimal('0')

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
                'label': (s_name if s_name else 'Tedarikçisiz'),
            })

        # Geriye dönük: eski (Process'siz) tedarikçisiz açılış stoğu varsa göster
        no_supplier_gram = total_snap_gram - sum_active_proc_gram
        if no_supplier_gram > Decimal('0.0009'):
            sources.append({
                'supplier_name': None,
                'supplier_id': None,
                'gram': float(no_supplier_gram),
                'process_no': None,
                'date': None,
                'label': 'Tedarikçisiz (Açılış Stoğu - Geçmiş)',
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


# ----------------- sil -----------------

@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('SCRAPS_DELETE')
@transaction.atomic
def delete(request):
    """
    Hurda silme akışı — havuz (pool) güvenlik kontrolü dahil.

    Davranış:
      A. 'selected_process_no' parametresi varsa: SADECE o Process iptal
         edilir, havuzun kalan bölümü korunur.
      B. 'selected_process_no' yoksa:
         1. Bağlı Process kayıtları bulunur
         2. Tek kayıt → tamamı iptal (cancel_stock_entry)
         3. Birden fazla kayıt → MULTI_SOURCE_POOL hatası (frontend modal açar)
         4. Kayıt yoksa → mevcut stoğu RETURN_OUT ile düş (geriye dönük)
         5. Stok sıfırsa Products + Scraps soft-delete

    ONARIM FAZI 4 / ADIM 3 (BUG B FIX):
        Eski kod: SupplierLedger.update(is_active=False) — reversal yok,
        balance audit trail kopuyor. Yeni kod: cancel_stock_entry kullanır;
        StockLedger reversal + SupplierLedger reversal + soft-disable atomik.
    """
    from apps.stock_management.services.stock_service import InsufficientStockError

    ids = request.POST.getlist('ids[]') or []
    selected_process_no = (request.POST.get('selected_process_no') or '').strip()
    force = (request.POST.get('force') or '').lower() in ('1', 'true', 'yes')
    store = request.user.store

    try:
        _hs_rate_tl = Decimal('0')
        try:
            _hs_data = PriceService.get_price('GOLD_24K')
            _hs_rate_tl = Decimal(str(_hs_data.get('buy_tl', Decimal('0'))))
        except Exception:
            pass

        scraps = Scraps.objects.filter(id__in=ids, store=store).select_related('product')

        for s in scraps:
            p = s.product
            if not p:
                s.is_deleted = True
                s.is_active = False
                s.save(update_fields=['is_deleted', 'is_active'])
                continue

            # Bu hurda ürününe bağlı aktif alış Process kayıtları
            # R-Faz 7: IN_PROGRESS taslak satırları iptal listesinde gösterme.
            linked_procs_qs = Process.objects.filter(
                product=p, store=store,
                transaction_type='PURCHASE',
                is_deleted=False,
                is_status='COMPLETED',
            )

            linked_procs = list(linked_procs_qs)
            multi_source = len(linked_procs) > 1

            # ── MOD A: Kullanıcı belirli bir Process seçmiş ─────────────
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

                # Sadece hedef process'i geri sar (BUG B FIX)
                _cancel_single_process(
                    proc=target_proc, product=p, store=store,
                    user=request.user,
                )

                # Havuzda hâlâ başka kaynaklar varsa → Products/Scraps silinmez;
                # Snapshot gramı 0'a indiyse soft-delete yapılır.
                remaining_snap = StockSnapshot.objects.filter(
                    product=p, store=store
                ).first()
                if remaining_snap and remaining_snap.stock_gram <= Decimal('0'):
                    p.is_active = False
                    p.save(update_fields=['is_active'])
                    s.is_deleted = True
                    s.is_active = False
                    s.save(update_fields=['is_deleted', 'is_active'])

            # ── MOD B: Klasik silme akışı ──────────────────────────────
            else:
                # Güvenlik: multi-source ve force=False → reddet
                if multi_source and not force:
                    return JsonResponse({
                        'result': False,
                        'error': 'MULTI_SOURCE_POOL',
                        'error_msg': (
                            f"'{p.name}' havuzuna {len(linked_procs)} farklı alış kaydı "
                            f"eklenmiş. Doğrudan silinemez — lütfen iptal edilecek "
                            f"işlemi seçin."
                        ),
                        'product_id': str(p.id),
                        'scrap_id': str(s.id),
                        'process_count': len(linked_procs),
                    }, status=409)

                # Tek kaynak veya force=True → tüm Process kayıtlarını iptal et
                for proc in linked_procs:
                    _cancel_single_process(
                        proc=proc, product=p, store=store,
                        user=request.user,
                    )

                # Process kaydı yoksa ve stokta kalan varsa RETURN_OUT (legacy)
                if not linked_procs:
                    snap = StockSnapshot.objects.filter(
                        product=p, store=store
                    ).first()
                    if snap and snap.stock_gram > Decimal('0'):
                        try:
                            StockService.record_exit(
                                product=p, store=store,
                                quantity_gram=snap.stock_gram,
                                quantity_pieces=snap.stock_pieces,
                                reason=StockLedger.Reason.RETURN_OUT,
                                ref_type='scrap_delete',
                                ref_id=str(s.id),
                                unit_cost_hs=snap.weighted_avg_cost_hs,
                                unit_cost_tl=snap.weighted_avg_cost_tl,
                                hs_rate_tl=_hs_rate_tl,
                                user=request.user,
                                notes=f"Hurda silindi (proc yok): {p.name}",
                            )
                        except InsufficientStockError:
                            pass

                # Products + Scraps soft-delete
                p.is_active = False
                p.save(update_fields=['is_active'])
                s.is_deleted = True
                s.is_active = False
                s.save(update_fields=['is_deleted', 'is_active'])

        # Dashboard cache invalidation
        try:
            from django.core.cache import cache as _cache
            _cache.delete(f"dashboard_assets_summary:{store.id}")
        except Exception:
            pass

        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


def _cancel_single_process(*, proc, product, store, user):
    """
    Tek bir alış Process kaydını güvenli biçimde iptal eder.

    ONARIM FAZI 4 / ADIM 3 (BUG B FIX):
        Eski kod manuel iki adım yapıyordu:
          1. StockService.record_exit(reason=RETURN_OUT, ref_id=f"cancel_{...}")
          2. SupplierLedger.filter(...).update(is_active=False)  ← reversal YOK
        Yeni kod evrensel cancel_stock_entry kullanır:
          - StockLedger reversal kaydı (RETURN_OUT)
          - SupplierLedger reversal kaydı (EXIT — frozen rate ile)
          - Orijinal SupplierLedger satırları is_active=False (soft-disable)
        Hepsi tek atomic transaction içinde, audit trail tam korunur.

    BUG A (ref_id mismatch) ÇÖZÜLDÜ:
        scrap_add artık StockLedger.ref_id = sp_process_no yazıyor, bu da
        Process.process_no ile bire bir eşleşiyor → cancel_stock_entry
        StockLedger'ı doğru bulup geri sarabiliyor.
    """
    if not proc.process_no:
        # process_no yoksa cancel_stock_entry çağrılamaz; legacy davranışa düş
        proc.is_status = 'CANCELED'
        proc.is_deleted = True
        proc.save(update_fields=['is_status', 'is_deleted'])
        return

    # Tedarikçisiz Process'lerde SupplierLedger yok → reverse_supplier_ledger=False
    has_supplier = bool(proc.supplier_id)

    # ------------------------------------------------------------------
    # ONARIM FAZI 7 / ADIM 3 — REF_TYPE FALLBACK (Toptan-Hurda Köprüsü)
    # ------------------------------------------------------------------
    # Hurda modülü StockLedger'a `ref_type='scrap_add'` ile yazar; ancak
    # toptan modülü (`update_product_stock` → satır 224) `ref_type='process'`
    # ile yazar. Bu ayrılık nedeniyle toptan ekranından eklenmiş bir hurdayı
    # iptal etmeye çalıştığımızda `cancel_stock_entry(ref_type='scrap_add')`
    # 0 sonuç buluyor (sessiz fail). Stok geri sarılmıyor → muhasebe yanıyor.
    #
    # Çözüm: Önce 'scrap_add' ile dene; eğer cancelled_stock_count == 0 VE
    # deactivated_supplier_ledgers == 0 ise (yani gerçekten hiçbir kayıt
    # bulunamadı), 'process' ref_type'ı ile tekrar dene. Bu retry zinciri,
    # hurda ve toptan kaynaklı kayıtları aynı iptal akışıyla kapatır.
    #
    # ONARIM FAZI 10 NOTU: Eskiden break sinyali olarak
    # `len(supplier_ledger_reversals) > 0` kullanılıyordu. Bulgu 5
    # düzeltmesinden sonra reversal SupplierLedger satırı üretilmediği
    # için bu liste daima boş; doğru sinyal artık
    # `deactivated_supplier_ledgers > 0`.
    # ------------------------------------------------------------------
    _cancel_result = None
    _cancel_err = None
    for _attempt_ref_type in ('scrap_add', 'process'):
        try:
            _cancel_result = cancel_stock_entry(
                ref_type=_attempt_ref_type,
                ref_id=proc.process_no,
                user=user,
                reverse_supplier_ledger=has_supplier,
                notes=f"Hurda alım iptali: {product.name} (proc {proc.process_no})",
            )
        except Exception as _err:
            _cancel_err = _err
            _cancel_result = None
            # Bir sonraki ref_type ile devam et (sadece son denemenin hatası loglanır)
            continue

        # Başarılı çağrı: stok geri sarıldıysa veya SL satırı pasifleştiyse break,
        # aksi halde diğer ref_type'ı dene.
        _stock_count = _cancel_result.get('cancelled_stock_count', 0) if _cancel_result else 0
        _sl_count = _cancel_result.get('deactivated_supplier_ledgers', 0) if _cancel_result else 0
        if _stock_count > 0 or _sl_count > 0:
            _cancel_err = None
            break
        # 0 sonuç → diğer ref_type'ı dene (loop devam)

    if _cancel_err is not None:
        # ONARIM FAZI 5 / ADIM E — Sessiz başarısızlık YERİNE log düşür.
        logger.error(
            "scrap_cancel: cancel_stock_entry başarısız "
            "(proc_no=%s, product_id=%s, has_supplier=%s): %s",
            proc.process_no, product.id, has_supplier, _cancel_err,
        )
    elif _cancel_result is not None:
        _final_stock = _cancel_result.get('cancelled_stock_count', 0)
        _final_sl = _cancel_result.get('deactivated_supplier_ledgers', 0)
        if _final_stock == 0 and _final_sl == 0:
            # Hiçbir ref_type altında kayıt bulunamadı — operasyonel sinyal
            logger.warning(
                "scrap_cancel: ne 'scrap_add' ne 'process' ref_type altında "
                "StockLedger/SupplierLedger kaydı bulunamadı "
                "(proc_no=%s, product_id=%s)",
                proc.process_no, product.id,
            )

    # ONARIM FAZI 5 / ADIM B — Legacy Products.gram için ZEMIN KORUMASI.
    # Eski kod: F('gram') - gram → korumasız çıkarma; aynı Process'in birden
    # fazla iptali ya da gram uyuşmazlığı Products.gram'ı negatife düşürebilirdi.
    # Negatif gram bir sonraki hurda girişinde update_scrap_pool_weighted_mileage'in
    # full_clean() bariyerini patlatıyordu (5xx). Greatest(..., 0) ile zemin
    # korunur; alan asla negatif olamaz. StockSnapshot.stock_gram zaten DB
    # CheckConstraint ile korunuyor — bu fix legacy alanı da hizaya getirir.
    try:
        gram = Decimal(str(proc.gram or 0))
        if gram > 0:
            Products.objects.filter(id=product.id).update(
                gram=Greatest(F('gram') - gram, Decimal('0'))
            )
    except Exception as _gram_err:
        logger.error(
            "scrap_cancel: Products.gram düşürme başarısız "
            "(proc_no=%s, product_id=%s): %s",
            proc.process_no, product.id, _gram_err,
        )

    # Process → iptal
    proc.is_status = 'CANCELED'
    proc.is_deleted = True
    proc.save(update_fields=['is_status', 'is_deleted'])

    # ------------------------------------------------------------------
    # ONARIM FAZI 7 / BULGU 1 — WAC GERİ HESAPLAMA
    # ------------------------------------------------------------------
    # Stok geri sarıldı; ancak `Products.product_mileage` ve
    # `StockSnapshot.weighted_avg_cost_hs` (WAC çıkışta sabit kalır
    # ilkesi gereği) eski değeri tutuyor. Havuz kalanlarına göre
    # ağırlıklı ortalama yeniden hesaplanır ve atomic UPDATE ile yazılır.
    # Tüm girişler iptal edilmişse milyem 0'a düşer.
    # ------------------------------------------------------------------
    try:
        recalculate_scrap_pool_mileage_after_cancel(product=product, store=store)
    except Exception as _wac_err:
        logger.error(
            "scrap_cancel: recalculate_scrap_pool_mileage_after_cancel başarısız "
            "(proc_no=%s, product_id=%s): %s",
            proc.process_no, product.id, _wac_err,
        )


# ----------------- durum değiştir -----------------

@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('SCRAPS_CHANGE_STATUS')
@transaction.atomic
def change_status(request):
    ids = request.POST.getlist('ids[]') or []
    try:
        rows = Scraps.objects.filter(id__in=ids).select_related('product')
        for r in rows:
            new_state = not r.is_active
            r.is_active = new_state
            r.save(update_fields=['is_active'])
            if r.product_id:
                Products.objects.filter(id=r.product_id).update(is_active=new_state)
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)
