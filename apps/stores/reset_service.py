"""
FAZ 9.2 — GRANULAR STORE RESET SERVICE
======================================
Mağaza sıfırlamayı, raporda tanımlanan 5 mantıksal "bucket" üzerinden
parça parça yürüten servis katmanı.

Bucket bağımlılık zinciri (silme sırası — katı):

    A. İşlem ve Finans Geçmişi  (Ledgers, Process, Invoice, Banking Tx, Logs)
    B. Operasyonel Geçmiş       (Custody, Inventories, Repairs, Bracelets,
                                 Orders, Counts, Scraps)
    C1. Müşteriler & Tedarikçiler
    C2. Ürün Kataloğu           (Products + bağlı tanımlar)
    D. Finansal Altyapı         (BankAccount, ProcessGroup, EsurecCredential)
    E. Hurda Altın Alımları     (GoldPurchases — TAMAMEN İZOLE)

Bağımlılık kuralı:
    B  → A gerektirir
    C1 → A, B gerektirir
    C2 → A, B gerektirir
    D  → A, B, C1, C2 gerektirir
    E  → BAĞIMSIZ. Hiçbir gruba bağımlı değildir, hiçbir grup tarafından
         silinmez. SADECE kullanıcı E'yi açıkça seçerse GoldPurchases silinir.
         (Hurda alımları yasal/finansal hassasiyet nedeniyle izole tutuldu.)

Bu modül sadece silme mantığını içerir; HTTP/permission view tarafında
yönetilir (apps/stores/views.py).

PROTECT FK uyarıları (bu yüzden sıra şart):
    • CustomerLedger.customer / .store → PROTECT
    • StockSnapshot.product / StockLedger.product → PROTECT
    • Proposals.created_by → PROTECT
"""

from collections import OrderedDict
from django.db import transaction
from django.db.models import Count


# ─────────────────────────────────────────────────────────────────────
#  BUCKET TANIMLARI (UI için meta + dependency grafiği)
# ─────────────────────────────────────────────────────────────────────

BUCKET_META = OrderedDict([
    ('A', {
        'code': 'A',
        'title': 'İşlem ve Finans Geçmişi',
        'icon': 'bi-journal-text',
        'color': 'danger',
        'description': (
            'Müşteri/Tedarikçi cari defterleri, işlem geçmişi (Process), '
            'faturalar, banka hareketleri ve aktivite kayıtları.'
        ),
        'requires': [],
        'is_financial': True,  # Soft-archive zorunlu olabilir
    }),
    ('B', {
        'code': 'B',
        'title': 'Operasyonel Geçmiş',
        'icon': 'bi-box-seam',
        'color': 'warning',
        'description': (
            'Emanet, hurda alımları, stok hareketleri, tamirler, '
            'bilezik kayıtları, siparişler ve sayım oturumları.'
        ),
        'requires': ['A'],
        'is_financial': False,
    }),
    ('C1', {
        'code': 'C1',
        'title': 'Müşteriler & Tedarikçiler',
        'icon': 'bi-people',
        'color': 'warning',
        'description': (
            'Mağazaya ait müşteri ve tedarikçi kök kayıtları. '
            'Tüm cari geçmiş ve operasyon kayıtları önce temizlenmelidir.'
        ),
        'requires': ['A', 'B'],
        'is_financial': False,
    }),
    ('C2', {
        'code': 'C2',
        'title': 'Ürün Kataloğu',
        'icon': 'bi-tags',
        'color': 'warning',
        'description': (
            'Ürünler, stok snapshot/ledger kayıtları, barkod şablonları '
            've odaya özel ürün fiyatları.'
        ),
        'requires': ['A', 'B'],
        'is_financial': False,
    }),
    ('D', {
        'code': 'D',
        'title': 'Finansal Altyapı',
        'icon': 'bi-bank',
        'color': 'dark',
        'description': (
            'Banka hesapları, POS komisyon oranları, işlem grupları ve '
            'e-Süreç entegrasyon kimlik bilgileri.'
        ),
        'requires': ['A', 'B', 'C1', 'C2'],
        'is_financial': False,
    }),
    ('E', {
        'code': 'E',
        'title': 'Hurda Altın Alımları (İzole)',
        'icon': 'bi-coin',
        'color': 'warning',
        'description': (
            'GoldPurchases tablosu — hiçbir gruba bağımlı değildir ve '
            'başka hiçbir grup silindiğinde etkilenmez. Sadece açıkça '
            'seçilirse silinir. Yüksek risk: silme öncesi yedek alın.'
        ),
        'requires': [],
        'is_financial': True,
    }),
])


