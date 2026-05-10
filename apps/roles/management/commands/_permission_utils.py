"""
Ortak yetki keşif yardımcıları.

sync_permissions ve cleanup_permissions komutları bu modülü kullanır.
URL-tabanlı view fonksiyon tespiti burada merkezi olarak yapılır.
"""

import os
import re

# ─────────────────────────────────────────────────────────
#  Sabitler
# ─────────────────────────────────────────────────────────

APP_PATH = 'apps'

# ─────────────────────────────────────────────────────────
# İSTİSNA UYGULAMALAR (EXCLUDED APPS)
# ─────────────────────────────────────────────────────────
# Bu listedeki uygulamalar SaaS rol/permission sisteminin
# tamamen dışında tutulur:
#   - sync_permissions komutu bu app'lerin URL'lerini TARAMAZ
#   - cleanup_permissions bu app'lere ait mevcut permission'ları
#     agresif olarak siler
#   - @role_required dekoratörü ve RolePermissionMiddleware
#     bu app'lerin view'larını bypass eder
#   - Bu app'ler yalnızca Django'nun native @login_required
#     veya public erişim kurallarıyla çalışır
#
# Listeyi düzenlemek için buraya app_name değerlerini ekle/çıkar.
# app_name = urls.py'deki app_name değeridir.
# ─────────────────────────────────────────────────────────
EXCLUDED_APPS = {
    'kuyumplus',
    'contact_forms',
    # 'pavo' kaldırıldı — app yok
    'testimonials',
    'brands',
    # 'masak' kaldırıldı — app yok
    'currencies',
    'process',
    'categories',
    'whatsapp',
    'rates',
}

# Dashboard grubu: Kuyumcu mağazası günlük operasyonlarını
# kapsayan uygulamalar. Mağaza yöneticisi personel rolü
# oluştururken yalnızca bu gruptaki yetkileri seçebilir.
#
# Diğer tüm uygulamalar kendi app_name'lerini group olarak
# alır ve is_system_only=True işaretlenir.
DASHBOARD_APPS = {
    'dashboard',
    'products',
    'gold_purchases',
    'scraps',
    'repairs',
    'counts',
    'suppliers',
    'custody',
    'banking',
    'bank_management',
    'workshops',
    'customers',
    'orders',
    'bracelets',
    'transactions_board',
    # 'invoices' kaldırıldı — app yok
    'inventories',
    'stock_management',
    'live_board',
    'settings',
    'supports',
}

# ─────────────────────────────────────────────────────────
# APP → GRUP OVERRIDE'LARI
# ─────────────────────────────────────────────────────────
# Bazı uygulamaların Permission.group değeri, app_name'den
# farklı bir grup slug'ı almalıdır. Bu sözlük bu eşleştirmeyi
# sağlar. Burada olmayan app'ler varsayılan olarak kendi
# app_name'lerini group olarak alır.
APP_GROUP_OVERRIDES = {
    'bank_management': 'cash_management',
    'supports': 'requests',
}

# ─────────────────────────────────────────────────────────
# TEKİL İZİN KODU → GRUP OVERRIDE'LARI
# ─────────────────────────────────────────────────────────
# transactions_board uygulaması tek bir app_name altında
# 4 farklı işlem tipini barındırıyor. Switch UI'de bunların
# ayrı gruplar olarak görünmesi için tekil permission
# kodlarına özel grup slug'ları atanır.
# Burada tanımlanan kodlar, APP_GROUP_OVERRIDES'tan önceliklidir.
PERM_GROUP_OVERRIDES = {
    'TRANSACTIONS_BOARD_FAST_INDEX_VIEW': 'transactions_board_fast',
    'TRANSACTIONS_BOARD_RETAIL_INDEX_VIEW': 'transactions_board_retail',
    'TRANSACTIONS_BOARD_WHOLESALE_INDEX_VIEW': 'transactions_board_wholesale',
    'TRANSACTIONS_BOARD_OPERATIONS_INDEX': 'transactions_board_process',
}

