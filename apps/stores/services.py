# FILE: apps/stores/services.py
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from apps.products.models import Products
from apps.stores.models import StorePriceCache, Stores, StoreModule
from apps.crm.packages.models import SaaSModule, PackagePermissionMatrix

def round2(v: Decimal) -> Decimal:
    return (v or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
from decimal import Decimal, ROUND_HALF_UP

def round3(v: Decimal) -> Decimal:
    return (v or Decimal('0')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

def get_store_margins(store):
    m = Decimal(getattr(store, 'price_margin_percent', 0) or 0)
    return m, m

def compute_store_has_tl(store, use_cache: bool = True):
    sale_margin, buy_margin = get_store_margins(store)
    if use_cache and (sale_margin != 0 or buy_margin != 0):
        cache = getattr(store, 'price_cache', None)
        if cache and cache.has_buy_tl and cache.has_sale_tl:
            return Decimal(cache.has_buy_tl), Decimal(cache.has_sale_tl)
    try:
        has_prod = Products.objects.get(name__iexact='Has Altın 24 Ayar')
        base_buy = Decimal(has_prod.buy_price_tl or 0)
        base_sale = Decimal(has_prod.sale_price_tl or 0)
    except Products.DoesNotExist:
        return Decimal('0'), Decimal('0')
    store_has_buy = round2(base_buy * (Decimal('1') + buy_margin / Decimal('100')))
    store_has_sale = round2(base_sale * (Decimal('1') + sale_margin / Decimal('100')))
    return store_has_buy, store_has_sale

def update_store_has_cache_for_all_stores():
    stores = Stores.objects.filter(is_deleted=False)
    for s in stores:
        buy, sale = compute_store_has_tl(s, use_cache=False)
        StorePriceCache.objects.update_or_create(
            store=s,
            defaults={'has_buy_tl': buy, 'has_sale_tl': sale}
        )


# ─────────────────────────────────────────────────────────
#  Faz 12.3: Store Efektif Yetki Havuzu Servisleri
# ─────────────────────────────────────────────────────────

def get_store_effective_permission_ids(store):
    """
    Bir mağazanın efektif (birleşik) yetki havuzunu döndürür.

    Hesaplama mantığı (ADDİTİVE UNION):
        Efektif Yetkiler = Çekirdek Modül Yetkileri
                           ∪ Paket Yetkileri (varsa)
                           ∪ Mağaza Modül Yetkileri (StoreModule)

    1. Çekirdek Modül Yetkileri:
       is_core=True olan modüllerin yetkileri her zaman dahildir.

    2. Paket Yetkileri (opsiyonel):
       Eğer mağazanın paketi varsa, PackagePermissionMatrix(available=True)
       kayıtlarının permission_id değerleri toplanır.

    3. Mağaza Modül Yetkileri:
       StoreModule üzerinden mağazaya atanmış modüller tespit edilir.
       Her modülün collect_all_permissions() sonucu (bağımlılıklar dahil)
       toplanır. Paketsiz mağazalarda bu tek yetki kaynağıdır.

    4. Üç kümenin birleşimi (union) döndürülür.

    Parametreler:
        store: apps.stores.models.Stores instance

    Returns:
        set: Permission UUID seti (efektif yetki havuzu)
    """
    if not store:
        return set()

    # ── 1. Çekirdek Modül Yetkileri (her zaman dahil) ──
    core_perm_ids = set()
    core_modules = SaaSModule.objects.filter(is_core=True, is_active=True)
    for cm in core_modules:
        core_perm_ids |= cm.collect_all_permissions()

    # ── 2. Paket Yetkileri (opsiyonel) ──
    package_perm_ids = set()
    if store.package_id:
        package_perm_ids = set(
            PackagePermissionMatrix.objects.filter(
                package_id=store.package_id,
                available=True,
            ).values_list('permission_id', flat=True)
        )

    # ── 3. Mağaza Modül Yetkileri (StoreModule) ──
    store_module_perm_ids = set()
    store_module_ids = set(
        StoreModule.objects.filter(store=store)
        .values_list('module_id', flat=True)
    )
    if store_module_ids:
        for module in SaaSModule.objects.filter(id__in=store_module_ids, is_active=True):
            store_module_perm_ids |= module.collect_all_permissions()

    # ── 4. Birleşim (ADDITIVE UNION) ──
    return core_perm_ids | package_perm_ids | store_module_perm_ids


def sync_store_modules(store, module_ids):
    """
    Bir mağazanın modül listesini günceller.

    Mevcut StoreModule kayıtlarını yeni listeyle karşılaştırır:
    - Listede olup tabloda olmayanları ekler.
    - Tabloda olup listede olmayanları siler.
    - Çekirdek modüller her zaman dahildir, StoreModule'e eklenmez.
    - Paketin zaten içerdiği modüller StoreModule'e eklenmez (gereksiz).

    Paketsiz mağazalarda: Tüm seçilen modüller (çekirdek hariç) StoreModule'e
    yazılır. Bu durumda modül seçimi ana yetki kaynağıdır.

    Parametreler:
        store:      apps.stores.models.Stores instance
        module_ids: list/set — atanacak SaaSModule UUID'leri

    Returns:
        dict: {'added': int, 'removed': int, 'skipped_in_package': int}
    """
    from apps.crm.packages.models import PackageModule

    requested_ids = set(module_ids) if module_ids else set()

    # Çekirdek modüller her zaman dahildir; StoreModule'e yazmaya gerek yok
    core_ids = set(
        SaaSModule.objects.filter(is_core=True, is_active=True)
        .values_list('id', flat=True)
    )

    # Paketin zaten içerdiği modülleri bul (bunlar da StoreModule'e eklenmez)
    package_module_ids = set()
    if store.package_id:
        package_module_ids = set(
            PackageModule.objects.filter(package_id=store.package_id)
            .values_list('module_id', flat=True)
        )

    # Çekirdek + Paketteki modülleri birleştir → StoreModule'den hariç tut
    excluded_ids = core_ids | package_module_ids

    # Hariç tutulanları çıkar
    skipped = requested_ids & excluded_ids
    effective_ids = requested_ids - excluded_ids

    # Mevcut StoreModule kayıtlarını bul
    current_ids = set(
        StoreModule.objects.filter(store=store)
        .values_list('module_id', flat=True)
    )

    to_add = effective_ids - current_ids
    to_remove = current_ids - effective_ids

    removed = 0
    if to_remove:
        removed = StoreModule.objects.filter(
            store=store, module_id__in=to_remove
        ).delete()[0]

    added = 0
    for mid in to_add:
        StoreModule.objects.create(store=store, module_id=mid)
        added += 1

    return {
        'added': added,
        'removed': removed,
        'skipped_in_package': len(skipped),
    }


# ─────────────────────────────────────────────────────────
#  Faz 12.7 (Faz D): Teklif Kabul → Otomatik Mağaza Oluşturma
# ─────────────────────────────────────────────────────────

def auto_create_store_from_proposal(proposal):
    """
    Teklif kabul edildiğinde otomatik olarak pasif (is_active=False) bir
    mağaza oluşturur ve teklifin modül kalemlerini mağazaya atar.

    İş Mantığı:
    1. Eğer proposal.company yoksa:
       - proposal.lead varsa → Lead bilgileriyle yeni Company oluşturulur
       - Hiçbiri yoksa → None döndürür (mağaza oluşturulamaz)
    2. Firmanın zaten aktif bir mağazası varsa → mevcut mağazayı döndürür
       (tekrar oluşturmaz)
    3. Store(is_active=False) oluşturulur
    4. ProposalItems'taki module FK'lar → sync_store_modules ile atanır
    5. Korumalı ürünler envantere eklenir

    Parametreler:
        proposal: apps.crm.proposals.models.Proposals instance

    Returns:
        Stores instance veya None
    """
    import random
    from apps.stores.models import Stores, Company

    # ── 1. Firma Kontrolü / Oluşturma ──
    company = proposal.company

    if not company:
        lead = proposal.lead
        if not lead:
            return None

        # Lead bilgilerinden firma oluştur
        company = Company.objects.create(
            title=lead.business_name or lead.full_name or "Yeni Firma",
            phone=lead.phone or None,
            email=lead.email or None,
            city=lead.city or None,
            district=lead.district or None,
        )
        # Proposal'a firmayı bağla
        proposal.company = company
        proposal.save(update_fields=['company'])

    # ── 2. Firma altında zaten mağaza varsa tekrar oluşturma ──
    existing_store = Stores.objects.filter(
        company=company, is_deleted=False
    ).first()
    if existing_store:
        # Mevcut mağazanın modüllerini güncelle (eksik varsa ekle)
        module_ids = list(
            proposal.items.filter(module__isnull=False)
            .values_list('module_id', flat=True)
        )
        if module_ids:
            sync_store_modules(existing_store, module_ids)
        return existing_store

    # ── 3. Benzersiz Mağaza ID Oluştur ──
    def _gen_store_id():
        while True:
            s_id = str(random.randint(10 ** 10, 10 ** 11 - 1))
            if not Stores.objects.filter(store_id=s_id).exists():
                return s_id

    # ── 4. Pasif Mağaza Oluştur ──
    store = Stores.objects.create(
        store_id=_gen_store_id(),
        company=company,
        title=company.title or "Yeni Mağaza",
        email=company.email or None,
        phone=company.phone or None,
        address=company.address or None,
        city=company.city or None,
        district=company.district or None,
        postal_code=company.postal_code or None,
        country=company.country or "Türkiye",
        is_active=False,
    )

    # ── 5. Korumalı Ürünleri Envantere Ekle ──
    try:
        from apps.inventories.models import StockSnapshot
        protected_products = Products.objects.filter(is_protected=True)
        snapshots = []
        for product in protected_products:
            if not StockSnapshot.objects.filter(product=product, store=store).exists():
                snapshots.append(
                    StockSnapshot(
                        product=product,
                        store=store,
                        stock_pieces=0,
                        stock_gram=Decimal('0.0000'),
                    )
                )
        if snapshots:
            StockSnapshot.objects.bulk_create(snapshots)
    except Exception:
        pass  # Envanter modülü yoksa veya hata varsa sessizce devam et

    # ── 6. Modülleri Ata ──
    module_ids = list(
        proposal.items.filter(module__isnull=False)
        .values_list('module_id', flat=True)
    )
    if module_ids:
        sync_store_modules(store, module_ids)

    return store


# ─────────────────────────────────────────────────────────────────────────────
#  FAZ 19 — Hızlı Onboarding (Fast-Track) Servisleri
#
#  Üç ana servis:
#    1) create_demo_store()        → Gölge sipariş + DEMO mağaza üretir
#    2) convert_demo_to_active()   → DEMO/PENDING_PAYMENT → ACTIVE dönüşümü
#    3) expire_demo_stores()       → Süresi dolan demoları EXPIRED'e çeker
#
#  SSOT: Tüm akışlar Stores.objects.create() üzerinden ilerler; bu sayede
#        post_save sinyalleri (StoreConfiguration + 3 varsayılan kasa) otomatik
#        tetiklenir. Servisler idempotent ve transaction.atomic() korumalıdır.
# ─────────────────────────────────────────────────────────────────────────────

DEMO_DEFAULT_DURATION_DAYS = 14
DEMO_PACKAGE_FALLBACK_CODE = 'demo'
DEMO_MODULE_SLUG = 'demo-access'


def _get_or_resolve_demo_package():
    """
    Demo paketini bulur. Sıralama:
      1) is_demo=True olan ilk aktif paket
      2) code='demo' olan paket (geriye dönük güvence)
      3) None — admin paneli üzerinden manuel oluşturulmalı
    """
    from apps.crm.packages.models import Packages
    pkg = Packages.objects.filter(is_demo=True, is_active=True).order_by('order', 'name').first()
    if pkg:
        return pkg
    return Packages.objects.filter(code=DEMO_PACKAGE_FALLBACK_CODE, is_active=True).first()


def _get_demo_module():
    """
    DEMO erişim modülünü döndürür (slug='demo-access'). Yoksa None.
    Bu modülün permissions'ı, DEMO mağazalarda dekoratörün çekeceği
    efektif yetki kümesidir.
    """
    from apps.crm.packages.models import SaaSModule
    return SaaSModule.objects.filter(slug=DEMO_MODULE_SLUG, is_active=True).first()


def create_demo_store(
    *,
    first_name: str,
    last_name: str,
    phone: str,
    email: str = '',
    business_name: str,
    city: str = '',
    module_ids=None,
    created_by=None,
    duration_days: int = DEMO_DEFAULT_DURATION_DAYS,
):
    """
    FAZ 19 — Fast-Track ana servisi.

    Gölge Lead + Gölge Proposal + Stores(status='DEMO') üretir. Mevcut
    `auto_create_store_from_proposal()` akışını yeniden kullanır; böylece
    SSOT korunur (tek mağaza oluşturma yolu).

    8 Adım:
      1. Firma çakışma kontrolü (mevcut Company + Stores)
      2. Yoksa yeni Company oluştur
      3. Gölge Lead oluştur (status='won', channel='other' — fallback)
      4. Gölge Proposal + ProposalItems oluştur (title='[DEMO] ...')
      5. auto_create_store_from_proposal() ile mağazayı üret
      6. status='DEMO', is_active=True, package=demo_paketi, demo_expires_at
      7. PackageApplication varsa proposal_created'e güncelle
      8. (Bildirimler view katmanında / opsiyonel)

    Returns:
        dict: {'store': Stores, 'proposal': Proposals|None, 'lead': Lead|None,
               'is_new': bool, 'error': str|None}
    """
    import logging
    from datetime import date as _date
    from django.utils import timezone
    from apps.stores.models import Stores, Company

    log = logging.getLogger(__name__)

    if not phone or not business_name:
        return {'store': None, 'proposal': None, 'lead': None,
                'is_new': False, 'error': 'phone ve business_name zorunlu.'}

    duration_days = int(duration_days or DEMO_DEFAULT_DURATION_DAYS)
    if duration_days < 1:
        duration_days = DEMO_DEFAULT_DURATION_DAYS

    demo_package = _get_or_resolve_demo_package()
    demo_module = _get_demo_module()

    # Modül listesi: explicit verilmediyse demo-access modülü kullanılır
    requested_module_ids = list(module_ids) if module_ids else []
    if not requested_module_ids and demo_module:
        requested_module_ids = [str(demo_module.id)]

    with transaction.atomic():
        # ── Adım 1: Firma Çakışma Kontrolü ──
        company = Company.objects.filter(phone=phone, is_deleted=False).first()
        existing_store = None
        if company:
            existing_store = Stores.objects.filter(
                company=company, is_deleted=False
            ).first()

        # Aktif/demo/ödeme bekleyen mağaza varsa engelle
        if existing_store and existing_store.status in ('DEMO', 'ACTIVE', 'PENDING_PAYMENT'):
            return {
                'store': existing_store, 'proposal': None, 'lead': None,
                'is_new': False,
                'error': 'Bu firmaya ait zaten aktif/demo bir mağaza var.'
            }

        # EXPIRED mağaza varsa reaktive et (yeni kayıt açma)
        if existing_store and existing_store.status == 'EXPIRED':
            existing_store.status = 'DEMO'
            existing_store.is_active = True
            existing_store.demo_expires_at = timezone.now() + timezone.timedelta(days=duration_days)
            existing_store.demo_converted_at = None
            existing_store.onboarding_source = 'fast_track'
            if demo_package:
                existing_store.package = demo_package
            existing_store.save(update_fields=[
                'status', 'is_active', 'demo_expires_at',
                'demo_converted_at', 'onboarding_source', 'package',
            ])
            if requested_module_ids:
                sync_store_modules(existing_store, requested_module_ids)
            log.info(
                "create_demo_store: EXPIRED mağaza reaktive edildi. store_id=%s",
                existing_store.store_id
            )
            return {
                'store': existing_store, 'proposal': None, 'lead': None,
                'is_new': False, 'error': None,
            }

        # ── Adım 2: Firma Oluştur ──
        if not company:
            company = Company.objects.create(
                title=business_name,
                phone=phone or None,
                email=email or None,
                city=city or None,
                country='Türkiye',
            )

        # ── Adım 3: Gölge Lead Oluştur ──
        lead = None
        try:
            from apps.crm.leads.models import Lead, PackageApplication

            # PackageApplication varsa lead'ini yeniden kullan
            pa = PackageApplication.objects.filter(phone=phone, status='pending').first()
            if pa and pa.lead_id:
                lead = pa.lead
            else:
                # 'fast_track' channel'ı henüz CHANNELS'a eklenmediyse
                # 'other' fallback ile devam et (migration gerektirmez).
                channel_value = 'other'
                try:
                    channel_codes = {c[0] for c in Lead.CHANNELS}
                    if 'fast_track' in channel_codes:
                        channel_value = 'fast_track'
                except Exception:
                    pass

                lead = Lead.objects.create(
                    full_name=f"{first_name} {last_name}".strip() or business_name,
                    business_name=business_name,
                    phone=phone or None,
                    email=email or None,
                    city=city or None,
                    status='won',
                    channel=channel_value,
                    created_by=created_by,
                    store=None,
                )
                if pa and not pa.lead_id:
                    pa.lead = lead
                    pa.save(update_fields=['lead'])
        except Exception as exc:
            log.warning(
                "create_demo_store: Gölge Lead oluşturulamadı, devam ediliyor. err=%s", exc
            )
            lead = None

        # ── Adım 4: Gölge Proposal Oluştur ──
        proposal = None
        try:
            from apps.crm.proposals.models import Proposals, ProposalItems
            from apps.crm.packages.models import SaaSModule

            proposal = Proposals.objects.create(
                company=company,
                lead=lead,
                created_by=created_by,
                title=f"[DEMO] {business_name}",
                status='accepted',
                currency='TRY',
                notes='Otomatik oluşturulmuş demo teklifi. Fast-Track akışı.',
                discount_amount=Decimal('0.00'),
                tax_rate=Decimal('0.00'),
                date=_date.today(),
            )

            for mid in requested_module_ids:
                try:
                    module = SaaSModule.objects.get(id=mid)
                except SaaSModule.DoesNotExist:
                    continue
                ProposalItems.objects.create(
                    proposal=proposal,
                    module=module,
                    description=module.name,
                    quantity=1,
                    unit_price=Decimal('0.00'),
                    maintenance_included=False,
                )
        except Exception as exc:
            # Proposal oluşturulamasa bile mağaza oluşmalı (gölge sipariş opsiyonel kalsın)
            log.warning(
                "create_demo_store: Gölge Proposal oluşturulamadı, devam ediliyor. err=%s", exc
            )
            proposal = None

        # ── Adım 5: Mağaza Oluştur ──
        if proposal:
            store = auto_create_store_from_proposal(proposal)
        else:
            # Proposal yoksa elle oluştur (auto_create_store_from_proposal proposal bekliyor)
            import random
            def _gen_store_id():
                while True:
                    s_id = str(random.randint(10 ** 10, 10 ** 11 - 1))
                    if not Stores.objects.filter(store_id=s_id).exists():
                        return s_id

            store = Stores.objects.create(
                store_id=_gen_store_id(),
                company=company,
                title=business_name,
                email=email or None,
                phone=phone or None,
                city=city or None,
                country='Türkiye',
                is_active=False,
            )
            if requested_module_ids:
                sync_store_modules(store, requested_module_ids)

        if not store:
            return {'store': None, 'proposal': proposal, 'lead': lead,
                    'is_new': False, 'error': 'Mağaza oluşturulamadı.'}

        # ── Adım 6: Demo Statüsü Atama ──
        store.status = 'DEMO'
        store.is_active = True
        if demo_package:
            store.package = demo_package
        store.demo_expires_at = timezone.now() + timezone.timedelta(days=duration_days)
        store.onboarding_source = 'fast_track'
        store.save(update_fields=[
            'status', 'is_active', 'package',
            'demo_expires_at', 'onboarding_source',
        ])

        # ── Adım 7: PackageApplication Güncelleme ──
        try:
            from apps.crm.leads.models import PackageApplication
            qs = PackageApplication.objects.filter(phone=phone, status='pending')
            update_fields = {'status': 'proposal_created'}
            if lead:
                update_fields['lead'] = lead
            if proposal:
                update_fields['proposal'] = proposal
            qs.update(**update_fields)
        except Exception as exc:
            log.warning(
                "create_demo_store: PackageApplication güncellenemedi. err=%s", exc
            )

        log.info(
            "create_demo_store: DEMO mağaza oluşturuldu. store_id=%s expires_at=%s",
            store.store_id, store.demo_expires_at
        )

        return {
            'store': store,
            'proposal': proposal,
            'lead': lead,
            'is_new': True,
            'error': None,
        }


def convert_demo_to_active(
    store,
    package,
    module_ids=None,
    subscription_start=None,
    converted_by=None,
):
    """
    FAZ 19 — DEMO/PENDING_PAYMENT mağazayı ACTIVE'e dönüştürür.

    - Demo modüllerini StoreModule'den temizler
    - Yeni gerçek paketi ve modülleri atar
    - status='ACTIVE', is_active=True, demo_expires_at=None
    - demo_converted_at damgalanır

    Mağaza verileri (ürün, müşteri, satış) korunur. store.id değişmez.

    Returns:
        Stores instance (güncellenmiş)
    Raises:
        ValueError — store.status uygun değilse
    """
    import logging
    from datetime import date as _date
    from django.utils import timezone
    from apps.crm.packages.models import SaaSModule
    from apps.stores.models import StoreModule

    log = logging.getLogger(__name__)

    if not store:
        raise ValueError("store parametresi zorunlu.")
    if store.status not in ('DEMO', 'PENDING_PAYMENT', 'EXPIRED'):
        raise ValueError(
            f"Yalnızca DEMO/PENDING_PAYMENT/EXPIRED mağazalar dönüştürülebilir "
            f"(mevcut: {store.status})."
        )
    if not package:
        raise ValueError("package parametresi zorunlu.")
    if getattr(package, 'is_demo', False):
        raise ValueError("Hedef paket demo paketi olamaz.")

    if subscription_start is None:
        subscription_start = _date.today()

    requested_module_ids = list(module_ids) if module_ids else []

    with transaction.atomic():
        # ── Adım 1: Demo modüllerini temizle ──
        demo_module_ids = list(
            SaaSModule.objects.filter(slug=DEMO_MODULE_SLUG)
            .values_list('id', flat=True)
        )
        if demo_module_ids:
            StoreModule.objects.filter(
                store=store, module_id__in=demo_module_ids
            ).delete()

        # ── Adım 2: Paketi ve modülleri ata ──
        store.package = package
        # sync_store_modules paket içindeki modülleri zaten hariç tutar.
        sync_store_modules(store, requested_module_ids)

        # ── Adım 3: Statü güncelle ──
        store.status = 'ACTIVE'
        store.is_active = True
        store.subscription_start = subscription_start
        store.demo_expires_at = None
        store.demo_converted_at = timezone.now()
        store.save(update_fields=[
            'package', 'status', 'is_active',
            'subscription_start', 'demo_expires_at', 'demo_converted_at',
        ])

        # ── Adım 4: Lead bağlıysa won'a çek ──
        try:
            from apps.crm.leads.models import Lead
            Lead.objects.filter(
                package_applications__proposal__company=store.company
            ).update(status='won')
        except Exception as exc:
            log.warning(
                "convert_demo_to_active: Lead güncellenemedi. err=%s", exc
            )

    log.info(
        "convert_demo_to_active: store_id=%s package=%s converted_by=%s",
        store.store_id, package.code,
        getattr(converted_by, 'username', None)
    )
    return store


def expire_demo_stores():
    """
    FAZ 19 — Süresi dolan DEMO mağazaları otomatik EXPIRED'e çeker.

    Cron / management komutu (`expire_demos`) tarafından çağrılır.
    Idempotent: Süresi dolmamış kayıtlara dokunmaz; aynı mağaza ikinci kez
    işlenmez.

    Returns:
        dict: {'expired_count': int, 'store_ids': list[str]}
    """
    import logging
    from django.utils import timezone
    from apps.stores.models import Stores

    log = logging.getLogger(__name__)

    now = timezone.now()
    qs = Stores.objects.filter(
        status='DEMO',
        demo_expires_at__lt=now,
        is_deleted=False,
    )

    expired_ids = []
    for store in qs:
        try:
            with transaction.atomic():
                store.status = 'EXPIRED'
                store.is_active = False
                store.save(update_fields=['status', 'is_active'])
            expired_ids.append(store.store_id or str(store.id))
        except Exception as exc:
            log.exception(
                "expire_demo_stores: Mağaza EXPIRED'e çekilemedi. store_id=%s err=%s",
                getattr(store, 'store_id', None), exc
            )

    if expired_ids:
        log.info(
            "expire_demo_stores: %d mağaza süresi doldu. ids=%s",
            len(expired_ids), expired_ids
        )

    return {'expired_count': len(expired_ids), 'store_ids': expired_ids}