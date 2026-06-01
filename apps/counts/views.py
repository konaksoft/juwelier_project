import json
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum, Count, Q, DecimalField, Value
from django.db.models.functions import Coalesce, Lower
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from xhtml2pdf import pisa

from apps.counts.models import InventoryCountSession, InventoryCountItem
from apps.definitions.categories.models import Categories
from apps.gold_purchases.models import GoldPurchases
from apps.products.models import Products, MaterialType
from apps.roles.decorators import role_required
from apps.stores.models import Stores


# ═══════════════════════════════════════════════════════════════════
# SAYIM "SATILDI" SSOT (FAZ 9.8 ile birebir uyumlu)
# ═══════════════════════════════════════════════════════════════════
# gold_purchases/views.py:906 ile aynı tanım:
#   sold_q = Q(is_status=False) | Q(product__is_completed=True)
# Satılmış barkodlu ürünler sayım hedefinden, bulunanlardan ve
# raporlardan çıkarılmalıdır. Bu helper SSOT olarak kullanılır.
def _sold_q_for_gold_purchases():
    """GoldPurchases queryset'i için 'satıldı' Q ifadesi (FAZ 9.8)."""
    return Q(is_status=False) | Q(product__is_completed=True)


def _is_product_sold(product):
    """Bir Products instance'ı satılmış mı? (scan guard'larında kullanılır.)

    GoldPurchases.is_status=False da satışı temsil eder; bu nedenle
    ilgili kayıt da kontrol edilir. Kayıt yoksa yalnızca is_completed bakılır.
    """
    if product is None:
        return False
    if getattr(product, 'is_completed', False) is True:
        return True
    try:
        gp_active_exists = GoldPurchases.objects.filter(
            product=product, is_deleted=False, is_status=True
        ).exists()
        gp_any_exists = GoldPurchases.objects.filter(
            product=product, is_deleted=False
        ).exists()
        # GoldPurchases satırı var ama hiçbiri 'tezgahta' (is_status=True) değilse satılmış kabul et.
        if gp_any_exists and not gp_active_exists:
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════════
# FAZ 2: KAPSAM (SCOPE) YARDIMCI FONKSIYONLARI
# ═══════════════════════════════════════════════════════════════════
# Kapsam tipine göre Products veya GoldPurchases queryset'i üzerinde
# uygulanacak Q() filtresi üretir. Backward-compatible: scope_type='ALL'
# veya boş ise hiçbir ek filtre uygulanmaz.

def _build_scope_q_for_product(scope_type, scope_filter):
    """
    Products queryset'i için kapsam Q() filtresi döner.
    'ALL' veya geçersiz tip için Q() (kimseyi dışlamayan) döner.
    """
    if not scope_type or scope_type == InventoryCountSession.ScopeType.ALL:
        return Q()

    scope_filter = scope_filter or {}

    if scope_type == InventoryCountSession.ScopeType.CATEGORY:
        ids = scope_filter.get('category_ids') or []
        if ids:
            return Q(category_id__in=ids)
        return Q(pk__in=[])  # boş kapsam -> hiçbir ürün dahil değil

    if scope_type == InventoryCountSession.ScopeType.MATERIAL:
        mats = scope_filter.get('material_types') or []
        if mats:
            return Q(material_type__in=mats)
        return Q(pk__in=[])

    if scope_type == InventoryCountSession.ScopeType.BRAND:
        brands = scope_filter.get('brands') or []
        if brands:
            return Q(brand__in=brands)
        return Q(pk__in=[])

    if scope_type == InventoryCountSession.ScopeType.JEWELRY_TYPE:
        jtypes = scope_filter.get('jewelry_types') or []
        if jtypes:
            return Q(jewelry_type__in=jtypes)
        return Q(pk__in=[])

    return Q()


def _build_scope_q_for_gold_purchases(scope_type, scope_filter):
    """
    GoldPurchases queryset'i için kapsam Q() filtresi.
    GoldPurchases, product üzerinden ilişkilendirildiği için product__ prefix'i kullanılır.
    """
    if not scope_type or scope_type == InventoryCountSession.ScopeType.ALL:
        return Q()

    scope_filter = scope_filter or {}

    if scope_type == InventoryCountSession.ScopeType.CATEGORY:
        ids = scope_filter.get('category_ids') or []
        if ids:
            return Q(product__category_id__in=ids)
        return Q(pk__in=[])

    if scope_type == InventoryCountSession.ScopeType.MATERIAL:
        mats = scope_filter.get('material_types') or []
        if mats:
            return Q(product__material_type__in=mats)
        return Q(pk__in=[])

    if scope_type == InventoryCountSession.ScopeType.BRAND:
        brands = scope_filter.get('brands') or []
        if brands:
            return Q(product__brand__in=brands)
        return Q(pk__in=[])

    if scope_type == InventoryCountSession.ScopeType.JEWELRY_TYPE:
        jtypes = scope_filter.get('jewelry_types') or []
        if jtypes:
            return Q(product__jewelry_type__in=jtypes)
        return Q(pk__in=[])

    return Q()