# ABC-pattern menü yetkileri. Özel menü görünürlük kodlarıdır
# ve çöp olarak işaretlenmemelidir.
# Örnekler: ABC1001D, J5J1000, MK2001, BANK3005A
ABC_PATTERN = re.compile(r'^[A-Z0-9]{2,5}\d{3,5}[A-Z]?$')

# Türkçe isim eşleştirme haritası (sync_permissions tarafından kullanılır)
TURKISH_NAME_MAP = {
    'index': 'Görüntüleme',
    'add': 'Ekleme',
    'edit': 'Düzenleme',
    'delete': 'Silme',
    'change': 'Durum Değiştirme',
    'profile': 'Profil',
    'detail': 'Detay',
    'list': 'Listeleme',
    'logout': 'Çıkış',
    'register': 'Kayıt',
    'reset': 'Sıfırlama',
    'create': 'Oluşturma',
    'update': 'Güncelleme',
    'get': 'Veri Çekme',
    'save': 'Kaydetme',
    'download': 'İndirme',
    'upload': 'Yükleme',
    'send': 'Gönderme',
    'search': 'Arama',
    'export': 'Dışa Aktarma',
    'import': 'İçe Aktarma',
    'complete': 'Tamamlama',
    'cancel': 'İptal',
    'approve': 'Onaylama',
    'reject': 'Reddetme',
    'check': 'Kontrol',
    'allocate': 'Tahsis',
    'convert': 'Dönüştürme',
    'open': 'Açma',
    'close': 'Kapatma',
    'receive': 'Alma',
    'transfer': 'Transfer',
    'process': 'İşlem',
    'dashboard': 'Kontrol Paneli',
}

# ─────────────────────────────────────────────────────────
#  Regex kalıpları
# ─────────────────────────────────────────────────────────

# urls.py'den app_name çıkarma
_APP_NAME_RE = re.compile(r"app_name\s*=\s*['\"]([\w-]+)['\"]")
# path() çağrılarından view fonksiyon adı çıkarma
# Desteklenen formatlar:
#   path('endpoint', function_name, name='xxx')
#   path('endpoint/<uuid:id>', function_name, name='xxx')
#   path("endpoint", function_name, name="xxx")
_PATH_FUNC_RE = re.compile(
    r"""path\(\s*['"][^'"]*['"]\s*,\s*(\w+)\s*,""",
)

# Import satırlarından kaynak modül tespiti
# from apps.X.views import *           → apps/X/views.py
# from apps.X.retail_views import *    → apps/X/retail_views.py
# from apps.crm.leads.views import *   → apps/crm/leads/views.py
_WILDCARD_IMPORT_RE = re.compile(
    r"from\s+(apps[\w.]+)\s+import\s+\*"
)

# Bir .py dosyasındaki fonksiyon tanımlarını bulmak için
_FUNC_DEF_RE = re.compile(r"def\s+(\w+)\s*\(")


# ─────────────────────────────────────────────────────────
#  Yardımcı fonksiyonlar
# ─────────────────────────────────────────────────────────

def is_excluded_app(app_name):
    """
    Verilen app_name'in istisna listesinde olup olmadığını döndürür.

    İstisna uygulamalar SaaS rol/permission sisteminin tamamen
    dışında tutulur. Yalnızca Django native auth ile çalışırlar.
    """
    return app_name in EXCLUDED_APPS


def get_turkish_name(func_name):
    """Fonksiyon adından Türkçe okunabilir isim üretir."""
    for key, value in TURKISH_NAME_MAP.items():
        if func_name.startswith(key):
            kalan = func_name[len(key):].replace('_', ' ').strip().capitalize()
            return f"{value} {kalan}".strip()
    return func_name.replace('_', ' ').capitalize()


