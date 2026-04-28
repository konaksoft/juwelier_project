from django.utils.text import slugify  # gerekirse
from apps.crm.packages.models import PackagePermissionMatrix

# ---- MENÜ TANIMI ----
MENU_DEF = [
    {"section": "Sayfalar", "items": [
        {"code": "ABC1001", "title": "Hızlı İşlem", "urlname": "transactions-board:fast-index", "icon": "fa-solid fa-bolt"},
        {"code": "ABC1001", "title": "Perakende İşlem", "urlname": "transactions-board:retail-index", "icon": "fa-solid fa-store"},
        {"code": "ABC1001", "title": "Toptan İşlem", "urlname": "transactions-board:wholesale-index", "icon": "fa-solid fa-warehouse"},
        {"code": "ABC1002", "title": "Rapor ve Grafikler", "urlname": "dashboard:index", "icon": "fa-solid fa-chart-simple"},
    ]},
    {"section": "Ürün Yönetimi", "items": [
        {"code": "ABC1003", "title": "Ürünler ve Stok", "urlname": "products:index", "icon": "fa-solid fa-boxes-stacked"},
        {"code": "ABC1007", "title": "Barkodlu Ürünler", "urlname": "gold-purchases:index", "icon": "fa-solid fa-barcode"},
        {"code": "ABC1008", "title": "Hurdalar", "urlname": "scraps:index", "icon": "fa-solid fa-recycle"},
        {"code": "ABC1006", "title": "Ürün Tamir", "urlname": "repairs:index", "icon": "fa-solid fa-screwdriver-wrench"},
        {"code": "ABC1009", "title": "Sayım", "urlname": "counts:index", "icon": "fa-solid fa-hourglass"},
    ]},
    {"section": "Cari Yönetimi", "items": [
        {"code": "ABC1010", "title": "İşlemler", "urlname": "process:index", "icon": "fa-solid fa-list-check"},
    ]},
    {"section": "Mağaza", "items": [
        {"code": "ABC1011", "title": "Tedarikçiler", "urlname": "suppliers:index", "icon": "fa-solid fa-truck"},
        {"code": "ABC1011", "title": "Emanet Yönetimi", "urlname": "custody:index", "icon": "fa-solid fa-handshake"},
        {"code": "ABC10118", "title": "Bankalar", "urlname": "banks:index", "icon": "fa-solid fa-bank"},
        {"code": "ABC1012", "title": "Atölyeler", "urlname": "workshops:index", "icon": "fa-solid fa-industry"},
        {"code": "ABC1015", "title": "Müşteriler", "urlname": "customers:index", "icon": "fa-solid fa-users"},
        {"code": "ABC1014", "title": "Markalar", "urlname": "brands:index", "icon": "fa-solid fa-tags"},
        {"code": "ABC1016", "title": "Mağaza Yönetimi", "urlname": "stores:detail", "icon": "fa-solid fa-store", "needs_store": True},
    ]},
]

ADMIN_MENU = [{
    "section": "Admin Yönetimi",
    "items": [
        {"title": "Rol Yönetimi", "urlname": "roles:index", "icon": "bi bi-person-lock"},
        {"title": "Mağazalar", "urlname": "stores:index", "icon": "bi bi-shop"},
        {"title": "Paketler", "urlname": "packages:index", "icon": "fa-solid fa-boxes-packing"},
        {"title": "İletişim", "urlname": "contact-forms:index", "icon": "fa-solid fa-address-book"},
    ]}]

ALWAYS_MENU = [{
    "section": None,
    "items": [{"title": "Çıkış", "urlname": "accounts:logout", "icon": "bi bi-box-arrow-in-right", "always": True}],
}]


def _resolve_user_package_id(user):
    if getattr(user, "package_id", None):
        return user.package_id
    store = getattr(user, "store", None)
    if store and getattr(store, "package_id", None):
        return store.package_id
    return None


def _build_menu_tree(user):
    is_super = bool(getattr(user, "is_superuser", False))
    has_store = bool(getattr(user, "store_id", None))
    package_id = _resolve_user_package_id(user)

    def allowed(item):
        if is_super or item.get("always"):
            return True
        if item.get("needs_store") and not has_store:
            return False
        if not package_id:
            return False
        # MENÜ: sadece PAKET-İZİN'e göre göster
        return PackagePermissionMatrix.objects.filter(
            package_id=package_id,
            permission__code=item.get("code"),
            available=True
        ).exists()

    tree = []
    for section in MENU_DEF:
        children = [it for it in section["items"] if allowed(it)]
        if children:
            tree.append({"section": section["section"], "children": children})

    if is_super:
        for sec in ADMIN_MENU:
            tree.append({"section": sec["section"], "children": sec["items"]})

    for sec in ALWAYS_MENU:
        tree.append({"section": sec["section"], "children": sec["items"]})

    return tree


def get_user_permissions(request):
    # sadece menü için context yeterli; rol kodlarına artık burada ihtiyacın yoksa kaldırılabilir.
    return {"menu_tree": _build_menu_tree(request.user)}