def _product_in_scope(product, scope_type, scope_filter):
    """
    Python tarafında tek bir Products nesnesinin kapsama dahil olup olmadığını
    kontrol eder (bulk scan'de her ürün için DB sorgusu yapmamak için).
    """
    if not scope_type or scope_type == InventoryCountSession.ScopeType.ALL:
        return True

    scope_filter = scope_filter or {}

    if scope_type == InventoryCountSession.ScopeType.CATEGORY:
        ids = scope_filter.get('category_ids') or []
        cat_id = product.category_id
        # UUID ve string karşılaştırmasını güvenli yap
        return cat_id is not None and str(cat_id) in {str(x) for x in ids}

    if scope_type == InventoryCountSession.ScopeType.MATERIAL:
        mats = scope_filter.get('material_types') or []
        return (product.material_type or '') in mats

    if scope_type == InventoryCountSession.ScopeType.BRAND:
        brands = scope_filter.get('brands') or []
        return (product.brand or '') in brands

    if scope_type == InventoryCountSession.ScopeType.JEWELRY_TYPE:
        jtypes = scope_filter.get('jewelry_types') or []
        return (product.jewelry_type or '') in jtypes

    return True


def _normalize_scope_payload(scope_type_raw, scope_filter_raw, scope_label_raw):
    """
    Frontend'ten gelen kapsam parametrelerini temizler ve doğrular.
    Dönüş: (scope_type, scope_filter_dict, scope_label) veya (_, _, _, error_msg).
    """
    scope_type = (scope_type_raw or '').strip().upper() or InventoryCountSession.ScopeType.ALL
    valid_types = {t[0] for t in InventoryCountSession.ScopeType.choices}
    if scope_type not in valid_types:
        return None, None, None, f"Geçersiz kapsam tipi: {scope_type}"

    # scope_filter JSON string olarak gelebilir
    scope_filter = {}
    if scope_type != InventoryCountSession.ScopeType.ALL:
        if isinstance(scope_filter_raw, dict):
            scope_filter = scope_filter_raw
        elif isinstance(scope_filter_raw, str) and scope_filter_raw.strip():
            try:
                scope_filter = json.loads(scope_filter_raw)
                if not isinstance(scope_filter, dict):
                    return None, None, None, "scope_filter bir JSON nesnesi olmalı."
            except (json.JSONDecodeError, ValueError):
                return None, None, None, "scope_filter geçerli JSON değil."

        # En az bir değer olmalı
        has_any = any([
            scope_filter.get('category_ids'),
            scope_filter.get('material_types'),
            scope_filter.get('brands'),
            scope_filter.get('jewelry_types'),
        ])
        if not has_any:
            return None, None, None, "Kapsam seçildi fakat hiçbir değer belirtilmedi."

    scope_label = (scope_label_raw or '').strip()[:255]

    return scope_type, scope_filter, scope_label, None


def _same_scope(session, scope_type, scope_filter):
    """
    Mevcut oturumun kapsamı, istenenle aynı mı?
    Kısmi JSON karşılaştırması; listeler sıralanarak eşitlenir.
    """
    if session.scope_type != scope_type:
        return False
    a = session.scope_filter or {}
    b = scope_filter or {}
    # Değerleri normalize et
    def norm(d):
        return {k: sorted(str(x) for x in (v or [])) for k, v in d.items() if v}
    return norm(a) == norm(b)


# ═══════════════════════════════════════════════════════════════════


@login_required(login_url='login')
@role_required('COUNTS_COUNTS_INDEX')
def counts_index(request):
    stores = Stores.objects.all()
    context = {
        'stores': stores,
        'title': 'Sayım'
    }
    return render(request, 'management/counts/index.html', context)