def resolve_permission_group(app_name, perm_code=None):
    """
    Uygulama adı ve isteğe bağlı permission koduna göre
    Permission.group ve is_system_only değerlerini döndürür.

    Öncelik sırası:
      1. PERM_GROUP_OVERRIDES — tekil permission kodu eşleşmesi (en yüksek)
      2. APP_GROUP_OVERRIDES — app bazlı grup yönlendirmesi
      3. Varsayılan — app_name'in kendisi

    Dashboard grubundaki uygulamalar is_system_only=False alır.
    Diğerleri is_system_only=True alır.

    Args:
        app_name: urls.py'deki app_name (normalize edilmiş)
        perm_code: Oluşturulacak permission kodu (opsiyonel)

    Returns:
        tuple: (group_name: str, is_system_only: bool)
    """
    is_system_only = app_name not in DASHBOARD_APPS

    # 1. Tekil permission kodu override'ı
    if perm_code and perm_code in PERM_GROUP_OVERRIDES:
        return PERM_GROUP_OVERRIDES[perm_code], is_system_only

    # 2. App bazlı override
    if app_name in APP_GROUP_OVERRIDES:
        return APP_GROUP_OVERRIDES[app_name], is_system_only

    # 3. Varsayılan
    return app_name, is_system_only


def _module_path_to_file(dotted_path):
    """
    Python modül yolunu dosya yoluna dönüştürür.
    'apps.crm.leads.views' → 'apps/crm/leads/views.py'
    """
    return dotted_path.replace('.', os.sep) + '.py'


def _find_all_urls_files():
    """
    apps/ dizini altındaki tüm urls.py dosyalarını bulur.
    Proje kökündeki (jewelery_project/urls.py) dosyayı DAHIL ETMEZ;
    sadece uygulama seviyesi urls.py dosyalarını döndürür.

    Returns:
        list[str]: urls.py dosya yollarının listesi
    """
    result = []
    for root, dirs, files in os.walk(APP_PATH):
        # __pycache__ ve migrations dizinlerini atla
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'migrations', '__pycache')]
        if 'urls.py' in files:
            result.append(os.path.join(root, 'urls.py'))
    return sorted(result)


def _parse_urls_file(urls_path):
    """
    Tek bir urls.py dosyasını parse eder.

    Returns:
        dict veya None:
            {
                'app_name': str,
                'func_names': set[str],   — path() içinde referans edilen fonksiyonlar
                'source_modules': list[str],  — import edilen modül dotted path'leri
                'urls_path': str,
            }
        app_name bulunamazsa None döner.
    """
    try:
        with open(urls_path, encoding='utf-8') as f:
            content = f.read()
    except (IOError, OSError):
        return None

    if not content.strip():
        return None

    # app_name çıkar
    m = _APP_NAME_RE.search(content)
    if not m:
        return None
    app_name = m.group(1).replace('-', '_')
    # path() fonksiyon adlarını çıkar
    func_names = set(_PATH_FUNC_RE.findall(content))
    if not func_names:
        return None

    # Wildcard import edilen kaynak modülleri çıkar
    source_modules = _WILDCARD_IMPORT_RE.findall(content)

    return {
        'app_name': app_name,
        'func_names': func_names,
        'source_modules': source_modules,
        'urls_path': urls_path,
    }


def _get_defined_functions(file_path):
    """
    Bir Python dosyasındaki tüm fonksiyon tanımlarını döndürür.

    Returns:
        set[str]: Fonksiyon adları seti. Dosya bulunamazsa boş set.
    """
    try:
        with open(file_path, encoding='utf-8') as f:
            content = f.read()
    except (IOError, OSError):
        return set()
    return set(_FUNC_DEF_RE.findall(content))


# ─────────────────────────────────────────────────────────
#  Ana keşif fonksiyonu
# ─────────────────────────────────────────────────────────

