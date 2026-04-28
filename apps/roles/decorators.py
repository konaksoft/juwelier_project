"""
FAZ 4-5 Ek 8 — Hybrid-Gate: URL Guessing Güvenlik Yaması

Üç Katmanlı (3-Tier) Yetki Kontrolü:

  Katman 1 — ABC Menü Kodları (ABC1001D, ABC1706D vb.):
      → Personelin RoleDetail tablosunda bu ABC kodu var mı?

  Katman 2 — Ana Sayfa Index Kodları (MAIN_PAGE_TO_ABC_MAP'te tanımlı):
      → Kod haritada varsa → eşleşen ABC koduna çevir → RoleDetail kontrolü
      → URL Guessing (tarayıcıya URL yazarak bypass) engellemesi

  Katman 3 — Alt İşlem Kodları (Yukarıdaki iki gruba girmeyen):
      → Konasoft personeli (is_staff): RoleDetail kontrolü
      → Mağaza personeli: SADECE mağazanın efektif havuzuna bak
"""
import re
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from apps.roles.models import Permission, RoleDetail
from apps.roles.management.commands._permission_utils import EXCLUDED_APPS

_ABC_PATTERN = re.compile(r'^[A-Z0-9]{2,5}\d{3,5}[A-Z]?$')

# ═══════════════════════════════════════════════════════════════════════
# MAIN_PAGE_TO_ABC_MAP — Ana sayfa index view yetki kodlarını
# sol menüdeki ABC görünürlük kodlarına eşler.
#
# Bu harita sayesinde personel URL'yi elle yazarak (URL Guessing)
# rol kısıtlamasını atlayamaz. Ana sayfa kodu bu haritada bulunursa
# personelin RoleDetail'inde eşleşen ABC kodu aranır.
#
# Format: 'PERMISSION_CODE': 'ABC_MENU_CODE'
# ═══════════════════════════════════════════════════════════════════════
MAIN_PAGE_TO_ABC_MAP = {
    # ── Dashboard ──
    'DASHBOARD_INDEX_VIEW':                     'ABC1002D',

    # ── İşlem Panoları ──
    'TRANSACTIONS_BOARD_FAST_INDEX_VIEW':        'ABC1001D',
    'TRANSACTIONS_BOARD_RETAIL_INDEX_VIEW':      'ABC1310D',
    'TRANSACTIONS_BOARD_WHOLESALE_INDEX_VIEW':   'ABC1030D',
    'TRANSACTIONS_BOARD_OPERATIONS_INDEX':       'ABC1062D',

    # ── Ürünler ──
    'PRODUCTS_PRODUCT_INDEX':                    'ABC1003D',

    # ── Altın Alım ──
    'GOLD_PURCHASES_GOLD_PURCHASES_INDEX':       'ABC1007D',

    # ── Hurda ──
    'SCRAPS_SCRAP_INDEX':                        'ABC1008D',

    # ── Bilezik ──
    'BRACELETS_BRACELET_INDEX':                  'ABC1908D',

    # ── Tamir ──
    'REPAIRS_REPAIR_INDEX':                      'ABC1006D',

    # ── Sayım ──
    'COUNTS_COUNTS_INDEX':                       'ABC1009D',

    # ── Faturalar / İrsaliye ──
    'PROCESS_PROCESS_INDEX':                     'ABC1010D',
    'INVOICES_INVOICES_INDEX':                   'ABC1706D',
    'INVOICES_DASHBOARD_INDEX':                  'ABC1706D',

    # ── Tedarikçiler ──
    'SUPPLIERS_SUPPLIERS_VIEW':                  'ABC1011D',

    # ── Emanet ──
    'CUSTODY_CUSTODY_INDEX':                     'ABC1544D',

    # ── Atölyeler ──
    'WORKSHOPS_WORKSHOPS_VIEW':                  'ABC1012D',

    # ── Müşteriler ──
    'CUSTOMERS_CUSTOMERS_VIEW':                  'ABC1015D',

    # ── Banka Yönetimi ──
    'BANKING_INDEX_VIEW':                        'ABC1706D',
    'BANKING_TRANSACTIONS_INDEX':                'ABC1706D',
    'BANKING_ACCOUNTS_INDEX':                    'ABC1706D',

    # ── Kasa Yönetimi ──
    'BANK_MANAGEMENT_INDEX_VIEW':                'ABC1017D',
    'CASH_MANAGEMENT_INDEX_VIEW':                'ABC1017D',

    # ── Siparişler ──
    'ORDERS_ORDERS_INDEX':                       'ABC9008D',

    # ── Talepler / Destek ──
    'SUPPORTS_INDEX_VIEW':                       'ABCD005D',

    # ── Ayarlar ──
    'STORES_STORES_VIEW':                        'ABC1016D',
    'STORES_DETAIL_VIEW':                        'ABC1016D',
    'SETTINGS_INDEX_VIEW':                       'ABC1016D',
}