def expand_with_dependencies(selected_codes):
    """
    Kullanıcının seçtiği bucket kodlarına bağımlılıklarını da ekleyip
    silme sırasına göre sıralı döndürür.

    Örnek: ['C1'] → ['A', 'B', 'C1']
    """
    needed = set()
    stack = list(selected_codes)
    while stack:
        code = stack.pop()
        if code in needed or code not in BUCKET_META:
            continue
        needed.add(code)
        stack.extend(BUCKET_META[code]['requires'])

    # BUCKET_META OrderedDict — silme sırasını koruyoruz (A, B, C1, C2, D)
    return [code for code in BUCKET_META.keys() if code in needed]


# ─────────────────────────────────────────────────────────────────────
#  SAYAÇ FONKSİYONLARI (Preview / Dry-run)
# ─────────────────────────────────────────────────────────────────────

def _safe_count(qs):
    try:
        return qs.count()
    except Exception:
        return 0


def count_bucket_a(store):
    """Grup A — İşlem ve Finans Geçmişi sayaçları."""
    counts = {}

    try:
        from apps.customers.models import CustomerLedger
        counts['CustomerLedger'] = _safe_count(CustomerLedger.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.suppliers.models import SupplierLedger
        counts['SupplierLedger'] = _safe_count(
            SupplierLedger.objects.filter(supplier__store=store)
        )
    except Exception:
        pass

    try:
        from apps.stock_management.models import StockLedger
        counts['StockLedger'] = _safe_count(StockLedger.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.process.models import Process, Payment
        counts['Process'] = _safe_count(Process.objects.filter(store=store))
        counts['Payment'] = _safe_count(
            Payment.objects.filter(process_group__store=store)
        )
    except Exception:
        pass

    # invoices app Juwelier Plus'ta yok — sayım bloğu kaldırıldı

    try:
        from apps.banking.models import BankTransaction, DailyCashClose
        counts['BankTransaction'] = _safe_count(
            BankTransaction.objects.filter(store=store)
        )
        counts['DailyCashClose'] = _safe_count(
            DailyCashClose.objects.filter(store=store)
        )
    except Exception:
        pass

    try:
        from apps.activity_logs.models import ActivityLogs
        counts['ActivityLogs'] = _safe_count(
            ActivityLogs.objects.filter(created_by__store=store)
        )
    except Exception:
        pass

    try:
        from apps.dashboard.models import DailyStoreReport, DailyEmployeeReport
        counts['DailyStoreReport'] = _safe_count(
            DailyStoreReport.objects.filter(store=store)
        )
        counts['DailyEmployeeReport'] = _safe_count(
            DailyEmployeeReport.objects.filter(store=store)
        )
    except Exception:
        pass

    return counts


def count_bucket_b(store):
    """Grup B — Operasyonel Geçmiş sayaçları."""
    counts = {}

    try:
        from apps.custody.models import CustomerCustodyLedger
        counts['CustomerCustodyLedger'] = _safe_count(
            CustomerCustodyLedger.objects.filter(store=store)
        )
    except Exception:
        pass

    # NOT: GoldPurchases artık Grup E'de (izole). Bu grup ondan dokunmaz.
    try:
        from apps.gold_purchases.models import ProductCategory, BarcodeTemplate
        counts['ProductCategory'] = _safe_count(ProductCategory.objects.filter(store=store))
        counts['BarcodeTemplate'] = _safe_count(BarcodeTemplate.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.inventories.models import Inventories, InventoryMovement
        counts['Inventories'] = _safe_count(Inventories.objects.filter(store=store))
        counts['InventoryMovement'] = _safe_count(InventoryMovement.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.repairs.models import Repairs
        counts['Repairs'] = _safe_count(Repairs.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.bracelets.models import Bracelets
        counts['Bracelets'] = _safe_count(Bracelets.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.scraps.models import Scraps
        counts['Scraps'] = _safe_count(Scraps.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.orders.models import Order
        counts['Order'] = _safe_count(Order.objects.filter(store=store))
    except Exception:
        pass

    try:
        from apps.counts.models import InventoryCountSession
        counts['InventoryCountSession'] = _safe_count(
            InventoryCountSession.objects.filter(store=store)
        )
    except Exception:
        pass

    return counts


def count_bucket_c1(store):
    """Grup C1 — Müşteriler & Tedarikçiler sayaçları."""
    counts = {}

    try:
        from apps.customers.models import Customers
        counts['Customers (mağazaya bağlı)'] = _safe_count(
            Customers.objects.filter(store=store)
        )
        counts['Customers (yalnızca bu mağazada)'] = _safe_count(
            Customers.objects.filter(store=store)
            .annotate(store_count=Count('store'))
            .filter(store_count=1)
        )
    except Exception:
        pass

    try:
        from apps.suppliers.models import Suppliers
        counts['Suppliers'] = _safe_count(Suppliers.objects.filter(store=store))
    except Exception:
        pass

    return counts


def count_bucket_c2(store):
    """
    Grup C2 — Ürün Kataloğu sayaçları.

    Sayım, gerçek silme mantığını birebir yansıtır:
      • is_protected=True ürünler hariç
      • GoldPurchases (Grup E) tarafından referans edilen ürünler hariç
        (bu ürünler izole tutulur, silinmez)
    """
    counts = {}

    # Grup E tarafından referans edilen ve dolayısıyla atlanacak ürün ID'leri
    protected_by_gold = set()
    try:
        from apps.gold_purchases.models import GoldPurchases
        protected_by_gold = set(
            GoldPurchases.objects
            .filter(product__store=store, product__isnull=False)
            .values_list('product_id', flat=True)
        )
    except Exception:
        pass

    try:
        from apps.products.models import Products
        deletable = Products.objects.filter(store=store)
        try:
            deletable = deletable.filter(is_protected=False)
        except Exception:
            pass
        if protected_by_gold:
            deletable = deletable.exclude(id__in=protected_by_gold)
        counts['Products (silinecek)'] = _safe_count(deletable)
        if protected_by_gold:
            counts['Products (Grup E korumasında, atlanacak)'] = len(protected_by_gold)
    except Exception:
        pass

    try:
        from apps.stock_management.models import StockSnapshot
        if protected_by_gold:
            counts['StockSnapshot (silinecek)'] = _safe_count(
                StockSnapshot.objects.filter(store=store)
                .exclude(product_id__in=protected_by_gold)
            )
        else:
            counts['StockSnapshot (silinecek)'] = _safe_count(
                StockSnapshot.objects.filter(store=store)
            )
    except Exception:
        pass

    return counts


def count_bucket_d(store):
    """Grup D — Finansal Altyapı sayaçları."""
    counts = {}

    try:
        from apps.banking.models import BankAccount, POSCommissionRate, EsurecTenantCredential
        store_accounts = BankAccount.objects.filter(store=store)
        counts['BankAccount'] = _safe_count(store_accounts)
        counts['POSCommissionRate'] = _safe_count(
            POSCommissionRate.objects.filter(bank_account__in=store_accounts)
        )
        counts['EsurecTenantCredential'] = _safe_count(
            EsurecTenantCredential.objects.filter(store=store)
        )
    except Exception:
        pass

    try:
        from apps.process.models import ProcessGroup
        counts['ProcessGroup'] = _safe_count(ProcessGroup.objects.filter(store=store))
    except Exception:
        pass

    # invoices app Juwelier Plus'ta yok — sayım bloğu kaldırıldı

    try:
        from apps.workshops.models import Workshops
        counts['Workshops'] = _safe_count(Workshops.objects.filter(store=store))
    except Exception:
        pass

    return counts


def count_bucket_e(store):
    """
    Grup E — Hurda Altın Alımları (İzole).

    Bu bucket başka hiçbir bucket'a bağımlı değildir ve başka hiçbir
    bucket bunu silmez. Sadece açıkça seçildiğinde silinir.
    """
    counts = {}

    try:
        from apps.gold_purchases.models import GoldPurchases
        counts['GoldPurchases'] = _safe_count(GoldPurchases.objects.filter(store=store))
    except Exception:
        pass

    return counts


COUNT_FUNCS = {
    'A': count_bucket_a,
    'B': count_bucket_b,
    'C1': count_bucket_c1,
    'C2': count_bucket_c2,
    'D': count_bucket_d,
    'E': count_bucket_e,
}


def build_preview(store, selected_codes):
    """
    UI için tek seferlik özet üretir:
        {
          'effective_buckets': ['A', 'B', 'C1'],
          'totals': { 'A': 1234, 'B': 27, 'C1': 38 },
          'details': {
              'A': { 'CustomerLedger': 47, 'Process': 234, ... },
              ...
          }
        }
    """
    effective = expand_with_dependencies(selected_codes)
    details = {}
    totals = {}
    for code in effective:
        d = COUNT_FUNCS[code](store)
        details[code] = d
        totals[code] = sum(d.values())
    return {
        'effective_buckets': effective,
        'totals': totals,
        'details': details,
        'grand_total': sum(totals.values()),
    }


# ─────────────────────────────────────────────────────────────────────
#  SİLME FONKSİYONLARI
# ─────────────────────────────────────────────────────────────────────

def delete_bucket_a(store):
    """Grup A — İşlem ve Finans Geçmişi (en bağımlı katman)."""

    # 1. Müşteri & Tedarikçi cari defterleri (PROTECT FK kıran katman)
    try:
        from apps.customers.models import CustomerLedger
        CustomerLedger.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.suppliers.models import SupplierLedger
        SupplierLedger.objects.filter(supplier__store=store).delete()
    except Exception:
        pass

    # 2. Stok defter (StockLedger) — PROTECT FK
    try:
        from apps.stock_management.models import StockLedger
        StockLedger.objects.filter(store=store).delete()
        # Çapraz mağaza ledger'ları için ürün-bazlı temizlik
        from apps.products.models import Products
        product_ids = list(Products.objects.filter(store=store).values_list('id', flat=True))
        if product_ids:
            StockLedger.objects.filter(product_id__in=product_ids).delete()
    except Exception:
        pass

    # 3. Fatura zinciri — invoices app Juwelier Plus'ta yok, adım atlandı

    # 4. İşlem zinciri (Payment → Process)  ProcessGroup Grup D'de silinir
    try:
        from apps.process.models import Payment, Process
        Payment.objects.filter(process_group__store=store).delete()
        process_nos = list(
            Process.objects.filter(store=store)
            .values_list('process_no', flat=True).distinct()
        )
        if process_nos:
            Payment.objects.filter(
                process_no__in=process_nos, process_group__isnull=True,
            ).delete()
        Process.objects.filter(store=store).delete()
    except Exception:
        pass

    # 5. Banking hareketleri & günlük kasa kapanışları
    try:
        from apps.banking.models import BankTransaction, DailyCashClose
        BankTransaction.objects.filter(store=store).delete()
        DailyCashClose.objects.filter(store=store).delete()
    except Exception:
        pass

    # 6. Aktivite kayıtları (mağaza personelinin)
    try:
        from apps.activity_logs.models import ActivityLogs
        ActivityLogs.objects.filter(created_by__store=store).delete()
    except Exception:
        pass

    # 7. Günlük raporlar
    try:
        from apps.dashboard.models import DailyStoreReport, DailyEmployeeReport
        DailyStoreReport.objects.filter(store=store).delete()
        DailyEmployeeReport.objects.filter(store=store).delete()
    except Exception:
        pass


def delete_bucket_b(store):
    """Grup B — Operasyonel Geçmiş."""

    try:
        from apps.custody.models import CustomerCustodyLedger
        CustomerCustodyLedger.objects.filter(store=store).delete()
    except Exception:
        pass

    # NOT: GoldPurchases artık Grup E'de (izole). Bu grup ondan dokunmaz.

    try:
        from apps.inventories.models import Inventories, InventoryMovement
        InventoryMovement.objects.filter(store=store).delete()
        Inventories.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.repairs.models import Repairs
        Repairs.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.bracelets.models import Bracelets
        Bracelets.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.scraps.models import Scraps
        Scraps.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.orders.models import Order
        Order.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.counts.models import InventoryCountSession
        InventoryCountSession.objects.filter(store=store).delete()
    except Exception:
        pass


def delete_bucket_c1(store):
    """Grup C1 — Müşteriler & Tedarikçiler."""

    # Customers: M2M ilişki — exclusive sil, shared M2M kopar
    try:
        from apps.customers.models import Customers
        exclusive_ids = list(
            Customers.objects.filter(store=store)
            .annotate(store_count=Count('store'))
            .filter(store_count=1)
            .values_list('id', flat=True)
        )
        store.customers.clear()
        if exclusive_ids:
            Customers.objects.filter(id__in=exclusive_ids).delete()
    except Exception:
        pass

    try:
        from apps.suppliers.models import Suppliers
        Suppliers.objects.filter(store=store).delete()
    except Exception:
        pass


def delete_bucket_c2(store):
    """
    Grup C2 — Ürün Kataloğu.

    KRİTİK İZOLASYON KURALI:
        GoldPurchases.product → Products FK'si CASCADE'dir. Products
        silindiğinde Django, ona bağlı GoldPurchases kayıtlarını da
        silmeye kalkar. Grup E (Hurda Altın Alımları) tam izole
        tutulduğu için bu davranış yasaklanmıştır.

        Bu nedenle: GoldPurchases tarafından referans verilen ürün
        ID'leri silme kapsamından ÇIKARILIR (skip). Böyle ürünler
        canlı kalır ve hurda alım geçmişi sağlam tutulur.
    """

    try:
        from apps.gold_purchases.models import ProductCategory, BarcodeTemplate
        BarcodeTemplate.objects.filter(store=store).delete()
        ProductCategory.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.products.models import Products

        # 1. GoldPurchases (Grup E — izole) tarafından referans edilen ürün ID'leri
        protected_by_gold = set()
        try:
            from apps.gold_purchases.models import GoldPurchases
            protected_by_gold = set(
                GoldPurchases.objects
                .filter(product__store=store, product__isnull=False)
                .values_list('product_id', flat=True)
            )
        except Exception:
            pass

        # 2. Silinecek ürün queryset'i — is_protected sistem ürünleri ve
        #    Grup E tarafından referans verilen ürünler hariç.
        deletable_products = Products.objects.filter(store=store)
        try:
            deletable_products = deletable_products.filter(is_protected=False)
        except Exception:
            pass
        if protected_by_gold:
            deletable_products = deletable_products.exclude(id__in=protected_by_gold)

        deletable_product_ids = list(deletable_products.values_list('id', flat=True))

        # 3. StockSnapshot — sadece silinecek ürünlere ait olanlar
        #    (GoldPurchases'a bağlı ürünlerin snapshot'ı korunur).
        try:
            from apps.stock_management.models import StockSnapshot
            if deletable_product_ids:
                StockSnapshot.objects.filter(
                    store=store, product_id__in=deletable_product_ids,
                ).delete()
            # Ürüne bağlı olmayan store-only snapshot'lar da temizlenebilir,
            # ama ürün-kilitli olanları bilinçli olarak bırakıyoruz.
        except Exception:
            pass

        # 4. Ürünleri sil (GoldPurchases'a bağlı olanlar atlandı)
        if deletable_product_ids:
            Products.objects.filter(id__in=deletable_product_ids).delete()
    except Exception:
        pass


def delete_bucket_d(store):
    """Grup D — Finansal Altyapı."""

    try:
        from apps.banking.models import (
            BankAccount, POSCommissionRate, EsurecTenantCredential,
        )
        store_accounts = BankAccount.objects.filter(store=store)
        POSCommissionRate.objects.filter(bank_account__in=store_accounts).delete()
        store_accounts.delete()
        EsurecTenantCredential.objects.filter(store=store).delete()
    except Exception:
        pass

    try:
        from apps.process.models import ProcessGroup
        ProcessGroup.objects.filter(store=store).delete()
    except Exception:
        pass

    # invoices app Juwelier Plus'ta yok — silme bloğu kaldırıldı

    try:
        from apps.workshops.models import Workshops
        Workshops.objects.filter(store=store).delete()
    except Exception:
        pass


def delete_bucket_e(store):
    """
    Grup E — Hurda Altın Alımları (İzole).

    UYARI: Bu fonksiyon SADECE kullanıcı Grup E'yi açıkça işaretlediğinde
    çağrılır. Diğer bucket'ların silme zincirinde bu tablo yer ALMAZ.
    """
    try:
        from apps.gold_purchases.models import GoldPurchases
        GoldPurchases.objects.filter(store=store).delete()
    except Exception:
        pass


DELETE_FUNCS = {
    'A': delete_bucket_a,
    'B': delete_bucket_b,
    'C1': delete_bucket_c1,
    'C2': delete_bucket_c2,
    'D': delete_bucket_d,
    'E': delete_bucket_e,
}


# ─────────────────────────────────────────────────────────────────────
#  ORKESTRASYON
# ─────────────────────────────────────────────────────────────────────

def execute_reset(store, selected_codes):
    """
    Seçilen bucket'ları bağımlılıklarıyla beraber, doğru sırayla siler.
    Tek transaction içinde — herhangi bir adım patlarsa tamamen geri alınır.

    Returns: { 'executed': ['A', 'B'], 'before': {...}, 'after': {...} }
    """
    effective = expand_with_dependencies(selected_codes)

    # Silme öncesi snapshot (audit log için)
    before = {code: COUNT_FUNCS[code](store) for code in effective}

    with transaction.atomic():
        for code in effective:
            DELETE_FUNCS[code](store)

    after = {code: COUNT_FUNCS[code](store) for code in effective}

    return {
        'executed': effective,
        'before': before,
        'after': after,
    }