@require_http_methods(["POST"])
@login_required
def start_or_continue_session(request):
    """
    FAZ 2: Kapsam bilinçli oturum başlatma.

    İsteğe bağlı POST parametreleri:
      - scope_type: 'ALL' (varsayılan) | 'CATEGORY' | 'MATERIAL' | 'BRAND' | 'JEWELRY_TYPE'
      - scope_filter: JSON string. Örn: '{"category_ids": ["uuid1"]}'
      - scope_label: Okunur etiket. Örn: '14 Ayar Yüzükler'

    Geriye uyum: parametreler gönderilmezse ALL kapsamıyla eski davranış.

    Açık oturum varsa:
      - Kapsamları aynıysa devam (result=True)
      - Farklıysa conflict (result=False, existing_session_info içerir)
    """
    user_store = request.user.store
    if not user_store:
        return JsonResponse({'error': True, 'error_msg': 'Kullanıcının bağlı olduğu bir mağaza tanımlı değil.'})

    scope_type, scope_filter, scope_label, err = _normalize_scope_payload(
        request.POST.get('scope_type'),
        request.POST.get('scope_filter'),
        request.POST.get('scope_label'),
    )
    if err:
        return JsonResponse({'error': True, 'error_msg': err})

    session = InventoryCountSession.objects.filter(store=user_store, is_closed=False).first()
    if session:
        if _same_scope(session, scope_type, scope_filter):
            return JsonResponse({
                'result': True,
                'session_id': str(session.id),
                'scope_type': session.scope_type,
                'scope_filter': session.scope_filter or {},
                'scope_label': session.scope_label or '',
                'message': 'Devam eden sayım oturumu bulundu.',
            })
        # Kapsam uyumsuz — kullanıcıya conflict döndür
        return JsonResponse({
            'error': True,
            'conflict': True,
            'error_msg': (
                'Bu mağaza için zaten açık bir sayım oturumu var ve kapsamı '
                'farklı. Önce mevcut oturumu bitirin veya sıfırlayın.'
            ),
            'existing_session': {
                'session_id': str(session.id),
                'scope_type': session.scope_type,
                'scope_label': session.scope_label or '',
                'start_time': timezone.localtime(session.start_time).strftime('%d.%m.%Y %H:%M'),
            },
        })

    new_session = InventoryCountSession.objects.create(
        store=user_store,
        created_by=request.user,
        start_time=timezone.now(),
        is_closed=False,
        scope_type=scope_type,
        scope_filter=scope_filter,
        scope_label=scope_label,
    )
    return JsonResponse({
        'result': True,
        'session_id': str(new_session.id),
        'scope_type': new_session.scope_type,
        'scope_filter': new_session.scope_filter or {},
        'scope_label': new_session.scope_label or '',
        'message': 'Yeni sayım oturumu başlatıldı.'
    })


@require_http_methods(["POST"])
@login_required
def close_session(request):
    session_id = request.POST.get('session_id')
    if not session_id:
        return JsonResponse({'error': True, 'error_msg': 'Oturum ID gerekli.'})

    try:
        session = get_object_or_404(InventoryCountSession, id=session_id)
        if session.is_closed:
            return JsonResponse({'error': True, 'error_msg': 'Sayım oturumu zaten kapatılmış.'})
        session.is_closed = True
        session.end_time = timezone.now()
        session.save()
        return JsonResponse({'result': True, 'message': 'Sayım oturumu kapatıldı.'})
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': str(e)})


@require_http_methods(["GET"])
@login_required
def get_stock_count_data(request):
    """
    FAZ 2: Aktif/son oturum kapsamına göre veri döner.
    Kapsam yoksa veya ALL ise eski davranış aynen korunur.
    """
    user_store = request.user.store
    if not user_store:
        return JsonResponse({'error': True, 'error_msg': 'Kullanıcının bağlı olduğu bir mağaza yok.'})

    # Aktif oturumu bul (kapsamı öğrenmek için)
    active_session = InventoryCountSession.objects.filter(
        store=user_store, is_closed=False
    ).first()

    scope_type = active_session.scope_type if active_session else InventoryCountSession.ScopeType.ALL
    scope_filter = active_session.scope_filter if active_session else {}

    scope_q = _build_scope_q_for_gold_purchases(scope_type, scope_filter)

    base = GoldPurchases.objects.filter(
        product__store=user_store,
        is_deleted=False,
        product__is_deleted=False,
        product__is_active=True,
    ).exclude(_sold_q_for_gold_purchases()).filter(scope_q).select_related('product', 'product__category')

    counted_qs = base.filter(count_is_status=1)
    error_qs = base.filter(count_is_status=2)
    notcounted_qs = base.filter(count_is_status=0)

    def row(obj):
        p = obj.product
        return {
            'purchase_id': str(obj.id),
            'product_id': str(p.id) if p else None,
            'barcode': p.barcode if p and p.barcode else '',
            'rfid_code': p.rfid_code if p and hasattr(p, 'rfid_code') else '',
            'gram': str(p.gram) if p and p.gram is not None else '0.000',
            'jewelry_type': p.jewelry_type if p and p.jewelry_type else '',
            'category_name': p.category.name if p and p.category else '',
        }

    return JsonResponse({
        'bulunan': [row(o) for o in counted_qs],
        'hatalilar': [row(o) for o in error_qs],
        'eksikler': [row(o) for o in notcounted_qs],
        'scope': {
            'type': scope_type,
            'label': active_session.scope_label if active_session else '',
            'filter': scope_filter,
        },
    })