def discover_url_routed_views():
    """
    Projedeki tüm urls.py dosyalarını tarayarak URL-routed view
    fonksiyonlarını keşfeder.

    EXCLUDED_APPS listesindeki uygulamalar tamamen atlanır —
    bu app'lerin view'ları için permission oluşturulmaz.

    Her bir urls.py için:
      1. app_name okunur (group olarak kullanılacak)
      2. EXCLUDED_APPS kontrolü yapılır — listedeyse atlanır
      3. path() çağrılarından fonksiyon adları çıkarılır
      4. Wildcard import edilen view dosyalarında fonksiyonların
         gerçekten tanımlı olduğu doğrulanır

    Returns:
        list[dict]: Her dict şu alanları içerir:
            - app_name   (str):  urls.py'deki app_name
            - func_name  (str):  View fonksiyon adı
            - code       (str):  Permission kodu (APP_NAME_FUNC_NAME)
            - group      (str):  Permission group değeri
            - is_system_only (bool): Sistem yetkisi mi?
            - source_file (str): Fonksiyonun tanımlı olduğu dosya yolu
    """
    results = []
    seen_codes = set()

    urls_files = _find_all_urls_files()

    for urls_path in urls_files:
        parsed = _parse_urls_file(urls_path)
        if not parsed:
            continue

        app_name = parsed['app_name']

        # ── İstisna uygulamaları atla ──
        if is_excluded_app(app_name):
            continue

        func_names = parsed['func_names']
        source_modules = parsed['source_modules']

        # Import edilen view dosyalarındaki fonksiyon tanımlarını topla
        # Her dosya → o dosyadaki fonksiyon adları seti
        module_func_map = {}
        for mod_dotted in source_modules:
            file_path = _module_path_to_file(mod_dotted)
            if os.path.exists(file_path):
                module_func_map[file_path] = _get_defined_functions(file_path)

        # Eğer hiç import bulunamadıysa, urls.py'nin bulunduğu dizindeki
        # views.py'yi varsayılan olarak dene
        if not module_func_map:
            urls_dir = os.path.dirname(urls_path)
            fallback_views = os.path.join(urls_dir, 'views.py')
            if os.path.exists(fallback_views):
                module_func_map[fallback_views] = _get_defined_functions(fallback_views)

        # Tüm kaynak dosyalardaki fonksiyon adlarını birleştir
        all_defined_funcs = {}
        for file_path, funcs in module_func_map.items():
            for fn in funcs:
                if fn not in all_defined_funcs:
                    all_defined_funcs[fn] = file_path

        for fn in sorted(func_names):
            code = f"{app_name.upper()}_{fn.upper()}"

            # Aynı kodu iki kez ekleme (farklı urls.py'lerden gelebilir)
            if code in seen_codes:
                continue

            # Her permission için grup ve is_system_only değerini çözümle
            # (PERM_GROUP_OVERRIDES ve APP_GROUP_OVERRIDES desteği)
            group, is_system_only = resolve_permission_group(app_name, perm_code=code)

            # Fonksiyonun gerçekten bir view dosyasında tanımlı olduğunu doğrula
            source_file = all_defined_funcs.get(fn)
            if source_file is None:
                # Hiçbir kaynak dosyada bulunamadı — muhtemelen
                # başka bir modülden import edilmiş, yine de ekle
                # ama source_file boş kalır
                source_file = ''

            seen_codes.add(code)
            results.append({
                'app_name': app_name,
                'func_name': fn,
                'code': code,
                'group': group,
                'is_system_only': is_system_only,
                'source_file': source_file,
            })

    return results


def build_valid_code_set():
    """
    Geçerli permission kodlarının setini döndürür.
    cleanup_permissions komutu tarafından çöp tespiti için kullanılır.

    Returns:
        set[str]: Geçerli permission kodları (örn: 'CUSTOMERS_ADD_CUSTOMER')
    """
    views = discover_url_routed_views()
    return {v['code'] for v in views}


def build_excluded_app_prefixes():
    """
    İstisna uygulamalara ait permission kodlarının prefix'lerini döndürür.
    cleanup_permissions komutu tarafından agresif silme için kullanılır.

    Örnek: EXCLUDED_APPS = {'kuyumplus'} → prefixes = {'KUYUMPLUS_'}

    Returns:
        set[str]: Permission kodu prefix'leri
    """
    return {f"{app.upper()}_" for app in EXCLUDED_APPS}
