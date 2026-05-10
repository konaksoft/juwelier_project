from django.db import transaction

from apps.crm.packages.models import SaaSModule, Packages, PackageModule, PackagePermissionMatrix
from apps.roles.models import Permission, Roles, RoleDetail


# ─────────────────────────────────────────────────────────
#  Faz 12.2: Package–Module senkronizasyon servisi
# ─────────────────────────────────────────────────────────

def sync_package_permissions_from_modules(package):
    """
    Bir paketin modül listesine göre PackagePermissionMatrix'i günceller.

    İş mantığı:
      1. Pakete atanmış modüllerin + çekirdek modüllerin permission'larını toplar.
      2. Modül kaynaklı (source='module') mevcut matrix satırlarını temizler.
      3. Toplanan permission'ları source='module' olarak matrix'e yazar.
      4. Elle atanmış (source='manual') satırlara DOKUNMAZ.

    Sonuç: Paket yetkileri = ELLE ATANAN ∪ MODÜLLERDEN GELEN

    Parametreler:
        package: apps.crm.packages.models.Packages instance

    Returns:
        dict: {'added': int, 'removed': int, 'total_module_perms': int}
    """
    # 1) Pakete bağlı modüller + çekirdek modüller
    assigned_module_ids = set(
        PackageModule.objects.filter(package=package)
        .values_list('module_id', flat=True)
    )
    core_module_ids = set(
        SaaSModule.objects.filter(is_core=True, is_active=True)
        .values_list('id', flat=True)
    )
    all_module_ids = assigned_module_ids | core_module_ids

    # 2) Bu modüllerin tüm permission'larını topla (bağımlılıklar dahil)
    all_perm_ids = set()
    for module in SaaSModule.objects.filter(id__in=all_module_ids):
        all_perm_ids |= module.collect_all_permissions()

    with transaction.atomic():
        # 3) Mevcut modül kaynaklı satırları temizle
        removed = PackagePermissionMatrix.objects.filter(
            package=package,
            source='module',
        ).exclude(
            permission_id__in=all_perm_ids
        ).delete()[0]

        # 4) Yeni modül kaynaklı satırları ekle / güncelle
        added = 0
        for perm_id in all_perm_ids:
            obj, created = PackagePermissionMatrix.objects.get_or_create(
                package=package,
                permission_id=perm_id,
                defaults={
                    'available': True,
                    'source': 'module',
                }
            )
            if created:
                added += 1
            elif obj.source == 'module' and not obj.available:
                # Modül kaynaklı ama pasif edilmişse tekrar aktif et
                obj.available = True
                obj.save(update_fields=['available'])
                added += 1

    return {
        'added': added,
        'removed': removed,
        'total_module_perms': len(all_perm_ids),
    }


def get_package_effective_permission_ids(package):
    """
    Bir paketin efektif yetki havuzunu döndürür.
    PackagePermissionMatrix(available=True) kayıtlarının permission_id seti.

    Returns:
        set: Permission UUID seti
    """
    if not package:
        return set()
    return set(
        PackagePermissionMatrix.objects.filter(
            package=package, available=True
        ).values_list('permission_id', flat=True)
    )


# ─────────────────────────────────────────────────────────
#  Faz 12.3: StoreModule tabanlı ek modül yönetimi
#
#  Eski activate_modules_for_store / deactivate_modules_for_store
#  fonksiyonları global Role'a yazıyordu ve çoklu mağaza
#  izolasyonunu bozuyordu. Artık modül atamaları StoreModule
#  tablosu üzerinden yapılıyor, efektif yetkiler ise
#  get_store_effective_permission_ids() ile hesaplanıyor.
#
#  Eski fonksiyonlar geriye uyumluluk için DEPRECATED olarak
#  korunmuştur; yeni geliştirmelerde kullanılmamalıdır.
# ─────────────────────────────────────────────────────────

def activate_modules_for_store(store, modules_list, role=None):
    """
    DEPRECATED — Faz 12.3'te StoreModule tabanlı yapıya geçilmiştir.

    Bu fonksiyon artık StoreModule tablosuna kayıt oluşturarak modül
    ataması yapar. Eski RoleDetail yazma mantığı kaldırılmıştır.

    Yeni kullanım:
        from apps.stores.services import sync_store_modules
        sync_store_modules(store, module_id_listesi)

    Geriye uyumluluk için korunmuştur; mevcut çağrılar kırılmasın diye
    StoreModule'e yönlendirir.

    Parametreler:
        store:        apps.stores.models.Stores instance
        modules_list: SaaSModule queryset veya list
        role:         Artık kullanılmıyor (ignored).
    """
    import warnings
    warnings.warn(
        "activate_modules_for_store() deprecated. "
        "Yerine apps.stores.services.sync_store_modules() kullanın.",
        DeprecationWarning, stacklevel=2
    )

    from apps.stores.models import StoreModule as SM

    added = 0
    for module in modules_list:
        # Ana modülü ekle
        _, created = SM.objects.get_or_create(
            store=store, module=module,
            defaults={'note': 'Legacy activate_modules_for_store() ile eklendi'}
        )
        if created:
            added += 1

        # Bağımlılıkları da ekle
        for dep_id in module.get_all_dependencies():
            try:
                dep_module = SaaSModule.objects.get(id=dep_id)
                _, dep_created = SM.objects.get_or_create(
                    store=store, module=dep_module,
                    defaults={'note': 'Bağımlılık olarak eklendi'}
                )
                if dep_created:
                    added += 1
            except SaaSModule.DoesNotExist:
                continue

    return {
        'created': added,
        'updated': 0,
        'total_permissions': 0,
        'note': 'StoreModule tablosuna yönlendirildi (Faz 12.3)',
    }


def deactivate_modules_for_store(store, modules_list, role=None):
    """
    DEPRECATED — Faz 12.3'te StoreModule tabanlı yapıya geçilmiştir.

    Bu fonksiyon artık StoreModule tablosundan kayıt silerek modül
    atamasını kaldırır. Eski RoleDetail pasif etme mantığı kaldırılmıştır.

    Yeni kullanım:
        from apps.stores.services import sync_store_modules
        sync_store_modules(store, güncel_module_id_listesi)

    Parametreler:
        store:        apps.stores.models.Stores instance
        modules_list: SaaSModule queryset veya list
        role:         Artık kullanılmıyor (ignored).
    """
    import warnings
    warnings.warn(
        "deactivate_modules_for_store() deprecated. "
        "Yerine apps.stores.services.sync_store_modules() kullanın.",
        DeprecationWarning, stacklevel=2
    )

    from apps.stores.models import StoreModule as SM

    remove_module_ids = set()
    for module in modules_list:
        remove_module_ids.add(module.id)
        remove_module_ids |= module.get_all_dependencies()

    if not remove_module_ids:
        return {'deactivated': 0}

    deactivated = SM.objects.filter(
        store=store, module_id__in=remove_module_ids
    ).delete()[0]

    return {
        'deactivated': deactivated,
        'note': 'StoreModule tablosundan silindi (Faz 12.3)',
    }


# ─────────────────────────────────────────────────────────
#  Faz 12.6: Modül Bazlı Paket Kapsam Tablosu
# ─────────────────────────────────────────────────────────

def build_module_scope_table(packages=None):
    """
    Paket bazlı müşteriler için modül–paket karşılaştırma tablosu oluşturur.

    Tüm aktif SaaS modüllerini sırayla gezer, her modülün
    is_system_only=False olan yetkilerini toplar ve
    her yetki için paketlerdeki available durumunu hesaplar.

    Sonuç, modül başlıkları altında gruplanmış yetki satırlarıdır.
    Aynı yetki birden fazla modülde varsa sadece ilk modülde gösterilir.

    Parametreler:
        packages: Queryset veya list — sütun başlıkları olarak kullanılacak
                  paketler. None ise tüm aktif paketler çekilir.

    Returns:
        tuple: (packages_list, module_rows)
            packages_list: list[Packages] — sütun başlıkları
            module_rows: [
                {
                    'module': SaaSModule,
                    'rows': [
                        {
                            'permission': Permission,
                            'cells': [{'available': bool, 'note': str}, ...]
                        }
                    ]
                },
                ...
            ]
    """
    if packages is None:
        packages = list(
            Packages.objects.filter(is_active=True).order_by('order', 'name')
        )
    else:
        packages = list(packages)

    # PackagePermissionMatrix lookup tablosu: (perm_id, pkg_id) → {available, note}
    matrix_entries = PackagePermissionMatrix.objects.filter(
        package__in=packages,
    ).select_related('permission', 'package')

    lookup = {}
    for entry in matrix_entries:
        lookup[(entry.permission_id, entry.package_id)] = {
            'available': entry.available,
            'note': entry.note or '',
        }

    # Aktif modülleri sıralı şekilde getir
    modules = SaaSModule.objects.filter(is_active=True).order_by('order', 'name')

    module_rows = []
    used_perm_ids = set()  # Aynı yetkiyi birden fazla modülde gösterme

    for module in modules:
        # Bu modülün müşteriye gösterilebilir yetkileri (sistem yetkileri hariç)
        mod_perms = module.permissions.filter(
            is_system_only=False,
        ).order_by('order', 'name')

        if not mod_perms.exists():
            continue

        rows = []
        for perm in mod_perms:
            if perm.id in used_perm_ids:
                continue
            used_perm_ids.add(perm.id)

            row_cells = []
            for pkg in packages:
                data = lookup.get(
                    (perm.id, pkg.id),
                    {'available': False, 'note': ''},
                )
                row_cells.append(data)
            rows.append({'permission': perm, 'cells': row_cells})

        if rows:
            module_rows.append({
                'module': module,
                'rows': rows,
            })

    return packages, module_rows


# ─────────────────────────────────────────────────────────
#  Faz 12.8: Paketsiz Müşteri — Modül Kapsam Tablosu
# ─────────────────────────────────────────────────────────

def build_proposal_module_table(proposal):
    """
    Paketsiz teklifler için müşterinin satın aldığı modülleri ve
    her modülün özelliklerini listeler.

    Sonuç: Satın alınan modüller başlık satırı olarak gösterilir,
    altında her modülün sistem yetkileri (özellikleri) listelenir.
    Tüm özellikler dahil (✓) olarak gösterilir çünkü müşteri o modülü
    satın almıştır.

    Çekirdek modüller (is_core=True) her zaman otomatik dahildir ve
    listenin başında gösterilir.

    Parametreler:
        proposal: apps.crm.proposals.models.Proposals instance

    Returns:
        list: [
            {
                'module': SaaSModule,
                'is_core': bool,
                'rows': [
                    {'permission': Permission, 'included': True}
                ]
            },
            ...
        ]
    """
    if not proposal:
        return []

    # Teklifin kalemlerinden modülleri al
    purchased_module_ids = list(
        proposal.items.filter(module__isnull=False)
        .values_list('module_id', flat=True).distinct()
    )

    if not purchased_module_ids:
        return []

    # Satın alınan modüller
    purchased_modules = SaaSModule.objects.filter(
        id__in=purchased_module_ids, is_active=True
    ).order_by('order', 'name')

    # Çekirdek modüller (her zaman dahildir, ayrıca göster)
    core_modules = SaaSModule.objects.filter(
        is_core=True, is_active=True
    ).exclude(id__in=purchased_module_ids).order_by('order', 'name')

    module_rows = []
    used_perm_ids = set()

    # Önce çekirdek modülleri listele
    for module in core_modules:
        perms = module.permissions.filter(
            is_system_only=False,
        ).order_by('order', 'name')

        rows = []
        for perm in perms:
            if perm.id in used_perm_ids:
                continue
            used_perm_ids.add(perm.id)
            rows.append({
                'permission': perm,
                'included': True,
            })

        if rows:
            module_rows.append({
                'module': module,
                'is_core': True,
                'rows': rows,
            })

    # Sonra satın alınan modülleri listele
    for module in purchased_modules:
        perms = module.permissions.filter(
            is_system_only=False,
        ).order_by('order', 'name')

        rows = []
        for perm in perms:
            if perm.id in used_perm_ids:
                continue
            used_perm_ids.add(perm.id)
            rows.append({
                'permission': perm,
                'included': True,
            })

        if rows:
            module_rows.append({
                'module': module,
                'is_core': False,
                'rows': rows,
            })

    return module_rows


# ─────────────────────────────────────────────────────────
#  Faz 59: Sözleşme Müşteri Kapsam Tablosu (ABC pattern)
# ─────────────────────────────────────────────────────────

def build_contract_scope_table(packages=None):
    """
    Sözleşme imza ekranı için müşteri-odaklı paket kapsam tablosu.

    Mevcut build_module_scope_table SaaSModule.permissions M2M üzerinden
    gezdiği için iki ayrı sorun üretir:
      1. Modüllere atanmış teknik permission'lar (add_custody, banking_index
         gibi snake_case kodlar) müşteriye gösterilir.
      2. PackagePermissionMatrix'de var olan ama hiçbir SaaSModule'e
         atanmamış müşteri-odaklı ABC permission'ları (Raporlar, Faturalar,
         Bilezikler vb.) tabloya hiç düşmez.

    Bu fonksiyon yapıyı tersine çevirir: doğrudan PackagePermissionMatrix
    üzerinden ABC pattern'li (^ABC[0-9]+D$) permission'ları sorgular ve
    Permission.order alanına göre düz liste olarak döndürür. Modül
    gruplaması yoktur — sözleşmede her özellik ayrı bir satırdır.

    ABC9xxx kodları (Whatsapp/Rol/Paket Yönetimi gibi SaaS-admin yetkileri)
    pakete satılmadığı için PackagePermissionMatrix'de yer almaz, böylece
    ek bir filtreye gerek kalmaz; sorguya düşmezler.

    Parametreler:
        packages: Queryset veya list — sütun başlıkları olarak kullanılacak
                  paketler. None ise tüm aktif paketler çekilir.

    Returns:
        tuple: (packages_list, contract_rows)
            packages_list: list[Packages] — sütun başlıkları
            contract_rows: [
                {
                    'permission': Permission,
                    'cells': [{'available': bool, 'note': str}, ...]
                },
                ...
            ]  # Düz liste — modül başlığı yok.
    """
    if packages is None:
        packages = list(
            Packages.objects.filter(is_active=True).order_by('order', 'name')
        )
    else:
        packages = list(packages)

    if not packages:
        return [], []

    # ABC pattern: ABC + en az 1 rakam + D (ABC1001D, ABC1908D vb.)
    abc_perm_ids = list(
        Permission.objects.filter(
            code__regex=r'^ABC[0-9]+D$',
            is_system_only=False,
        ).values_list('id', flat=True)
    )

    if not abc_perm_ids:
        return packages, []

    # ABC permission'ların paketlerdeki matrix kayıtları
    matrix_qs = PackagePermissionMatrix.objects.filter(
        package__in=packages,
        permission_id__in=abc_perm_ids,
    ).select_related('permission', 'package')

    lookup = {}
    available_perm_ids = set()
    for entry in matrix_qs:
        lookup[(entry.permission_id, entry.package_id)] = {
            'available': entry.available,
            'note': entry.note or '',
        }
        if entry.available:
            available_perm_ids.add(entry.permission_id)

    # Sadece en az bir aktif paketde available=True olan ABC permission'ları
    # göster — hiçbir pakete satılmamış (orphan/admin) ABC permission'lar
    # tabloda yer almaz. Bu ABC9xxx serisini de otomatik dışlar.
    if not available_perm_ids:
        return packages, []

    permissions = Permission.objects.filter(
        id__in=available_perm_ids,
    ).order_by('order', 'name')

    contract_rows = []
    for perm in permissions:
        cells = []
        for pkg in packages:
            data = lookup.get(
                (perm.id, pkg.id),
                {'available': False, 'note': ''},
            )
            cells.append(data)
        contract_rows.append({
            'permission': perm,
            'cells': cells,
        })

    return packages, contract_rows