@require_http_methods(["POST"])
@login_required
def scan_barcode_for_count(request):
    """
    Bu fonksiyon hem normal barkod (örn: PR0001)
    hem de RFID (örn: 15122025...) okumalarını kabul eder.

    FAZ 2: Kapsam dışı ürünler 'out_of_scope' koduyla reddedilir.
    """
    session_id = request.POST.get('session_id')
    code = (request.POST.get('barcode', '') or '').strip()

    if not session_id or not code:
        return JsonResponse({'error': True, 'error_msg': 'Oturum ID ve veri zorunludur.'})

    session = get_object_or_404(InventoryCountSession, id=session_id, is_closed=False)

    product = Products.objects.filter(
        Q(barcode__iexact=code) | Q(rfid_code__iexact=code),
        is_deleted=False,
        store=session.store
    ).first()

    if not product:
        return JsonResponse({'error': True, 'error_msg': f'"{code}" kodlu ürün (Barkod veya RFID) bulunamadı.'})

    # Satılmış ürün guard'ı — sayım kapsamına alınmamalı, count_is_status güncellenmemeli.
    if _is_product_sold(product):
        return JsonResponse({
            'error': True,
            'sold': True,
            'error_msg': 'Bu ürün satılmış olduğu için sayıma dahil edilemez.',
            'product_id': str(product.id),
            'barcode': product.barcode or code,
        })

    # Kapsam kontrolü
    if not _product_in_scope(product, session.scope_type, session.scope_filter):
        return JsonResponse({
            'error': True,
            'out_of_scope': True,
            'error_msg': f'"{product.barcode or code}" ürünü bu sayım kapsamında değil.',
            'product_id': str(product.id),
            'barcode': product.barcode or code,
        })

    with transaction.atomic():
        item, created = InventoryCountItem.objects.get_or_create(session=session, product=product)
        item.is_counted = True
        item.save(update_fields=["is_counted"])

        gold_purchase = GoldPurchases.objects.filter(product=product, store=session.store, is_deleted=False).first()
        if gold_purchase:
            gold_purchase.count_is_status = 1
            gold_purchase.save(update_fields=["count_is_status"])

    return JsonResponse({
        'result': True,
        'message': 'Okundu',
        'product': product.name or product.jewelry_type,
        'barcode': product.barcode,
        'product_id': str(product.id)
    })