def _resolve_app_name_from_view(view_func):
    """
    View fonksiyonunun ait olduğu app_name'i tespit eder.

    view_func.__module__ → 'apps.crm.leads.views' gibi bir dotted path döner.
    Buradan uygulama adını çıkarır:
      - 'apps.customers.views'          → 'customers'
      - 'apps.crm.leads.views'          → 'leads'
      - 'apps.definitions.brands.views' → 'brands'
      - 'apps.process.retail_views'     → 'process'

    Returns:
        str veya None: Tespit edilen app adı. Belirlenemezse None.
    """
    module = getattr(view_func, '__module__', None)
    if not module:
        return None

    parts = module.split('.')
    if len(parts) < 3 or parts[0] != 'apps':
        return None

    return parts[-2]


def _get_effective_perm_codes(user):
    """
    Kullanıcının mağazası üzerinden efektif yetki CODE setini döndürür.

    Hesaplama (ADDITIVE UNION):
        Çekirdek Modül Yetkileri ∪ Paket Yetkileri (varsa) ∪ StoreModule Yetkileri

    FAZ 19 — Mağaza Yaşam Döngüsü Davranışı:
        store.status == 'DEMO':
            Yetki kümesi DEMO SaaSModule (slug='demo-access') üzerinden gelir.
            Demo modülü sistemde yoksa boş set döner; kullanıcı pkg_missing
            sayfasına yönlendirilir.
        store.status in ('EXPIRED', 'SUSPENDED'):
            Boş set döner. Tüm işlevsel yetkiler reddedilir; kullanıcı
            "Demo süreniz doldu / mağazanız askıda" sayfasına düşer.
        Aksi halde (ACTIVE, PENDING_PAYMENT vb.):
            Standart efektif yetki havuzu hesaplanır.

    Sonuçlar request bazlı cache'lenir (_eff_codes).

    Returns:
        set[str]: Permission code'ları seti. Mağaza yoksa boş set.
    """
    if hasattr(user, '_eff_codes'):
        return user._eff_codes

    codes = set()

    store = getattr(user, 'store', None)
    if not store:
        user._eff_codes = codes
        return codes

    # ── FAZ 19: EXPIRED / SUSPENDED → tüm işlevsel yetki reddi ──
    status = getattr(store, 'status', 'ACTIVE')
    if status in ('EXPIRED', 'SUSPENDED'):
        user._eff_codes = set()
        return user._eff_codes

    # ── FAZ 19: DEMO → yetkiler doğrudan demo-access modülünden gelir ──
    if status == 'DEMO':
        try:
            from apps.crm.packages.models import SaaSModule
            demo_module = SaaSModule.objects.filter(
                slug='demo-access', is_active=True
            ).first()
            if demo_module:
                demo_perm_ids = demo_module.collect_all_permissions()
                if demo_perm_ids:
                    codes = set(
                        Permission.objects.filter(id__in=demo_perm_ids)
                        .values_list('code', flat=True)
                    )
        except Exception:
            codes = set()
        user._eff_codes = codes
        return codes

    # ── Standart akış (ACTIVE / PENDING_PAYMENT / diğer) ──
    from apps.stores.services import get_store_effective_permission_ids
    perm_ids = get_store_effective_permission_ids(store)
    if perm_ids:
        codes = set(
            Permission.objects.filter(id__in=perm_ids)
            .values_list('code', flat=True)
        )

    user._eff_codes = codes
    return codes


def _check_role_detail(user, abc_code):
    """
    Kullanıcının RoleDetail'inde belirtilen ABC kodunun
    aktif olarak tanımlı olup olmadığını kontrol eder.

    Returns:
        bool: ABC kodu RoleDetail'de status=True ise True.
    """
    role_id = user.role_id
    if not role_id:
        return False

    return RoleDetail.objects.filter(
        role_id=role_id,
        permission__code=abc_code,
        status=True,
    ).exists()


def _redir(request, reason_code):
    base = reverse("accounts:error")
    q = urlencode({"reason": reason_code, "next": request.get_full_path()})
    return redirect(f"{base}?{q}")