# ──────────────────────────────────────────────────────────────
# TOPLU TARAMA ENDPOINTİ (BULK SCAN) - BUFFER/BATCH OPTİMİZASYONU
# FAZ 2: Kapsam dışı ürünler ayrı response alanında döner.
# ──────────────────────────────────────────────────────────────
@require_http_methods(["POST"])
@login_required
def bulk_scan_for_count(request):
    """
    Toplu RFID/Barkod tarama endpoint'i.
    Frontend'den JSON array olarak gelen kodları tek seferde işler.
    Giriş: {"session_id": "uuid", "codes": ["EPC001", "EPC002", ...]}
    Çıkış:
    {
      "result": True,
      "found": [...],             # yeni sayılan (kapsam içi)
      "already_counted": [...],   # daha önce sayılmış (kapsam içi)
      "out_of_scope": [...],      # sistemdeki ama kapsam dışı ürünler (FAZ 2)
      "errors": [...],            # sistemde olmayan kodlar
      "totals": {...}             # kapsam içi toplamlar
    }
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz JSON formatı.'}, status=400)

    session_id = body.get('session_id', '')
    codes = body.get('codes', [])

    if not session_id:
        return JsonResponse({'error': True, 'error_msg': 'Oturum ID zorunludur.'})

    if not codes or not isinstance(codes, list):
        return JsonResponse({'error': True, 'error_msg': 'Kod listesi boş veya geçersiz.'})

    # Kodları temizle (boşluk, satır sonu vb.)
    clean_codes = []
    for c in codes:
        cleaned = str(c).strip().replace('\n', '').replace('\r', '').replace(' ', '')
        if cleaned:
            clean_codes.append(cleaned)

    if not clean_codes:
        return JsonResponse({'error': True, 'error_msg': 'Geçerli kod bulunamadı.'})

    # Oturum doğrulama
    try:
        session = InventoryCountSession.objects.get(id=session_id, is_closed=False)
    except InventoryCountSession.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Geçerli bir açık sayım oturumu bulunamadı.'})

    store = session.store
    scope_type = session.scope_type
    scope_filter = session.scope_filter or {}

    # ─── 1. TEK SORGU İLE TÜM ÜRÜNLERİ BUL ───
    lower_codes = [c.lower() for c in clean_codes]

    products = Products.objects.annotate(
        _barcode_lower=Lower('barcode'),
        _rfid_lower=Lower('rfid_code')
    ).filter(
        Q(_barcode_lower__in=lower_codes) | Q(_rfid_lower__in=lower_codes),
        is_deleted=False,
        store=store
    ).select_related('category')

    # ─── 2. LOOKUP DİCT OLUŞTUR ───
    barcode_to_product = {}
    rfid_to_product = {}
    for p in products:
        if p.barcode:
            barcode_to_product[p.barcode.lower()] = p
        if p.rfid_code:
            rfid_to_product[p.rfid_code.lower()] = p

    # ─── 3. KODLARI AYIR: SATILMIŞ / KAPSAM-İÇİ BULUNAN / KAPSAM-DIŞI / HATA ───
    found_products = {}        # kapsam içi — sayıma dahil olacak
    out_of_scope_products = {} # sistemdeki ama kapsam dışı
    sold_products = {}         # satılmış ürünler — sayıma alınmaz
    error_codes = []           # hiç bulunamayan

    # Satılmış ürünleri tek sorguda tespit et (her bir tarama için DB sorgusu yapmamak için)
    _candidate_ids = [p.id for p in products]
    _sold_via_completed = set()
    _sold_via_gp_status = set()
    if _candidate_ids:
        _sold_via_completed = set(
            Products.objects.filter(
                id__in=_candidate_ids, is_completed=True
            ).values_list('id', flat=True)
        )
        # GoldPurchases kaydı olup hiçbiri is_status=True olmayan ürünler de satılmış sayılır
        _gp_has_any = set(
            GoldPurchases.objects.filter(
                product_id__in=_candidate_ids, is_deleted=False
            ).values_list('product_id', flat=True)
        )
        _gp_has_active = set(
            GoldPurchases.objects.filter(
                product_id__in=_candidate_ids, is_deleted=False, is_status=True
            ).values_list('product_id', flat=True)
        )
        _sold_via_gp_status = _gp_has_any - _gp_has_active

    _sold_ids = _sold_via_completed | _sold_via_gp_status

    for code in clean_codes:
        code_lower = code.lower()
        product = barcode_to_product.get(code_lower) or rfid_to_product.get(code_lower)
        if not product:
            error_codes.append(code)
            continue

        if product.id in _sold_ids:
            sold_products[str(product.id)] = product
            continue

        if _product_in_scope(product, scope_type, scope_filter):
            found_products[str(product.id)] = product
        else:
            out_of_scope_products[str(product.id)] = product

    found_product_list = list(found_products.values())
    out_of_scope_list = list(out_of_scope_products.values())
    sold_list = list(sold_products.values())

    # ─── 4. MEVCUT SAYIM KAYITLARINI KONTROL ET ───
    existing_item_product_ids = set(
        InventoryCountItem.objects.filter(
            session=session,
            product_id__in=[p.id for p in found_product_list]
        ).values_list('product_id', flat=True).distinct()
    )
    existing_item_product_ids = {str(pid) for pid in existing_item_product_ids}

    new_products = [p for p in found_product_list if str(p.id) not in existing_item_product_ids]
    already_counted_products = [p for p in found_product_list if str(p.id) in existing_item_product_ids]

    # ─── 5. TOPLU VERİTABANI İŞLEMLERİ ───
    with transaction.atomic():
        if new_products:
            InventoryCountItem.objects.bulk_create([
                InventoryCountItem(
                    session=session,
                    product=p,
                    is_counted=True
                ) for p in new_products
            ])

        if found_product_list:
            GoldPurchases.objects.filter(
                product__in=found_product_list,
                store=store,
                is_deleted=False
            ).update(count_is_status=1)

    # ─── 6. RESPONSE VERİLERİNİ HAZIRLA ───
    def product_row(p):
        return {
            'product_id': str(p.id),
            'barcode': p.barcode or '',
            'rfid_code': p.rfid_code if hasattr(p, 'rfid_code') and p.rfid_code else '',
            'gram': str(p.gram) if p.gram is not None else '0.000',
            'jewelry_type': p.jewelry_type or p.name or '',
            'category_name': p.category.name if p.category else '',
        }

    found_rows = [product_row(p) for p in new_products]
    already_rows = [product_row(p) for p in already_counted_products]
    # Satılmış ürünler hatalı/yabancı listesine "Satılmış ürün sayıma dahil edilemez." mesajıyla düşer.
    error_rows = [{'barcode': code, 'message': 'Tanımsız / Yabancı Kod'} for code in error_codes]
    for p in sold_list:
        error_rows.append({
            'barcode': p.barcode or (p.rfid_code or ''),
            'message': 'Satılmış ürün sayıma dahil edilemez.',
        })
    out_of_scope_rows = [{
        **product_row(p),
        'message': 'Kapsam Dışı',
    } for p in out_of_scope_list]

    # ─── 7. KAPSAM-İÇİ GÜNCEL TOPLAMLARI HESAPLA ───
    scope_q = _build_scope_q_for_gold_purchases(scope_type, scope_filter)
    base_qs = GoldPurchases.objects.filter(
        product__store=store,
        is_deleted=False,
        product__is_deleted=False,
        product__is_active=True,
    ).exclude(_sold_q_for_gold_purchases()).filter(scope_q)

    totals = base_qs.aggregate(
        found=Count('id', filter=Q(count_is_status=1)),
        errors=Count('id', filter=Q(count_is_status=2)),
        missing=Count('id', filter=Q(count_is_status=0)),
    )

    return JsonResponse({
        'result': True,
        'found': found_rows,
        'already_counted': already_rows,
        'out_of_scope': out_of_scope_rows,
        'errors': error_rows,
        'totals': {
            'found': totals['found'] or 0,
            'errors': totals['errors'] or 0,
            'missing': totals['missing'] or 0,
        },
        'batch_summary': {
            'total_sent': len(clean_codes),
            'new_found': len(new_products),
            'already_counted': len(already_counted_products),
            'out_of_scope': len(out_of_scope_list),
            'sold': len(sold_list),
            'not_found': len(error_codes),
        }
    })


# ──────────────────────────────────────────────
# Görev 1: Ürün Görseli Önizleme (AJAX Endpoint)
# ──────────────────────────────────────────────
@require_http_methods(["GET"])
@login_required
def get_product_image(request):
    product_id = request.GET.get('product_id')
    if not product_id:
        return JsonResponse({'error': True, 'error_msg': 'Ürün ID gerekli.'})

    try:
        product = Products.objects.select_related('category').get(id=product_id)
    except Products.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Ürün bulunamadı.'})

    image_url = ''
    if product.image and hasattr(product.image, 'url'):
        try:
            image_url = product.image.url
        except ValueError:
            image_url = ''

    return JsonResponse({
        'result': True,
        'image_url': image_url,
        'barcode': product.barcode or '-',
        'name': product.name or product.jewelry_type or '-',
        'gram': str(product.gram) if product.gram else '0.000',
        'category': product.category.name if product.category else '-',
    })


# ──────────────────────────────────────────────
# FAZ 2: Kapsam seçenekleri endpoint'i
# ──────────────────────────────────────────────
@require_http_methods(["GET"])
@login_required
def scope_options(request):
    """
    Kapsam seçim modal'ı için mevcut mağazadaki ürünlerde bulunan
    kategorileri, materyal tiplerini, markaları ve mücevher tiplerini döner.
    Seçenek listesi, kullanıcının mağazasındaki aktif ve silinmemiş ürünlere
    göre dinamik olarak üretilir (boş seçenekler kullanıcıya gösterilmez).
    """
    user_store = request.user.store
    if not user_store:
        return JsonResponse({'error': True, 'error_msg': 'Kullanıcının mağazası tanımlı değil.'})

    base_products = Products.objects.filter(
        store=user_store,
        is_deleted=False,
        is_active=True,
    )

    # Kategoriler (kullanılıyor olanlar)
    used_category_ids = list(base_products.exclude(category__isnull=True).values_list(
        'category_id', flat=True
    ).distinct())

    # Kategori bazında ürün sayısı — UUID anahtarlarını string'e çevir
    cat_count_qs = (
        base_products.exclude(category__isnull=True)
        .values('category_id')
        .annotate(c=Count('id'))
    )
    cat_counts = {str(row['category_id']): row['c'] for row in cat_count_qs}

    raw_categories = Categories.objects.filter(
        id__in=used_category_ids,
        is_deleted=False,
    ).order_by('order', 'name').values('id', 'name')

    categories = []
    for c in raw_categories:
        sid = str(c['id'])
        categories.append({
            'id': sid,
            'name': c['name'] or '-',
            'count': cat_counts.get(sid, 0),
        })

    # Materyal tipleri
    used_materials = base_products.exclude(material_type__isnull=True).exclude(
        material_type=''
    ).values_list('material_type', flat=True).distinct()
    material_choices_map = {v: l for v, l in MaterialType.choices}
    materials = []
    for mt in used_materials:
        materials.append({
            'value': mt,
            'label': material_choices_map.get(mt, mt),
            'count': base_products.filter(material_type=mt).count(),
        })

    # Markalar
    used_brands = list(
        base_products.exclude(brand__isnull=True).exclude(brand='').values_list('brand', flat=True).distinct()
    )
    used_brands.sort(key=lambda s: (s or '').lower())
    brands = []
    for b in used_brands:
        brands.append({
            'value': b,
            'label': b,
            'count': base_products.filter(brand=b).count(),
        })

    # Mücevher tipleri
    used_jtypes = list(
        base_products.exclude(jewelry_type__isnull=True).exclude(jewelry_type='').values_list(
            'jewelry_type', flat=True
        ).distinct()
    )
    used_jtypes.sort(key=lambda s: (s or '').lower())
    jewelry_types = []
    for j in used_jtypes:
        jewelry_types.append({
            'value': j,
            'label': j,
            'count': base_products.filter(jewelry_type=j).count(),
        })

    return JsonResponse({
        'result': True,
        'total_products': base_products.count(),
        'categories': categories,
        'materials': materials,
        'brands': brands,
        'jewelry_types': jewelry_types,
    })


# ──────────────────────────────────────────────
# Yardımcı: Kategori bazlı gruplama
# ──────────────────────────────────────────────
def _group_rows_by_category(rows):
    """
    row listesini 'category' anahtarına göre gruplar.
    Dönüş: [{'category': 'Bilezik', 'count': 3, 'items': [...]}, ...]
    """
    groups = defaultdict(list)
    for r in rows:
        cat = r.get('category') or 'Kategori Belirtilmemiş'
        groups[cat].append(r)

    result = []
    for cat_name in sorted(groups.keys()):
        result.append({
            'category': cat_name,
            'count': len(groups[cat_name]),
            'items': groups[cat_name],
        })
    return result


# ──────────────────────────────────────────────
# Görev 2 + 3: Rapor Önizleme (HTML JSON Endpoint)
# FAZ 2: Kapsam filtreli özet ve listeler.
# ──────────────────────────────────────────────
@login_required
def preview_report(request, session_id):
    """
    Rapor verisini JSON olarak döner (modal önizleme için).
    Kapsam varsa yalnızca kapsama ait ürünler raporlanır.
    """
    session = get_object_or_404(InventoryCountSession, id=session_id)
    if request.user.store and session.store and request.user.store != session.store:
        return JsonResponse({'error': True, 'error_msg': 'Yetkisiz erişim.'}, status=403)

    store = session.store
    scope_q = _build_scope_q_for_gold_purchases(session.scope_type, session.scope_filter)

    base_qs = GoldPurchases.objects.filter(
        product__store=store,
        is_deleted=False,
        product__is_deleted=False,
        product__is_active=True,
    ).exclude(_sold_q_for_gold_purchases()).filter(scope_q).select_related("product", "product__category")

    DEC_FIELD = DecimalField(max_digits=18, decimal_places=3)
    ZERO = Value(Decimal("0.000"), output_field=DEC_FIELD)

    summary = base_qs.aggregate(
        total_qty=Count('id'),
        found_qty=Count('id', filter=Q(count_is_status=1)),
        missing_qty=Count('id', filter=Q(count_is_status=0)),
        error_qty=Count('id', filter=Q(count_is_status=2)),

        total_gram=Coalesce(Sum('product__gram', output_field=DEC_FIELD), ZERO),
        found_gram=Coalesce(Sum('product__gram', filter=Q(count_is_status=1), output_field=DEC_FIELD), ZERO),
        missing_gram=Coalesce(Sum('product__gram', filter=Q(count_is_status=0), output_field=DEC_FIELD), ZERO),

        total_has=Coalesce(Sum('product__buy_price_hs', output_field=DEC_FIELD), ZERO),
        found_has=Coalesce(Sum('product__buy_price_hs', filter=Q(count_is_status=1), output_field=DEC_FIELD), ZERO),
        missing_has=Coalesce(Sum('product__buy_price_hs', filter=Q(count_is_status=0), output_field=DEC_FIELD), ZERO),
    )

    all_items = base_qs.order_by('product__category__name', 'product__barcode')

    found_rows = []
    missing_rows = []
    error_rows = []

    for item in all_items:
        row = {
            'barcode': item.product.barcode or '-',
            'name': item.product.jewelry_type or item.product.name or '-',
            'gram': str(item.product.gram) if item.product.gram else '0.000',
            'has': str(item.product.buy_price_hs) if item.product.buy_price_hs else '0.000',
            'category': item.product.category.name if item.product.category else '',
        }

        if item.count_is_status == 1:
            found_rows.append(row)
        elif item.count_is_status == 2:
            error_rows.append(row)
        else:
            missing_rows.append(row)

    summary_json = {}
    for k, v in summary.items():
        summary_json[k] = str(v) if isinstance(v, Decimal) else v

    return JsonResponse({
        'result': True,
        'summary': summary_json,
        'missing_groups': _group_rows_by_category(missing_rows),
        'error_groups': _group_rows_by_category(error_rows),
        'found_groups': _group_rows_by_category(found_rows),
        'report_date': timezone.localtime(timezone.now()).strftime("%d.%m.%Y %H:%M"),
        'company_name': store.title or "Kuyum Plus",
        'scope_type': session.scope_type,
        'scope_label': session.scope_label or '',
    })


@login_required
def download_inventory_pdf(request, session_id):
    session = get_object_or_404(InventoryCountSession, id=session_id)
    if request.user.store and session.store and request.user.store != session.store:
        return JsonResponse({'error': True, 'error_msg': 'Yetkisiz erişim.'}, status=403)

    store = session.store
    scope_q = _build_scope_q_for_gold_purchases(session.scope_type, session.scope_filter)

    base_qs = GoldPurchases.objects.filter(
        product__store=store,
        is_deleted=False,
        product__is_deleted=False,
        product__is_active=True,
    ).exclude(_sold_q_for_gold_purchases()).filter(scope_q).select_related("product", "product__category")

    DEC_FIELD = DecimalField(max_digits=18, decimal_places=3)
    ZERO = Value(Decimal("0.000"), output_field=DEC_FIELD)

    summary = base_qs.aggregate(
        total_qty=Count('id'),
        found_qty=Count('id', filter=Q(count_is_status=1)),
        missing_qty=Count('id', filter=Q(count_is_status=0)),
        error_qty=Count('id', filter=Q(count_is_status=2)),

        total_gram=Coalesce(Sum('product__gram', output_field=DEC_FIELD), ZERO),
        found_gram=Coalesce(Sum('product__gram', filter=Q(count_is_status=1), output_field=DEC_FIELD), ZERO),
        missing_gram=Coalesce(Sum('product__gram', filter=Q(count_is_status=0), output_field=DEC_FIELD), ZERO),

        total_has=Coalesce(Sum('product__buy_price_hs', output_field=DEC_FIELD), ZERO),
        found_has=Coalesce(Sum('product__buy_price_hs', filter=Q(count_is_status=1), output_field=DEC_FIELD), ZERO),
        missing_has=Coalesce(Sum('product__buy_price_hs', filter=Q(count_is_status=0), output_field=DEC_FIELD), ZERO),
    )

    all_items = base_qs.order_by('product__category__name', 'product__barcode')

    found_rows = []
    missing_rows = []
    error_rows = []

    for item in all_items:
        row = {
            'barcode': item.product.barcode or '-',
            'name': item.product.jewelry_type or item.product.name or '-',
            'gram': item.product.gram,
            'has': item.product.buy_price_hs,
            'category': item.product.category.name if item.product.category else 'Kategori Belirtilmemiş',
        }

        if item.count_is_status == 1:
            found_rows.append(row)
        elif item.count_is_status == 2:
            error_rows.append(row)
        else:
            missing_rows.append(row)

    context = {
        'company_name': store.title or "Kuyum Plus",
        'report_date': timezone.localtime(timezone.now()).strftime("%d.%m.%Y %H:%M"),
        'session_info': f"Başlangıç: {timezone.localtime(session.start_time).strftime('%d.%m.%Y %H:%M')}",
        'report_no': f"CNT-{session.id}",
        'authorized': request.user.get_full_name() or request.user.username,
        'store_id': store.store_id or str(store.id),
        'summary': summary,
        'missing_groups': _group_rows_by_category(missing_rows),
        'error_groups': _group_rows_by_category(error_rows),
        'found_groups': _group_rows_by_category(found_rows),
        'missing_rows': missing_rows,
        'error_rows': error_rows,
        'found_rows': found_rows,
        'scope_type': session.scope_type,
        'scope_label': session.scope_label or '',
    }

    html_string = render_to_string('management/counts/counts_report.html', context)
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="SayimRaporu_{session.id}.pdf"'

    pisa_status = pisa.CreatePDF(html_string, dest=response, encoding='utf-8')

    if pisa_status.err:
        return HttpResponse('PDF hatası oluştu.', status=500)

    return response


@require_http_methods(["POST"])
@login_required
def reset_session(request):
    """
    FAZ 2: Kapsam bilinçli sıfırlama.
    Açık oturum varsa yalnızca o oturumun kapsamına ait ürünlerin
    count_is_status'u sıfırlanır; kapsam dışı ürünlere dokunulmaz.
    Açık oturum yoksa mağazadaki tüm ürünler sıfırlanır (eski davranış).
    """
    user_store = request.user.store
    if not user_store:
        return JsonResponse({'error': True, 'error_msg': 'Kullanıcının mağazası tanımlı değil.'})

    session = InventoryCountSession.objects.filter(store=user_store, is_closed=False).first()
    if session:
        scope_q = _build_scope_q_for_gold_purchases(session.scope_type, session.scope_filter)
        session.is_closed = True
        session.end_time = timezone.now()
        session.save()
    else:
        scope_q = Q()  # tüm mağaza

    GoldPurchases.objects.filter(
        product__store=user_store,
        is_deleted=False,
        product__is_deleted=False,
        product__is_active=True,
    ).exclude(_sold_q_for_gold_purchases()).filter(scope_q).update(count_is_status=0)

    return JsonResponse({
        'result': True,
        'message': 'Sıfırlama tamamlandı. Oturum kapatıldı.'
    })