def role_required(permission_code: str, *, require_package=True):
    """
    SaaS Hybrid-Gate yetkilendirme dekoratörü (3 Katmanlı).

    Katman 1 — ABC Menü Kodları (ABC1001D, ABC1706D vb.):
        Personelin RoleDetail tablosunda bu ABC kodu var mı?
        ABC kodları = saf menü görünürlük kodları.
        Menüyü göremeyen personel erişemez.

    Katman 2 — Ana Sayfa Index Kodları (MAIN_PAGE_TO_ABC_MAP):
        Kod haritada varsa → eşleşen ABC koduna çevir → RoleDetail kontrolü.
        Bu katman URL Guessing saldırısını engeller:
        Personel sol menüde göremediği bir sayfaya URL yazarak erişemez.

    Katman 3 — Alt İşlem Kodları:
        Konasoft personeli (is_staff=True):
          → Klasik rol kontrolü (RoleDetail). Paket/modül yok sayılır.
        Mağaza personeli:
          → SADECE mağazanın efektif yetki havuzuna bakılır.
          → Personelin bireysel RoleDetail'i yok sayılır.
          → Mağaza paketinde bu yetki varsa personel erişebilir.

    EXCLUDED_APPS bypass:
        EXCLUDED_APPS listesindeki uygulamalarda SaaS kontrolü atlanır.
    """
    def decorator(view_func):
        # İstisna app tespiti (dekoratör bağlama zamanında, tek sefer)
        app_name = _resolve_app_name_from_view(view_func)
        bypass_check = app_name in EXCLUDED_APPS if app_name else False

        # Kod tipi tespiti (tek sefer, her request'te tekrarlanmaz)
        is_abc = bool(_ABC_PATTERN.match(permission_code))
        mapped_abc = MAIN_PAGE_TO_ABC_MAP.get(permission_code)
        is_main_page = mapped_abc is not None

        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            # ── İstisna app bypass ──
            if bypass_check:
                return view_func(request, *args, **kwargs)

            u = request.user
            if not u.is_authenticated:
                return redirect("accounts:login")

            if u.is_superuser:
                return view_func(request, *args, **kwargs)

            # ─────────────────────────────────────────────
            # KATMAN 1: ABC Menü Kodu → RoleDetail kontrolü
            # ─────────────────────────────────────────────
            if is_abc:
                role_id = u.role_id
                if not role_id:
                    messages.error(
                        request,
                        "Yetkisiz erişim: Rol atanmamış."
                    )
                    return _redir(request, "role_missing")

                if not _check_role_detail(u, permission_code):
                    messages.error(
                        request,
                        "Bu menüye erişim yetkiniz bulunmamaktadır."
                    )
                    return _redir(request, "perm_denied")

                return view_func(request, *args, **kwargs)

            # ─────────────────────────────────────────────
            # KATMAN 2: Ana Sayfa Index Kodu → ABC'ye çevir
            #           → RoleDetail kontrolü
            # (URL Guessing Koruması)
            # ─────────────────────────────────────────────
            if is_main_page:
                role_id = u.role_id
                if not role_id:
                    messages.error(
                        request,
                        "Yetkisiz erişim: Rol atanmamış."
                    )
                    return _redir(request, "role_missing")

                if not _check_role_detail(u, mapped_abc):
                    messages.error(
                        request,
                        "Bu sayfaya erişim yetkiniz bulunmamaktadır."
                    )
                    return _redir(request, "perm_denied")

                return view_func(request, *args, **kwargs)

            # ─────────────────────────────────────────────
            # KATMAN 3a: Alt İşlem — Konasoft personeli (is_staff)
            # Paket/modül olmadığı için klasik rol kontrolü
            # ─────────────────────────────────────────────
            if getattr(u, 'is_staff', False):
                role_id = u.role_id
                if not role_id:
                    messages.error(
                        request,
                        "Yetkisiz erişim: Rol atanmamış."
                    )
                    return _redir(request, "role_missing")

                if not RoleDetail.objects.filter(
                    role_id=role_id,
                    permission__code=permission_code,
                    status=True,
                ).exists():
                    messages.error(
                        request,
                        "Bu işlem için rol yetkiniz yetersiz."
                    )
                    return _redir(request, "perm_denied")

                return view_func(request, *args, **kwargs)

            # ─────────────────────────────────────────────
            # KATMAN 3b: Alt İşlem — Mağaza personeli
            # Personelin bireysel rolü YOK SAYILIR.
            # Sadece mağazanın efektif yetki havuzuna bakılır.
            # ─────────────────────────────────────────────
            if require_package:
                effective_codes = _get_effective_perm_codes(u)

                if not effective_codes:
                    messages.error(
                        request,
                        "Hesabınıza bağlı aktif bir paket veya modül "
                        "bulunamadı. Lütfen yöneticinize başvurun."
                    )
                    return _redir(request, "pkg_missing")

                if permission_code not in effective_codes:
                    messages.error(
                        request,
                        "Bu özellik mevcut paketinizin veya modüllerinizin "
                        "kapsamında değil."
                    )
                    return _redir(request, "pkg_denied")

            # Mağaza havuzunda yetki var → erişim serbest
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator
