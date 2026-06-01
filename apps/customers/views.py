import json
import random
import re
import string
import secrets
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404

from apps.accounts.models import *
from apps.activity_logs.views import write_log
from apps.customers.models import Customers, CustomerLedger
from apps.customers.validators import validate_identification_number
from apps.process.models import Process, Payment
from apps.roles.decorators import role_required
from apps.settings.models import StoreConfiguration
from apps.settings.send_mail import *
from apps.whatsapp.services import wa_preflight, send_whatsapp_template_guarded
from apps.products.models import Products
from django.db.models import OuterRef, Subquery, Sum, DecimalField, F, Q
from django.db.models.functions import Coalesce
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction  # İşlem bütünlüğü için gerekli
# --- FAZ 4: StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
from django.db.models import Case, When
from django.db.models.functions import Coalesce
# --- FAZ 14 UI: Bakiye SSOT (CustomerLedger Subquery) + anlık has kuru ---
from apps.banking.exchange_rate_service import get_current_has_rate
from apps.banking.models import BankAccount, CashboxLedger
from apps.customers.services.ledger import LedgerService
from apps.customers.services.audit import extract_audit_context
from django.core.exceptions import ValidationError
from django.utils import timezone


# ════════════════════════════════════════════════════════════════════════════
# FAZ 14 UI — Bakiye SSOT: balance_hs için Subquery yardımcısı
# ════════════════════════════════════════════════════════════════════════════
#
# `Customers.balance_hs` property `customer.ledger_entries` üzerinde aggregate
# yapar; queryset üzerinde N+1 oluşturur. Liste API'leri (get_all,
# get_customers) için tek SQL içinde annotate edebileceğimiz bir Subquery
# tanımlıyoruz. Hesaplama formülü `Customers._ledger_aggregate` ile birebir
# aynı:
#
#     balance_hs = Σ(DEBT_INCREASING)         - Σ(REVERSAL of DEBT_INCREASING)
#                - Σ(DEBT_DECREASING)         + Σ(REVERSAL of DEBT_DECREASING)
#                + Σ(CORRECTION amount_hs_signed)
#
# Sadece `is_active=True` ve `is_approved=True` kayıtlar dahil edilir.
def _balance_hs_subquery():
    """Customers queryset'i üzerinde balance_hs annotate'i için Subquery."""
    base = CustomerLedger.objects.filter(
        customer=OuterRef('pk'),
        is_active=True,
        is_approved=True,
    )
    return Subquery(
        base.values('customer').annotate(
            balance=(
                Coalesce(
                    Sum(
                        'amount_hs',
                        filter=Q(transaction_type__in=Customers.DEBT_INCREASING_TYPES),
                    ),
                    Decimal('0'),
                )
                - Coalesce(
                    Sum(
                        'amount_hs',
                        filter=Q(
                            transaction_type='REVERSAL',
                            reversal_target_type__in=Customers.DEBT_INCREASING_TYPES,
                        ),
                    ),
                    Decimal('0'),
                )
                - Coalesce(
                    Sum(
                        'amount_hs',
                        filter=Q(transaction_type__in=Customers.DEBT_DECREASING_TYPES),
                    ),
                    Decimal('0'),
                )
                + Coalesce(
                    Sum(
                        'amount_hs',
                        filter=Q(
                            transaction_type='REVERSAL',
                            reversal_target_type__in=Customers.DEBT_DECREASING_TYPES,
                        ),
                    ),
                    Decimal('0'),
                )
                + Coalesce(
                    Sum(
                        'amount_hs_signed',
                        filter=Q(transaction_type='CORRECTION'),
                    ),
                    Decimal('0'),
                )
            ),
        ).values('balance')[:1],
        output_field=DecimalField(max_digits=18, decimal_places=3),
    )


# ════════════════════════════════════════════════════════════════════════════
# FAZ 33.3 — STORED TL Bakiye Subquery'si
# ════════════════════════════════════════════════════════════════════════════
#
# Liste endpoint'leri (get_customers, get_all) müşteri başına balance_eur
# döndürüyor. Eski formül `balance_hs × get_current_has_rate(store)` (anlık
# ALIŞ kuru) → satış SATIŞ kuruyla yapıldıysa spread farkı görünüyordu.
#
# Yeni formül: Σ signed amount_eur. Her ledger satırı yazıldığı kuruyla
# `amount_eur` alanını saklar; bu Subquery onları toplar. Kur dalgalanması
# liste TL'sine yansımaz.
#
# CORRECTION tipi için sign Case/When ile çıkarılır
# (amount_hs_signed > 0 → +amount_eur, < 0 → -amount_eur).
# Customers.balance_eur property ile birebir aynı sonucu verir.
def _balance_eur_subquery():
    """Customers queryset'i üzerinde balance_eur annotate'i için Subquery."""
    from django.db.models import Case, When, F
    base = CustomerLedger.objects.filter(
        customer=OuterRef('pk'),
        is_active=True,
        is_approved=True,
    )
    _tl_field = DecimalField(max_digits=14, decimal_places=2)
    return Subquery(
        base.values('customer').annotate(
            balance=(
                Coalesce(
                    Sum(
                        'amount_eur',
                        filter=Q(transaction_type__in=Customers.DEBT_INCREASING_TYPES),
                    ),
                    Decimal('0'),
                    output_field=_tl_field,
                )
                - Coalesce(
                    Sum(
                        'amount_eur',
                        filter=Q(
                            transaction_type='REVERSAL',
                            reversal_target_type__in=Customers.DEBT_INCREASING_TYPES,
                        ),
                    ),
                    Decimal('0'),
                    output_field=_tl_field,
                )
                - Coalesce(
                    Sum(
                        'amount_eur',
                        filter=Q(transaction_type__in=Customers.DEBT_DECREASING_TYPES),
                    ),
                    Decimal('0'),
                    output_field=_tl_field,
                )
                + Coalesce(
                    Sum(
                        'amount_eur',
                        filter=Q(
                            transaction_type='REVERSAL',
                            reversal_target_type__in=Customers.DEBT_DECREASING_TYPES,
                        ),
                    ),
                    Decimal('0'),
                    output_field=_tl_field,
                )
                + Coalesce(
                    Sum(
                        Case(
                            When(
                                transaction_type='CORRECTION',
                                amount_hs_signed__gt=0,
                                then=F('amount_eur'),
                            ),
                            When(
                                transaction_type='CORRECTION',
                                amount_hs_signed__lt=0,
                                then=-F('amount_eur'),
                            ),
                            default=Decimal('0'),
                            output_field=_tl_field,
                        ),
                    ),
                    Decimal('0'),
                    output_field=_tl_field,
                )
            ),
        ).values('balance')[:1],
        output_field=_tl_field,
    )


def normalize_tr_phone(phone_raw: str) -> str:
    """
    Telefon numarasını temizler ve 10 haneli standart formata getirir (Başında 0 yok).
    Örnek Girdiler: 0535 711 6458, 535-711-6458, +90 535...
    Çıktı: 5357116458
    """
    if not phone_raw:
        return ""

    # Sadece rakamları al
    digits = re.sub(r"\D", "", str(phone_raw))

    # Eğer 90 ile başlıyorsa ve uzunsa (örn 90535...) başındaki 90'ı at
    if digits.startswith("90") and len(digits) > 10:
        digits = digits[2:]

    # Başındaki 0'ı at (0535 -> 535)
    if digits.startswith("0"):
        digits = digits.lstrip("0")

    # Sonuç 10 hane olmalı
    if len(digits) == 10:
        return digits

    # Eğer standart dışıysa olduğu gibi veya boş döndür (Validasyon ayrıca yapılmalı)
    return digits  # veya phone_raw


def normalize_intl_phone(phone_raw: str) -> str:
    """
    Uluslararası telefon numarasını E.164 uyumlu sade biçime getirir.
    Boşluk, tire, parantez temizlenir; tek bir baştaki '+' korunur; harf reddedilir.
    Örnek: '+49 152 12345678' -> '+4915212345678', '0152 12345678' -> '015212345678'
    """
    if not phone_raw:
        return ""

    cleaned = re.sub(r"[\s\-().]", "", str(phone_raw))

    if cleaned.startswith("+"):
        return "+" + re.sub(r"\D", "", cleaned[1:])
    return re.sub(r"\D", "", cleaned)


@login_required()
@role_required('CUSTOMERS_CUSTOMERS_VIEW')
def customers_view(request):
    # FAZ 14 UI — B1: Has kuru artık Products tablosu yerine
    # exchange_rate_service.get_current_has_rate(store)'tan okunuyor.
    # Products.sale_price_eur manuel ürün satış fiyatıydı; gerçek anlık
    # kur (StoreConfig.price_cache + PriceQuote fallback) bu serviste
    # SSOT olarak tutuluyor.
    store = request.user.store
    has_rate = get_current_has_rate(store) or Decimal('0')

    context = {
        'title': 'Müşteriler',
        'has_price': float(has_rate),  # Şablon eski adı bekliyor; güvenli
        'has_rate': float(has_rate),   # Yeni semantik isim
        'store_masak_token': getattr(store, 'masak_public_token', ''),
    }
    write_log(request, 'Müşteriler', 'Müşteriler Görüntülendi.')
    return render(request, 'management/customers/index.html', context)


@login_required()
def add_customer(request):
    context = {
        'title': 'Müşteri Ekle',
    }
    store = request.user.store

    if request.method == 'POST':
        def _none_if_blank(v):
            v = (v or "").strip()
            return v or None

        record_id = request.POST.get('record_id')
        raw_phone = request.POST.get('phone')
        phone = None
        if raw_phone and raw_phone.strip():
            if len(raw_phone) > 30:
                return JsonResponse({'error': True, 'error_msg': 'Geçersiz telefon numarası. Lütfen ülke koduyla birlikte geçerli bir numara girin.'})
            phone = normalize_intl_phone(raw_phone)
            if not re.fullmatch(r"\+?\d{7,15}", phone):
                return JsonResponse({'error': True, 'error_msg': 'Geçersiz telefon numarası. Lütfen ülke koduyla birlikte geçerli bir numara girin.'})

        identification_number = _none_if_blank(request.POST.get('identification_number'))

        # --- DİNAMİK ZORUNLULUK + KİMLİK ALGORİTMA KONTROLLERİ ---
        # Lazy Enforcement: hem yeni kayıt hem güncelleme anında çalışır,
        # geçmişteki eksik veriyi geriye dönük tetiklemez.
        store_config, _ = StoreConfiguration.objects.get_or_create(store=store)

        if store_config.require_customer_phone and not phone:
            return JsonResponse({
                'error': True,
                'error_msg': 'Bu mağazada telefon numarası zorunludur.'
            })

        # Kimlik numarası girildiyse algoritma + format kontrolü:
        # 11 hane → TCKN matematiksel doğrulama
        # 10 hane → VKN format kontrolü (kurumsal müşteri)
        # diğer  → reddedilir
        if identification_number:
            store_country = (getattr(store, 'country', '') or '').strip().lower()
            if store_country in ('türkiye', 'turkiye', 'turkey', 'tr', ''):
                is_valid, err_msg = validate_identification_number(identification_number)
                if not is_valid:
                    return JsonResponse({'error': True, 'error_msg': err_msg})

        if record_id:
            record = get_object_or_404(Customers, id=record_id, store=store, is_deleted=False)
        else:
            record = Customers()

        front_uploaded = bool(request.FILES.get('identification_front_image'))
        back_uploaded = bool(request.FILES.get('identification_back_image'))
        any_uploaded = front_uploaded or back_uploaded

        if any_uploaded:
            if not identification_number and not (record.identification_number or ''):
                return JsonResponse({
                    'error': True,
                    'error_msg': 'Kimlik fotoğrafı yüklendiğinde Kimlik Numarası zorunludur.'
                })

            if front_uploaded and not back_uploaded and not record.identification_back_image:
                return JsonResponse({
                    'error': True,
                    'error_msg': 'Kimlik fotoğrafı ekleyecekseniz ön ve arka yüzü birlikte yüklemelisiniz.'
                })

            if back_uploaded and not front_uploaded and not record.identification_front_image:
                return JsonResponse({
                    'error': True,
                    'error_msg': 'Kimlik fotoğrafı ekleyecekseniz ön ve arka yüzü birlikte yüklemelisiniz.'
                })

        duplicate_exists = False
        if phone:
            qs = Customers.objects.filter(
                store=store,
                phone=phone,  # Formatlanmış numara ile ara
                is_deleted=False
            )
            if record_id:
                qs = qs.exclude(id=record_id)
            duplicate_exists = qs.exists()

        if duplicate_exists:
            return JsonResponse({
                'error': True,
                'error_msg': 'Bu mağazada bu telefon numarasıyla kayıtlı bir müşteri zaten var.'
            })

        if identification_number:
            qs_tc = Customers.objects.filter(
                store=store,
                identification_number=identification_number,
                is_deleted=False
            )
            if record_id:
                qs_tc = qs_tc.exclude(id=record_id)
            if qs_tc.exists():
                return JsonResponse({
                    'error': True,
                    'error_msg': 'Bu mağazada bu T.C. Kimlik Numarasıyla kayıtlı bir müşteri zaten var.'
                })

        record.first_name = request.POST.get('first_name')
        record.last_name = request.POST.get('last_name')
        record.identification_number = identification_number
        record.phone = phone
        record.gender = request.POST.get('gender')
        record.email = _none_if_blank(request.POST.get('email'))
        record.address = _none_if_blank(request.POST.get('address'))

        city_id = _none_if_blank(request.POST.get('city'))
        district_id = _none_if_blank(request.POST.get('district'))
        tax_office_id = _none_if_blank(request.POST.get('tax_office'))
        tax_office_code = _none_if_blank(request.POST.get('tax_office_code'))

        if city_id:
            record.city_id = city_id
        if district_id:
            record.district_id = district_id
        if tax_office_id:
            record.tax_office_id = tax_office_id

        record.tax_office_code = tax_office_code

        if front_uploaded:
            record.identification_front_image = request.FILES.get('identification_front_image')

        if back_uploaded:
            record.identification_back_image = request.FILES.get('identification_back_image')

        if not record_id and not record.customer_number:
            record.customer_number = get_customer_code()

        try:
            record.save()
            record.store.add(store)
            write_log(request, 'Müşteriler', 'Müşteri Eklendi. ID= ' + str(record.id).upper())
            return JsonResponse({'result': True, 'customer_id': record.id})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})

    return render(request, 'management/customers/index.html', context)


def get_customer_code():
    while True:
        new_customer_number = ''.join(secrets.choice('0123456789') for _ in range(6))
        if not Customers.objects.filter(customer_number=new_customer_number).exists():
            return new_customer_number


@login_required(login_url='login')
def get_customers(request):
    # FAZ 14 UI — A1: Statik receivable_hs/payable_hs alanları artık
    # CollectionService tarafından mutate edilmiyor. Bakiye, CustomerLedger
    # üzerinden Subquery ile hesaplanıyor.
    #
    # NOT: Annotation adı `balance_hs_db`. `balance_hs` adı `Customers`
    # modelinde @property (setter'sız data descriptor); Django ORM'in
    # annotation hidrasyonu setattr() çağırdığında AttributeError fırlatır.
    # Loop içinde de `c.balance_hs_db` kullanılmalı; aksi halde property
    # tetiklenir ve müşteri başına 1 ek SQL = N+1 oluşur.
    store = request.user.store
    customers = (
        Customers.objects
        .filter(is_deleted=False, is_active=True, store=store)
        .annotate(
            balance_hs_db=Coalesce(_balance_hs_subquery(), Decimal('0')),
            # FAZ 33.3 — STORED TL bakiye (Σ signed amount_eur)
            balance_eur_db=Coalesce(
                _balance_eur_subquery(),
                Decimal('0'),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
    )

    has_rate = get_current_has_rate(store) or Decimal('0')

    rows = []
    for c in customers:
        bal_hs = Decimal(c.balance_hs_db or 0)
        # FAZ 33.3 — annotated stored TL (anlık piyasa kuru kullanılmaz)
        bal_tl = Decimal(c.balance_eur_db or 0).quantize(Decimal('0.01'))
        rows.append({
            'id': str(c.id),
            'first_name': c.first_name,
            'last_name': c.last_name,
            'phone': c.phone,
            'identification_number': c.identification_number,
            # Geriye uyumluluk: eski statik alan adlarını bakiye-türevi olarak
            # döndürüyoruz; yeni client'lar `balance_hs / balance_eur`
            # kullanmalı, eski client'lar receivable/payable ile çalışmaya
            # devam etsin.
            'receivable_hs': str(bal_hs if bal_hs > 0 else Decimal('0')),
            'payable_hs': str((-bal_hs) if bal_hs < 0 else Decimal('0')),
            'balance_hs': str(bal_hs),
            'balance_eur': str(bal_tl),
        })
    return JsonResponse(rows, safe=False)


@login_required(login_url='login')
def get_all(request):
    draw = int(request.GET['draw'])
    length = int(request.GET['length'])
    start = int(request.GET['start'])
    search_value = request.GET['search[value]']
    order_column = request.GET['columns[' + request.GET['order[0][column]'] + '][data]']
    order = request.GET['order[0][dir]']

    filter_type = request.GET.get('filter_type', 'all')
    masak_filter = request.GET.get('masak_filter', '')  # '', 'filled', 'missing'

    if order_column is None:
        order_column = "created_on"

    if order == 'desc':
        order_column = '-' + order_column

    user_store = request.user.store

    # FAZ 14 UI — A1: Bakiye annotate'i ile SSOT (CustomerLedger).
    #
    # NOT: Annotation adı `balance_hs_db`. `balance_hs` adı `Customers`
    # modelinde @property (setter'sız data descriptor); Django ORM'in
    # annotation hidrasyonu setattr() çağırdığında AttributeError fırlatır.
    # Loop içinde de `c.balance_hs_db` kullanılmalı; aksi halde property
    # tetiklenir ve müşteri başına 1 ek SQL = N+1 oluşur.
    queryset = (
        Customers.objects
        .filter(is_deleted=False, store=user_store)
        .select_related('masak_declaration')
        .annotate(
            balance_hs_db=Coalesce(_balance_hs_subquery(), Decimal('0')),
            # FAZ 33.3 — STORED TL bakiye (Σ signed amount_eur)
            balance_eur_db=Coalesce(
                _balance_eur_subquery(),
                Decimal('0'),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
    )

    # Filter — annotate edilmiş `balance_hs_db` üzerinden:
    #   debtors  → balance_hs_db > 0  (müşteri mağazaya borçlu)
    #   creditors→ balance_hs_db < 0  (mağaza müşteriye borçlu)
    if filter_type == 'debtors':
        queryset = queryset.filter(balance_hs_db__gt=Decimal('0'))
    elif filter_type == 'creditors':
        queryset = queryset.filter(balance_hs_db__lt=Decimal('0'))

    if masak_filter == 'filled':
        queryset = queryset.filter(masak_declaration__isnull=False)
    elif masak_filter == 'missing':
        queryset = queryset.filter(masak_declaration__isnull=True)

    total = queryset.count()

    if search_value:
        # Bug 7 fix: arama first_name/last_name/phone/customer_number/
        # identification_number alanlarını birlikte tarar.
        queryset = queryset.filter(
            Q(first_name__icontains=search_value)
            | Q(last_name__icontains=search_value)
            | Q(phone__icontains=search_value)
            | Q(customer_number__icontains=search_value)
            | Q(identification_number__icontains=search_value)
        )

    count = queryset.count()

    # Sıralama — DataTables'tan gelen `order_column` adı receivable_hs ya da
    # payable_hs olabilir; bunlar artık türetilmiş alanlar olduğu için
    # `balance_hs_db` üzerinden sıralamaya yönlendiriyoruz.
    sort_field = order_column
    sort_field_clean = sort_field.lstrip('-')
    if sort_field_clean in ('receivable_hs', 'payable_hs', 'balance_hs', 'balance_eur'):
        sort_field = ('-balance_hs_db' if sort_field.startswith('-')
                      or sort_field_clean == 'payable_hs'
                      else 'balance_hs_db')

    if str(length) == '-1':
        queryset = queryset.order_by(sort_field)
    else:
        queryset = queryset.order_by(sort_field)[start:start + length]

    has_rate = get_current_has_rate(user_store) or Decimal('0')

    # Bug 3 fix: `Customers.store` ManyToManyField; instance'ta `store_id`
    # attribute YOKTUR (M2M için Django _id suffix'i eklemez). Queryset
    # zaten `store=user_store` filtresi ile çekildiği için listedeki tüm
    # müşterilerin store'u user_store; ek bir DB sorgusu/M2M traversal
    # gerekmez.
    user_store_pk = getattr(user_store, 'store_id', '') or ''

    data = []
    for c in queryset:
        bal_hs = Decimal(c.balance_hs_db or 0)
        # FAZ 33.3 — annotated stored TL (anlık piyasa kuru kullanılmaz)
        bal_tl = Decimal(c.balance_eur_db or 0).quantize(Decimal('0.01'))
        data.append({
            'id': str(c.id),
            'store__store_id': user_store_pk,
            'first_name': c.first_name or '',
            'last_name': c.last_name or '',
            'identification_number': c.identification_number or '',
            'customer_number': c.customer_number or '',
            'phone': c.phone or '',
            'gender': c.gender or '',
            'email': c.email or '',
            'address': c.address or '',
            'is_active': c.is_active,
            # Eski client uyumluluğu (DataTable column adları)
            'receivable_hs': str(bal_hs if bal_hs > 0 else Decimal('0')),
            'payable_hs': str((-bal_hs) if bal_hs < 0 else Decimal('0')),
            # Yeni semantik alanlar
            'balance_hs': str(bal_hs),
            'balance_eur': str(bal_tl),
            'masak_declaration__id': (
                str(c.masak_declaration.id) if getattr(c, 'masak_declaration', None) else None
            ),
        })

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "has_rate": str(has_rate),
        "data": data,
    })


@login_required(login_url='login')
@role_required('CUSTOMERS_DELETE')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            # GUVENLIK: store filtresi zorunlu — sadece kendi magazasinin
            # musterileri silinebilir; baska magazanin UUID'leri sessizce atlanir.
            records = Customers.objects.filter(id__in=ids, store=request.user.store)
            for record in records:
                record.is_deleted = True
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
@role_required('CUSTOMERS_CHANGE_STATUS')
def change_status(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            # GUVENLIK: store filtresi zorunlu — sadece kendi magazasinin
            # musterilerinin statusu degistirilebilir.
            records = Customers.objects.filter(id__in=ids, store=request.user.store)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required()
@role_required('CUSTOMERS_CUSTOMER_DETAIL_VIEW')
def customer_detail_view(request, customer_id):
    # GUVENLIK: store filtresi zorunlu — baska magazanin musterisi gorulemez.
    # Customers.store M2M oldugu icin store= filtresi M2M gecisini destekler.
    customer = get_object_or_404(Customers, id=customer_id, is_deleted=False, store=request.user.store)

    processes = (
        Process.objects
        .filter(customer=customer, is_deleted=False)
        .order_by('-date')
    )

    purchase_processes = processes.filter(transaction_type__in=['PURCHASE', 'RETURN'])

    sale_processes = processes.filter(transaction_type='SALE')

    # FAZ 41 — "Ödemeler" sekmesi sorgu genişletmesi.
    #
    # Eski sorgu: yalnızca Process.process_no üzerinden Payment toplardı.
    # Sorun: CollectionService.collect_and_close() tahsilat sırasında kendi
    # `process_no = TAH-YYYYMMDDHHMMSS` üretir; bu kod Process tablosuna
    # YAZILMAZ. Dolayısıyla tahsilat Payment'ları detay ekranında "Ödemeler"
    # sekmesine düşmüyordu (CashboxLedger'a yazılsa bile UI tarafında
    # görünmüyordu).
    #
    # Yeni: iki kanaldan da topla
    #   (a) Process.process_no üzerinden (satış parçalı ödemeler vb.)
    #   (b) CustomerLedger.related_payment üzerinden (tahsilat — TAH-...
    #       prefix'iyle yazılan Payment'lar müşteriye CustomerLedger
    #       FK üzerinden bağlıdır).
    # Q OR ile birleştir, distinct ile çift kayıt önle. is_cancelled=False
    # filtresi iptal edilen Payment'ları gizler (cancel_row sonrası temiz UI).
    customer_process_nos = (
        Process.objects.filter(customer=customer)
        .values_list('process_no', flat=True)
    )
    customer_payment_ids = (
        CustomerLedger.objects
        .filter(customer=customer, related_payment__isnull=False)
        .values_list('related_payment_id', flat=True)
    )
    payments = (
        Payment.objects
        .filter(
            Q(process_no__in=customer_process_nos)
            | Q(id__in=customer_payment_ids)
        )
        .filter(is_cancelled=False)
        .distinct()
        .order_by('-date')
    )

    try:
        from apps.repairs.models import Repairs
        repairs = (
            Repairs.objects
            .filter(customer=customer, is_deleted=False)
            .select_related('workshop', 'received_by', 'delivered_by')
            .order_by('-created_at')
        )
    except Exception:
        repairs = []

    # ── Cari/Emanet Refactor: Bakiyeyi append-only CustomerLedger
    # üzerinden hesapla. Eski statik alanlar (receivable_hs / payable_hs)
    # artık mutate edilmediği için sıfır okunup başlık-tablo asimetrisine
    # yol açıyordu. Tek kaynak: Customers.balance_hs property.
    #
    # Konvansiyon (yeni model):
    #   balance_hs > 0 → müşteri mağazaya borçlu (debt)
    #   balance_hs < 0 → mağaza müşteriye borçlu (credit/müşteri alacaklı)
    #   balance_hs == 0 → bakiye sıfır
    ledger_balance_hs = Decimal(customer.balance_hs or 0)

    # ──────────────────────────────────────────────────────────────
    # FAZ 33.3 — Bakiye TL'si STORED (borç yazıldığı andaki kur)
    # ──────────────────────────────────────────────────────────────
    # Eski: balance_eur = balance_hs × get_current_has_rate(store)
    # (anlık ALIŞ kuru) → satış SATIŞ kuruyla yapıldıysa görüntülenen
    # TL spread farkı kadar düşüyordu.
    # Yeni: balance_eur = customer.balance_eur (Σ signed amount_eur).
    # Her ledger satırı yazıldığı kuruyla amount_eur alanını saklar
    # → "satıştaki TL = cari TL = tahsilattaki TL" garantisi.
    # has_rate context'e hâlâ gönderiliyor; frontend'de bazı modaller
    # (tahsilat preview hariç) anlık kur referansı için kullanır.
    # ──────────────────────────────────────────────────────────────
    has_rate = get_current_has_rate(request.user.store) or Decimal('0')
    has_price = has_rate  # Şablon "has_price" adını kullanıyor olabilir

    balance_eur = Decimal(customer.balance_eur or 0)

    if ledger_balance_hs > 0:
        balance_type = 'debt'
    elif ledger_balance_hs < 0:
        balance_type = 'credit'
    else:
        balance_type = 'zero'

    # Template her zaman pozitif tutarı + balance_type'ı bekler.
    balance_hs = abs(ledger_balance_hs)
    balance_eur_abs = abs(balance_eur)

    context = {
        'title': f'{(customer.first_name or "")} {(customer.last_name or "")}'.strip() or 'Müşteri',
        'customer': customer,
        'processes': processes,
        'payments': payments,
        'purchase_processes': purchase_processes,
        'sale_processes': sale_processes,
        'repairs': repairs,
        'balance_hs': balance_hs,
        'balance_eur': balance_eur_abs,
        'balance_type': balance_type,
        # FAZ 14 UI — B1: Anlık has kuru, frontend'in tutarlı TL dönüşümü
        # yapabilmesi için context'e ekleniyor.
        # FAZ 22.1 — has_price (eski isim) şablonun iki yerinde
        # kullanılıyor (gizli input + JS FALLBACK_HAS_RATE). Eksik olduğu
        # için kur 0'la başlıyordu; eklendi.
        'has_price': float(has_rate),
        'has_rate': float(has_rate),
        'has_rate_str': str(has_rate),
    }
    write_log(request, 'Müşteriler', f'Müşteri Detayları Görüntülendi. ID= {customer.id}')
    return render(request, 'management/customers/detail.html', context)


def _gen_code(n=6): return ''.join(random.choice(string.digits) for _ in range(n))


def normalize_tr_msisdn(phone: str) -> str:
    if not phone: return ""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"): digits = digits.lstrip("0")
    if not digits.startswith("90"): digits = "90" + digits
    return f"+{digits}"


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff: return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


@login_required
def customer_verify_state(request, customer_id):
    # GUVENLIK: store filtresi — baska magazanin musteri kontakt durumu okunamaz.
    c = get_object_or_404(Customers, pk=customer_id, is_deleted=False, store=request.user.store)
    return JsonResponse({
        'phone': {'value': c.phone or '', 'verified': bool(c.is_phone_verified)},
        'email': {'value': c.email or '', 'verified': bool(c.is_email_verified)},
    })


@login_required
def send_customer_verification(request, customer_id):
    """
    Müşteriye doğrulama kodu gönderir (Email veya WhatsApp).
    Telefon numarası formatı burada normalize edilir.
    """
    # GUVENLIK: store filtresi — baska magazanin musterisine OTP gonderilemez.
    c = get_object_or_404(Customers, pk=customer_id, is_deleted=False, store=request.user.store)
    channel = request.POST.get('channel')  # 'email' | 'phone'

    if channel not in ('email', 'phone'):
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz kanal seçimi.'})

    # Varsa eski kullanılmamış kodları iptal et (temizlik)
    OtpCode.objects.filter(
        owner_type='customer', owner_id=str(c.id), channel=channel,
        used=False
    ).update(used=True)

    # Yeni kod oluştur
    code = _gen_code()
    OtpCode.objects.create(
        owner_type='customer', owner_id=str(c.id), channel=channel,
        code=code, purpose=f'verify_{channel}',
        expires_at=timezone.now() + timedelta(minutes=10)
    )

    # --- 1. E-POSTA GÖNDERİMİ ---
    if channel == 'email':
        if not c.email:
            return JsonResponse({'error': True, 'error_msg': 'Müşterinin e-posta adresi kayıtlı değil.'})

        display_name = f"{c.first_name or ''} {c.last_name or ''}".strip() or c.email.split('@')[0]

        ctx = {
            'subject': 'İletişim Onayı • Doğrulama Kodu',
            'otp_string': code,
            'user': {'username': display_name},
            'verify_url': request.build_absolute_uri('#'),
            'consent_scope': 'E-posta',
            'brand_name': 'Kuyum Plus',
            'support_phone': '+902166068801',
            'support_mail': 'destek@kuyumplus.com',
            'address_line': 'Merkez',
        }

        # --- EmailService Kullanımı (GÜNCELLENDİ) ---
        is_sent = EmailService.send(
            user=c,
            subject='İletişim Onayı • Doğrulama Kodu',
            template_name='management/mail_templates/verify_contact_iys.html',
            context=ctx
        )

        if is_sent:
            return JsonResponse({'result': True})
        else:
            return JsonResponse(
                {'error': True, 'error_msg': 'E-posta gönderilemedi (Sistem hatası veya bildirimler kapalı).'})

    # --- 2. TELEFON (WHATSAPP) GÖNDERİMİ ---
    # Veritabanındaki ham telefonu al
    to_raw = c.phone or ''
    if not to_raw:
        return JsonResponse({'error': True, 'error_msg': 'Müşterinin telefon numarası kayıtlı değil.'})

    to_normalized = normalize_tr_phone(to_raw)

    store = getattr(request.user, "store", None)
    if not store and c.store.exists():
        store = c.store.first()

    if not store:
        return JsonResponse({'error': True, 'error_msg': 'İşlem yapılacak mağaza bulunamadı.'})

    can_send, reason, lang = wa_preflight(store, "verify_phone_v1", "tr_TR")
    if not can_send:
        return JsonResponse({'error': True, 'error_msg': f'WhatsApp gönderilemiyor: {reason or "Bilinmeyen hata"}'})

    ok, err_code = send_whatsapp_template_guarded(
        store=store,
        user=request.user,
        customer=c,
        to=to_normalized,
        template="verify_phone_v1",
        language=lang,
        header_params=None,
        body_params=[code],
        button_params=[code],
        validate=False,
        return_reason=True
    )

    if not ok:
        return JsonResponse({'error': True, 'error_msg': f'WhatsApp hatası: {err_code}'})

    return JsonResponse({'result': True})


@login_required
def confirm_customer_verification(request, customer_id):
    """
    Müşterinin girdiği kodu doğrular ve veritabanını günceller.
    """
    # GUVENLIK: store filtresi — baska magazanin musterisinin OTP'si onaylanamaz.
    c = get_object_or_404(Customers, pk=customer_id, is_deleted=False, store=request.user.store)
    channel = request.POST.get('channel')
    code = (request.POST.get('code') or '').strip()
    consent_iys = request.POST.get('consent_iys') == '1'  # Checkbox işaretli mi?
    now = timezone.now()

    if not code:
        return JsonResponse({'error': True, 'error_msg': 'Lütfen doğrulama kodunu giriniz.'})

    # OTP Kodunu Veritabanında Ara
    # - Müşteri ID eşleşmeli
    # - Kanal (email/phone) eşleşmeli
    # - Amaç (verify_email/verify_phone) eşleşmeli
    # - Kod eşleşmeli
    # - Kullanılmamış olmalı
    # - Süresi dolmamış olmalı
    otp_record = OtpCode.objects.filter(
        owner_type='customer',
        owner_id=str(c.id),
        channel=channel,
        purpose=f'verify_{channel}',
        code=code,
        used=False,
        expires_at__gt=now
    ).first()

    if not otp_record:
        return JsonResponse({'error': True, 'error_msg': 'Kod geçersiz veya süresi dolmuş.'})

    # 1. Kodu kullanıldı olarak işaretle
    otp_record.used = True
    otp_record.save()

    # 2. Müşteri kaydını güncelle
    if channel == 'email':
        c.is_email_verified = True
    else:
        c.is_phone_verified = True

    c.save(update_fields=['is_email_verified', 'is_phone_verified'])

    # 3. İYS Onayı (Varsa)
    if consent_iys:
        ContactConsent.objects.update_or_create(
            owner_type='customer', owner_id=str(c.id), channel=channel,
            defaults={
                'is_consented': True,
                'consented_at': timezone.now(),
                'ip_address': _client_ip(request),  # IP alma fonksiyonunuzu kullandım
                'source': 'otp_verify',
                'iys_status': 'pending',  # Sonra toplu gönderim için pending
                'iys_ref': None
            }
        )

    return JsonResponse({'result': True})


@login_required(login_url='login')
def get_customer_transactions(request, customer_id):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '')
    order_column_index = request.GET.get('order[0][column]', '0')
    order_dir = request.GET.get('order[0][dir]', 'asc')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    tx = (request.GET.get('transaction_type') or 'PURCHASE').upper()

    columns = [
        "process_no", "transaction_type", "process_mileage", "employee__first_name",
        "product__name", "piece", "unit_price", "gram", "amount", "price_hs", "date", "is_status",
    ]
    order_column = columns[int(order_column_index)] if str(order_column_index).isdigit() else "date"
    if order_dir == 'desc':
        order_column = f"-{order_column}"

    base_qs = Process.objects.filter(is_deleted=False, store=request.user.store, customer_id=customer_id)
    all_process_nos = list(base_qs.values_list('process_no', flat=True).distinct())

    # 1. ÖDEMELERİ BUL
    payments = Payment.objects.filter(process_no__in=all_process_nos).values('process_no').annotate(
        paid_in=Sum(Case(When(is_output=False, then=F('amount')), default=0, output_field=DecimalField())),
        paid_out=Sum(Case(When(is_output=True, then=F('amount')), default=0, output_field=DecimalField()))
    )
    payment_dict = {p['process_no']: (p['paid_in'] or Decimal('0')) - (p['paid_out'] or Decimal('0')) for p in payments}

    # 2. SEPET BAZINDA NET TL VE NET HAS (price_hs) DEĞERLERİNİ TOPLA
    process_sums = base_qs.values('process_no').annotate(
        sales_total=Sum(Case(When(transaction_type__in=['SALE', 'ORDER_IN'], then=F('amount')), default=0,
                             output_field=DecimalField())),
        purchases_total=Sum(
            Case(When(transaction_type__in=['PURCHASE', 'RETURN', 'STOCK_IN'], then=F('amount')), default=0,
                 output_field=DecimalField())),
        sales_hs=Sum(Case(When(transaction_type__in=['SALE', 'ORDER_IN'], then=F('price_hs')), default=0,
                          output_field=DecimalField())),
        purchases_hs=Sum(
            Case(When(transaction_type__in=['PURCHASE', 'RETURN', 'STOCK_IN'], then=F('price_hs')), default=0,
                 output_field=DecimalField()))
    )

    basket_data = {}
    for p in process_sums:
        net_tl = (p['sales_total'] or Decimal('0')) - (p['purchases_total'] or Decimal('0'))
        net_hs = (p['sales_hs'] or Decimal('0')) - (p['purchases_hs'] or Decimal('0'))
        basket_data[p['process_no']] = {'net_tl': net_tl, 'net_hs': net_hs}

    # 3. FİLTRELEME
    valid_pnos = []
    for pno in all_process_nos:
        net = basket_data.get(pno, {}).get('net_tl', Decimal('0'))
        paid = payment_dict.get(pno, Decimal('0'))
        bal = net - paid
        if tx == 'DEBT':
            if abs(bal) > Decimal('0.01'):
                valid_pnos.append(pno)
        else:
            valid_pnos.append(pno)

    qs = base_qs.filter(process_no__in=valid_pnos).select_related('product', 'employee')

    if tx != 'DEBT' and tx != 'ALL':
        qs = qs.filter(transaction_type=tx)

    if date_from:
        try:
            df = datetime.strptime(date_from, '%d/%m/%Y')
            qs = qs.filter(date__date__gte=df)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, '%d/%m/%Y')
            qs = qs.filter(date__date__lte=dt)
        except ValueError:
            pass
    if search_value:
        qs = qs.filter(
            Q(process_no__icontains=search_value) | Q(product__name__icontains=search_value) |
            Q(employee__first_name__icontains=search_value) | Q(employee__last_name__icontains=search_value)
        )

    total = qs.count()
    if length != -1:
        qs = qs.order_by(order_column)[start:start + length]
    else:
        qs = qs.order_by(order_column)

    # 4. JSON OLUŞTUR VE ORANSAL HAS HESAPLA
    data = []
    seen_pnos = set()

    for r in qs:
        direction = 'Giriş' if r.transaction_type in ('PURCHASE', 'STOCK_IN', 'RETURN') else 'Çıkış'
        pno = r.process_no

        basket = basket_data.get(pno, {'net_tl': Decimal('0'), 'net_hs': Decimal('0')})
        net_amt = basket['net_tl']
        net_hs = basket['net_hs']

        paid_amt = payment_dict.get(pno, Decimal('0'))
        balance_eur = net_amt - paid_amt

        if pno not in seen_pnos:
            seen_pnos.add(pno)
            show_checkbox = True

            # KUYUMCU MATEMATİĞİ: Bakiye TL / Net TL oranı kadar Has kalmıştır
            basket_remaining_hs = Decimal('0.000')
            if net_amt != Decimal('0'):
                basket_remaining_hs = (balance_eur / net_amt) * net_hs
            else:
                basket_remaining_hs = net_hs

            remaining_hs = basket_remaining_hs
        else:
            show_checkbox = False
            remaining_hs = Decimal('0.000')

        data.append({
            "process_no": r.process_no,
            "transaction_type": r.transaction_type,
            "direction": direction,
            "employee": (r.employee.first_name if r.employee else "-"),
            "product": (r.product.name if r.product else "-"),
            "piece": int(r.piece or 0),
            "unit_price": float(r.unit_price or 0),
            "gram": float(r.gram or 0.0),
            "amount": float(r.amount or 0),
            "price_hs": float(r.price_hs or 0),
            "paid_amount": float(paid_amt) if show_checkbox else 0.0,
            "remaining_hs": float(remaining_hs),
            "show_checkbox": show_checkbox,
            "date": r.date,
            "status": r.is_status or "-",
        })

    return JsonResponse({"draw": draw, "recordsTotal": total, "recordsFiltered": total, "data": data})


# apps/customers/views.py (En alta ekle)

@login_required(login_url='login')
def get_customer_detail_json(request):
    """
    AJAX ile tek bir müşterinin detaylarını JSON olarak döner.
    Resim URL'leri ve MASAK beyan formundaki İYS izin durumu da dahil edildi.
    """
    customer_id = request.GET.get('id')
    if not customer_id:
        return JsonResponse({'result': False, 'error_msg': 'ID parametresi eksik.'}, status=400)

    try:
        customer = Customers.objects.get(id=customer_id, store=request.user.store, is_deleted=False)

        # --- Kimlik görselleri ---
        # Öncelik: MASAK beyan formundaki görseller, sonra müşterinin kendi alanları.
        masak_decl = getattr(customer, 'masak_declaration', None)

        front_img_url = None
        back_img_url = None

        if masak_decl and masak_decl.id_front_image:
            try:
                front_img_url = masak_decl.id_front_image.url
            except Exception:
                front_img_url = None
        if masak_decl and masak_decl.id_back_image:
            try:
                back_img_url = masak_decl.id_back_image.url
            except Exception:
                back_img_url = None

        if not front_img_url and customer.identification_front_image:
            try:
                front_img_url = customer.identification_front_image.url
            except Exception:
                front_img_url = None
        if not back_img_url and customer.identification_back_image:
            try:
                back_img_url = customer.identification_back_image.url
            except Exception:
                back_img_url = None

        # --- MASAK / İYS izin durumu ---
        if masak_decl is not None:
            has_masak = True
            iys_approved = bool(
                masak_decl.consent_iys_sms
                or masak_decl.consent_iys_email
                or masak_decl.consent_iys_call
            )
            iys_sms = bool(masak_decl.consent_iys_sms)
            iys_email = bool(masak_decl.consent_iys_email)
            iys_call = bool(masak_decl.consent_iys_call)
        else:
            has_masak = False
            iys_approved = False
            iys_sms = False
            iys_email = False
            iys_call = False

        data = {
            'id': str(customer.id),
            'first_name': customer.first_name or '',
            'last_name': customer.last_name or '',
            'identification_number': customer.identification_number or '',
            'phone': customer.phone or '',
            'email': customer.email or '',
            'gender': customer.gender or '',
            'address': customer.address or '',
            'city': customer.city_id or '',
            'district': customer.district_id or '',
            'tax_office': customer.tax_office_id or '',
            'tax_office_code': customer.tax_office_code or '',
            'identification_front_image': front_img_url,
            'identification_back_image': back_img_url,
            # MASAK / İYS
            'has_masak_declaration': has_masak,
            'iys_approved': iys_approved,
            'iys_sms': iys_sms,
            'iys_email': iys_email,
            'iys_call': iys_call,
        }
        return JsonResponse({'result': True, 'data': data})

    except Customers.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri bulunamadı.'}, status=404)
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


def generate_process_no():
    return 'P' + ''.join(random.choices('0123456789', k=10))


# ════════════════════════════════════════════════════════════════════════════
# FAZ 14 UI — Bug 4/5/6 fix: Kasa-Cari atomik tahsilat helper'ları
# ════════════════════════════════════════════════════════════════════════════
#
# `save_debt_collection` eski form akışı `CollectionService` yerine doğrudan
# Payment yazıyordu. Sonuçlar:
#   - Bug 4: CashboxLedger.INCOME/EXPENSE yazılmıyordu → kasa bakiyesi SSOT
#     ile çelişiyor.
#   - Bug 5: Çoklu işlem seçiminde tek toplu CustomerLedger satırı yazılıyor,
#     diğer process_no'lar audit trail'inde kayboluyor.
#   - Bug 6: Payment.objects.create() Django'da clean()'i çağırmıyor; FAZ 14
#     bank_account validasyonu bypass ediliyor.
#
# Bu helper'lar her tahsilat birimi (process_no) için tek atomik adımda
# Payment + CashboxLedger + (opsiyonel) per-process audit yazımı yapar.
def _resolve_default_cash_account(store, request):
    """Bu store için kullanılacak BankAccount'ı belirler.

    Öncelik:
      1) POST 'bank_account_id' ile gelen, store'a ait, aktif hesap
         (CASH/POS/BANK üçü de kabul).
      2) Store'un account_type=CASH, is_active=True ilk kasası (fallback).

    FAZ 36 — Account-type kısıtı kaldırıldı:
        Eskiden yalnız CASH kabul ediliyordu; POS/BANK kasaya tahsilat
        ValidationError ile reddediliyordu. CollectionService zaten
        POS→CREDIT_CARD, BANK→TRANSFER haritalamasıyla üç tipi de
        destekliyor. `save_debt_collection` artık aynı kapsamı sağlar.
    """
    posted_id = (request.POST.get('bank_account_id') or '').strip()
    if posted_id:
        try:
            account = BankAccount.objects.get(
                pk=posted_id, store=store, is_active=True, is_deleted=False,
            )
        except BankAccount.DoesNotExist:
            raise ValidationError(
                'Seçilen kasa bulunamadı veya bu mağazaya ait değil.',
            )
        return account

    cash_account = (
        BankAccount.objects
        .filter(
            store=store,
            account_type=BankAccount.AccountType.CASH,
            is_active=True,
            is_deleted=False,
        )
        .order_by('created_on')
        .first()
    )
    if cash_account is None:
        raise ValidationError(
            'Bu mağaza için aktif bir nakit kasa tanımlı değil. '
            'Önce Banka/Kasa Hesapları ekranından CASH tipinde kasa açılmalı.',
        )
    return cash_account


def _create_payment_with_clean(*, process_no, amount, bank_account, is_output, reference):
    """Payment'ı clean() validasyonu tetiklenerek oluşturur.

    FAZ 36 — payment_type, bank_account.account_type'tan türetilir.
        CASH → 'CASH', POS → 'CREDIT_CARD', BANK → 'TRANSFER'.
        Reconciliation: CASH için NOT_REQUIRED, diğerleri PENDING.
    """
    from apps.customers.services.collection import ACCOUNT_TYPE_TO_PAYMENT_TYPE
    payment_type = ACCOUNT_TYPE_TO_PAYMENT_TYPE.get(
        getattr(bank_account, 'account_type', None), 'CASH',
    )
    if payment_type == 'CASH':
        recon_status = Payment.ReconciliationStatus.NOT_REQUIRED
    else:
        recon_status = Payment.ReconciliationStatus.PENDING
    payment = Payment(
        process_no=process_no,
        payment_type=payment_type,
        amount=amount,
        date=timezone.now(),
        is_output=is_output,
        reference=reference,
        bank_account=bank_account,
        reconciliation_status=recon_status,
    )
    payment.clean()  # FAZ 14 Step 5 validasyonu (bank_account zorunluluğu)
    payment.save()
    return payment


def _write_cashbox_movement(*, payment, bank_account, store, direction, audit, description):
    """Payment'a eşlik eden CashboxLedger satırını yazar (Bug 4 fix).

    direction='collection'        → INCOME (kasaya giriş)
    direction='payment_to_customer'→ EXPENSE (kasadan çıkış)
    """
    movement_type = (
        CashboxLedger.MovementType.INCOME
        if direction == 'collection'
        else CashboxLedger.MovementType.EXPENSE
    )
    currency = (bank_account.currency or 'TRY').upper()

    prior_balance = bank_account.get_balance(currency=currency)
    delta = payment.amount if direction == 'collection' else -payment.amount
    new_balance = (prior_balance + delta).quantize(Decimal('0.01'))

    return CashboxLedger.objects.create(
        cashbox=bank_account,
        store=store,
        movement_type=movement_type,
        amount=payment.amount,
        currency=currency,
        amount_eur_equivalent=payment.amount,
        exchange_rate=None,
        balance_snapshot=new_balance,
        related_payment=payment,
        process_no=payment.process_no,
        description=description[:255],
        created_by=audit.get('actor'),
        ip_address=audit.get('ip_address'),
        user_agent=audit.get('user_agent') or '',
    )


# ════════════════════════════════════════════════════════════════════════════
# FAZ 36 — Per-process bakiye hesabı (CustomerLedger SSOT)
# ════════════════════════════════════════════════════════════════════════════
# Eski hesap (Process+Payment) FAZ 33.3 sonrası split-brain yaratıyordu:
#   Process.amount (satış anındaki kur) ile CustomerLedger.amount_eur (efektif
#   stored TL) farklılaşınca per-process bakiye negatif çıkıyor → tahsilat
#   process'i atlıyor → Payment + CashboxLedger hiç yazılmıyor → "kasaya
#   para girmedi" şikayeti. Yeni hesap Customers._ledger_aggregate kuralını
#   process_no filtresi ile aynen kullanır; tek SSOT: CustomerLedger.
def _get_process_ledger_balance(customer, process_no):
    """Per-process CustomerLedger bakiyesi (HS, TL).

    Customers._ledger_aggregate ile aynı tip kümeleri ve REVERSAL/CORRECTION
    mantığını uygular; tek fark process_no filtresi.

    Returns:
        (balance_hs, balance_eur) tuple — pozitif = müşteri borçlu.
    """
    base_qs = CustomerLedger.objects.filter(
        customer=customer,
        process_no=process_no,
        is_active=True,
        is_approved=True,
    )
    _tl_field = DecimalField(max_digits=14, decimal_places=2)

    agg = base_qs.aggregate(
        debt_hs=Coalesce(
            Sum('amount_hs', filter=Q(transaction_type__in=CustomerLedger.DEBT_INCREASING_TYPES)),
            Decimal('0'),
        ),
        credit_hs=Coalesce(
            Sum('amount_hs', filter=Q(transaction_type__in=CustomerLedger.DEBT_DECREASING_TYPES)),
            Decimal('0'),
        ),
        rev_debt_hs=Coalesce(
            Sum('amount_hs', filter=Q(
                transaction_type='REVERSAL',
                reversal_target_type__in=CustomerLedger.DEBT_INCREASING_TYPES,
            )),
            Decimal('0'),
        ),
        rev_credit_hs=Coalesce(
            Sum('amount_hs', filter=Q(
                transaction_type='REVERSAL',
                reversal_target_type__in=CustomerLedger.DEBT_DECREASING_TYPES,
            )),
            Decimal('0'),
        ),
        correction_hs=Coalesce(
            Sum('amount_hs_signed', filter=Q(transaction_type='CORRECTION')),
            Decimal('0'),
        ),
        debt_eur=Coalesce(
            Sum('amount_eur', filter=Q(transaction_type__in=CustomerLedger.DEBT_INCREASING_TYPES)),
            Decimal('0'),
            output_field=_tl_field,
        ),
        credit_eur=Coalesce(
            Sum('amount_eur', filter=Q(transaction_type__in=CustomerLedger.DEBT_DECREASING_TYPES)),
            Decimal('0'),
            output_field=_tl_field,
        ),
        rev_debt_eur=Coalesce(
            Sum('amount_eur', filter=Q(
                transaction_type='REVERSAL',
                reversal_target_type__in=CustomerLedger.DEBT_INCREASING_TYPES,
            )),
            Decimal('0'),
            output_field=_tl_field,
        ),
        rev_credit_eur=Coalesce(
            Sum('amount_eur', filter=Q(
                transaction_type='REVERSAL',
                reversal_target_type__in=CustomerLedger.DEBT_DECREASING_TYPES,
            )),
            Decimal('0'),
            output_field=_tl_field,
        ),
        correction_eur=Coalesce(
            Sum(Case(
                When(
                    transaction_type='CORRECTION',
                    amount_hs_signed__gt=0,
                    then=F('amount_eur'),
                ),
                When(
                    transaction_type='CORRECTION',
                    amount_hs_signed__lt=0,
                    then=-F('amount_eur'),
                ),
                default=Decimal('0'),
                output_field=_tl_field,
            )),
            Decimal('0'),
            output_field=_tl_field,
        ),
    )

    balance_hs = (
        (agg['debt_hs'] - agg['rev_debt_hs'])
        - (agg['credit_hs'] - agg['rev_credit_hs'])
        + agg['correction_hs']
    )
    balance_eur = (
        (agg['debt_eur'] - agg['rev_debt_eur'])
        - (agg['credit_eur'] - agg['rev_credit_eur'])
        + agg['correction_eur']
    )
    return balance_hs, balance_eur


def _write_customer_ledger_for_unit(
    *, customer, store, direction, currency_type, alloc_hs, alloc_tl,
    rate_val, process_no, audit, description, related_payment=None,
):
    """Tek bir tahsilat/ödeme birimi için CustomerLedger satırı yazar.

    FAZ 39 — YÖN DÜZELTMESİ:
        Eski kod `direction='payment_to_customer'` için `write_credit`
        çağırıyordu (CREDIT = DEBT_DECREASING). Müşteri ALACAKLI iken
        (balance < 0) bir CREDIT daha eklemek balance'ı daha negatife
        itip mağazanın borcunu **iki katına çıkarıyordu**.

        Doğru semantik: store müşteriye ödeme yapınca müşteri alacağı
        kapanmalı → balance 0'a yaklaşmalı → DEBT_INCREASING (DEBT)
        kaydı yazılmalı. Bu ne mağazanın yeni borcunu yaratır ne de
        müşterinin yeni borcunu — sadece alacağı sıfırlayan karşı giriş.

    FAZ 39 — STORED TL TUTARLILIĞI:
        Hem write_collection hem write_debt/write_credit çağrılarına
        `amount_eur` ve `exchange_rate_eur` aktarılır. Bu sayede satıştaki
        TL küsuratı korunur (round-trip yuvarlaması yok).

    FAZ 51 — KASA SİMETRİ ONARIMI (R-01):
        `related_payment` parametresi eklendi. Caller (save_debt_collection)
        Payment kaydını oluşturduktan sonra bu fonksiyona iletir; biz de
        write_collection / write_debt çağrılarına aktarırız. Sonuç olarak
        REVERSAL akışında `propagate_reversal_side_effects` orijinal
        CashboxLedger satırını related_payment_id üzerinden bulur ve
        karşı REVERSAL hareketini yazar (kasa-cari mutabakat sağlanır).
        Önce sadece CollectionService bu bağı kuruyordu; sepet modu yetimdi.
    """
    rate_kwarg = rate_val if rate_val and rate_val > 0 else None

    if direction == 'collection':
        ledger_currency = (
            CustomerLedger.CURRENCY_TRY
            if currency_type == 'TL'
            else CustomerLedger.CURRENCY_HS
        )
        return LedgerService.write_collection(
            customer=customer,
            store=store,
            transaction_type=(
                CustomerLedger.COLLECTION_TL
                if currency_type == 'TL'
                else CustomerLedger.COLLECTION_HS
            ),
            amount_hs=alloc_hs,
            amount_eur=alloc_tl,
            exchange_rate_eur=rate_val if rate_val > 0 else Decimal('0'),
            process_no=process_no,
            audit=audit,
            currency=ledger_currency,
            related_payment=related_payment,
            description=description[:255],
        )

    # FAZ 39 — payment_to_customer: müşteri alacağını kapatan DEBT girişi.
    # FAZ 51 — Bu DEBT'in kasa karşılığı CashboxLedger.EXPENSE'tir;
    # related_payment bağlantısı REVERSAL'da kasa karşı hareketinin
    # bulunabilmesi için kritiktir.
    return LedgerService.write_debt(
        customer=customer,
        store=store,
        amount_hs=alloc_hs,
        amount_eur=alloc_tl,
        exchange_rate_eur=rate_kwarg,
        process_no=process_no,
        audit=audit,
        description=description[:255],
        related_payment=related_payment,
    )


@login_required
@transaction.atomic
def save_debt_collection(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek.'})

    try:
        customer_id = request.POST.get('customer_id')
        currency_type = request.POST.get('currency_type')

        try:
            amount_val = Decimal(request.POST.get('amount', '0').replace(',', '.') or '0')
            rate_val = Decimal(request.POST.get('exchange_rate', '0').replace(',', '.') or '1')
        except:
            return JsonResponse({'result': False, 'error_msg': 'Lütfen geçerli sayısal değerler giriniz.'})

        description = request.POST.get('description', '')
        direction = request.POST.get('direction', 'collection')
        selected_processes_raw = request.POST.get('selected_processes', '').strip()

        if amount_val <= 0:
            return JsonResponse({'result': False, 'error_msg': 'Tutar 0 dan büyük olmalıdır.'})

        if currency_type == 'TL' and rate_val <= 0:
            return JsonResponse({'result': False, 'error_msg': 'TL işlemi için geçerli bir kur girilmelidir.'})

        # GUVENLIK: store ve is_deleted filtresi zorunlu — baska magazanin
        # musterisine odeme/tahsilat yapilamaz; silinmis musteri de isleme alinmaz.
        user = request.user
        store = user.store
        customer = get_object_or_404(Customers, pk=customer_id, store=store, is_deleted=False)

        # FAZ 14 Bug 4/6 fix: Kasa hesabını burada çözümle. Eksikse erken
        # uyarı ver (transaction içinde zaten herhangi bir Payment yazımı
        # da clean() tarafından reddedilirdi).
        try:
            bank_account = _resolve_default_cash_account(store, request)
        except ValidationError as ve:
            return JsonResponse({
                'result': False,
                'error_msg': '; '.join(ve.messages) if hasattr(ve, 'messages') else str(ve),
            })

        # ════════════════════════════════════════════════════════════════
        # FAZ 31 / BUG-6 — KASA SEÇİMİ ŞEFFAFLIĞI (2026-05-01)
        # ════════════════════════════════════════════════════════════════
        # Müşteri şikayeti: "Tahsilat yaptım kasaya eklenmedi."
        # Asıl sebep: Frontend bank_account_id göndermeyince
        # `_resolve_default_cash_account` mağazanın ilk CASH kasasını
        # otomatik seçiyor — kullanıcı POS/banka kasasına gittiğini sanıyor.
        # Bu log, hangi tahsilatın hangi kasaya otomatik atandığını
        # production loglarına yazar. Frontend'e ayrıca dropdown ekleniyor.
        # ════════════════════════════════════════════════════════════════
        _posted_bank_id = (request.POST.get('bank_account_id') or '').strip()
        if not _posted_bank_id:
            import logging as _faz31_log
            _faz31_logger = _faz31_log.getLogger('apps.customers.collection')
            _faz31_logger.warning(
                "save_debt_collection: bank_account_id POST'ta yok, "
                "otomatik kasa seçildi: name=%s id=%s store=%s customer=%s",
                getattr(bank_account, 'name', '?'),
                getattr(bank_account, 'id', '?'),
                getattr(store, 'id', '?'),
                customer_id,
            )

        amount_eur = Decimal('0')
        amount_hs = Decimal('0')

        if currency_type == 'TL':
            amount_eur = amount_val
            if rate_val > 0:
                amount_hs = amount_eur / rate_val
        else:
            amount_hs = amount_val
            amount_eur = amount_hs * rate_val

        amount_eur = amount_eur.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amount_hs = amount_hs.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        if direction == 'payment_to_customer':
            desc_prefix = "Müşteriye Ödeme"
        else:
            desc_prefix = "Tahsilat"

        selected_process_nos = []
        if selected_processes_raw:
            try:
                selected_process_nos = json.loads(selected_processes_raw)
                if not isinstance(selected_process_nos, list):
                    selected_process_nos = []
            except (json.JSONDecodeError, TypeError):
                selected_process_nos = [x.strip() for x in selected_processes_raw.split(',') if x.strip()]

        # FAZ 14 Bug 5 fix: per-process LedgerService yazımı için audit
        # context'i bir kez alınır.
        audit = extract_audit_context(request)
        ref_text = f"{desc_prefix} ({currency_type}) - {description}"
        ledger_desc = f'{desc_prefix} ({currency_type}) — {description}'

        if selected_process_nos:
            remaining_to_distribute_hs = amount_hs
            total_paid_hs = Decimal('0')

            unique_pnos = list(set(selected_process_nos))

            for pno in unique_pnos:
                if remaining_to_distribute_hs <= Decimal('0.001'):
                    break

                procs = Process.objects.filter(process_no=pno, customer=customer, store=store, is_deleted=False)
                if not procs.exists():
                    continue

                # ── FAZ 36 — Per-process bakiye CustomerLedger SSOT'tan ──
                # Eski hesap Process.amount + Payment.amount kullanıyordu;
                # FAZ 33.3 efektif kur ile yazılan tahsilatlar Process.amount
                # ile aynı kura sahip değildi → balance_eur negatif → process
                # atlanıyor → Payment + CashboxLedger yazılmıyordu. Yeni hesap
                # CustomerLedger SSOT'tan gelir; "satıştaki TL = cari TL" .
                balance_hs_proc, balance_eur_proc = _get_process_ledger_balance(
                    customer, pno,
                )

                # Sıfır bakiyeli process'i atla (zaten kapanmış).
                if abs(balance_hs_proc) <= Decimal('0.001'):
                    continue

                is_customer_debt = (balance_hs_proc > 0)
                if direction == 'collection' and not is_customer_debt:
                    continue
                if direction == 'payment_to_customer' and is_customer_debt:
                    continue

                basket_hs_abs = abs(balance_hs_proc)
                basket_tl_abs = abs(balance_eur_proc)

                # Kapatılacak Has Miktarı
                alloc_hs = min(remaining_to_distribute_hs, basket_hs_abs)
                alloc_hs = alloc_hs.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

                # Has → TL dönüşümü: önce per-process efektif kur (stored TL/HS),
                # yoksa formdan gelen rate_val. Bu sayede her process kendi
                # yazıldığı andaki kurla simetrik kapanır.
                if basket_hs_abs > Decimal('0') and basket_tl_abs > Decimal('0'):
                    proc_rate = basket_tl_abs / basket_hs_abs
                    alloc_tl = alloc_hs * proc_rate
                else:
                    alloc_tl = alloc_hs * rate_val

                alloc_tl = alloc_tl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                # FAZ 14 Bug 6 fix: Payment.clean() tetiklenerek yazılır.
                payment = _create_payment_with_clean(
                    process_no=pno,
                    amount=alloc_tl,
                    bank_account=bank_account,
                    is_output=(direction == 'payment_to_customer'),
                    reference=ref_text[:99],
                )
                # FAZ 14 Bug 4 fix: CashboxLedger eşzamanlı yazılır.
                _write_cashbox_movement(
                    payment=payment,
                    bank_account=bank_account,
                    store=store,
                    direction=direction,
                    audit=audit,
                    description=ledger_desc,
                )
                # FAZ 14 Bug 5 fix: Her process_no için ayrı CustomerLedger
                # satırı; toplu kayıt yerine birebir audit trail.
                # FAZ 51 (R-01): payment instance'ı CustomerLedger'a bağlanır
                # → REVERSAL'da propagate_reversal_side_effects kasa karşı
                # hareketini doğrudan related_payment_id ile bulur.
                _write_customer_ledger_for_unit(
                    customer=customer,
                    store=store,
                    direction=direction,
                    currency_type=currency_type,
                    alloc_hs=alloc_hs,
                    alloc_tl=alloc_tl,
                    rate_val=rate_val,
                    process_no=pno,
                    audit=audit,
                    description=ledger_desc,
                    related_payment=payment,
                )

                remaining_to_distribute_hs -= alloc_hs
                total_paid_hs += alloc_hs

            total_paid_hs = total_paid_hs.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

            # FAZ 36 — Sessiz "başarı" yerine açık hata.
            # Eski davranış: tüm process'ler atlandığında (zaten kapanmış,
            # yön uyumsuz, vb.) hiçbir Payment/CashboxLedger yazılmadan
            # `result: True` dönüyordu → kullanıcı "kasaya gitmedi" şikayeti.
            if total_paid_hs <= Decimal('0.001'):
                return JsonResponse({
                    'result': False,
                    'error_msg': (
                        'Seçilen işlemlere tahsilat dağıtılamadı. '
                        'İşlemlerin tamamı zaten kapanmış olabilir veya '
                        'işlem yönü (tahsilat/ödeme) bakiye ile uyumsuz. '
                        'Lütfen müşteri modunu deneyin ya da güncel '
                        'bakiyeyi kontrol edin.'
                    ),
                })

        else:
            # FAZ 39 — Müşteri modu yön/bakiye uyumluluk guard'ı.
            # Sepet modunda her process_no için aynı kontrol vardı
            # (line ~1559); müşteri modunda eksikti — örneğin müşteri
            # BORÇLU iken "Ödeme Yap (Kasadan ÇIKIŞ)" seçilirse iade
            # niyetli olmayan bir DEBT yazımı bakiye yönü ile çakışıyordu.
            # Bakiyenin yönü ile direction uyuşmuyorsa açık hata döndür.
            current_balance_hs = customer.balance_hs or Decimal('0')
            if direction == 'payment_to_customer' and current_balance_hs > Decimal('0.001'):
                return JsonResponse({
                    'result': False,
                    'error_msg': (
                        'Müşteri borçlu durumda; "Ödeme Yap (Kasadan ÇIKIŞ)" '
                        'yönü uygun değildir. Borç tahsilatı için '
                        '"Tahsilat Yap (Kasaya GİRİŞ)" yönünü seçin. İade '
                        'işlemi için ürün bazlı PURCHASE/RETURN akışı '
                        'kullanılmalıdır.'
                    ),
                })
            if direction == 'collection' and current_balance_hs < Decimal('-0.001'):
                return JsonResponse({
                    'result': False,
                    'error_msg': (
                        'Müşteri alacaklı durumda; "Tahsilat Yap (Kasaya '
                        'GİRİŞ)" yönü uygun değildir. Alacağı kapatmak için '
                        '"Ödeme Yap (Kasadan ÇIKIŞ)" yönünü seçin.'
                    ),
                })

            process = Process()
            process.customer = customer
            process.store = store
            process.employee = user
            process.process_type = 'FAST_PROCESS'
            process.transaction_type = 'PAYMENT'
            process.date = timezone.now()
            process.process_no = generate_process_no()
            process.amount = amount_eur
            process.price_hs = amount_hs
            process.hs_rate_sale_eur = rate_val
            process.save()

            # FAZ 14 Bug 6 fix: Payment.clean() tetiklenerek yazılır.
            payment = _create_payment_with_clean(
                process_no=process.process_no,
                amount=amount_eur,
                bank_account=bank_account,
                is_output=(direction == 'payment_to_customer'),
                reference=ref_text[:99],
            )
            # FAZ 14 Bug 4 fix: CashboxLedger.
            _write_cashbox_movement(
                payment=payment,
                bank_account=bank_account,
                store=store,
                direction=direction,
                audit=audit,
                description=ledger_desc,
            )
            # FAZ 14 Bug 5 fix: Tek satır CustomerLedger (zaten tek
            # process_no var, bu kolda toplu/per-process ayrımı yok).
            # FAZ 51 (R-01): payment bağlantısı REVERSAL'da kasa
            # simetrisini garanti eder.
            _write_customer_ledger_for_unit(
                customer=customer,
                store=store,
                direction=direction,
                currency_type=currency_type,
                alloc_hs=amount_hs,
                alloc_tl=amount_eur,
                rate_val=rate_val,
                process_no=process.process_no,
                audit=audit,
                description=ledger_desc,
                related_payment=payment,
            )

            total_paid_hs = amount_hs

        # Statik alanları balance_hs property'sinden türeterek senkronize et.
        # Bu adım eski raporları/ekranları bozmamak için tutulur; ama artık
        # gerçek veri kaynağı CustomerLedger'dir.
        balance = customer.balance_hs or Decimal('0')
        customer.receivable_hs = balance if balance > Decimal('0') else Decimal('0')
        customer.payable_hs = (-balance) if balance < Decimal('0') else Decimal('0')
        customer.save(update_fields=['receivable_hs', 'payable_hs'])

        target_product = None
        quantity_change = Decimal('0')

        if currency_type == 'HS':
            target_product = Products.objects.filter(
                name="Has Altın",
                is_deleted=False
            ).first()

            if not target_product:
                target_product = Products.objects.filter(name__icontains="Has Altın", is_deleted=False).first()

            quantity_change = total_paid_hs

        elif currency_type == 'TL':
            target_product = Products.objects.filter(
                name="TRY - Türk Lirası",
                is_deleted=False
            ).first()

            if not target_product:
                target_product = Products.objects.filter(name__icontains="TRY", is_deleted=False).first()

            quantity_change = amount_eur

        if target_product:
            # FAZ 4: StockService ile stok güncelleme
            _hs_rate_eur = Decimal('0')
            try:
                _hs_data = PriceService.get_price('GOLD_24K')
                _hs_rate_eur = Decimal(str(_hs_data.get('buy_tl', Decimal('0'))))
            except Exception:
                pass

            inv_process_no = selected_process_nos[0] if selected_process_nos else process.process_no

            qty_gram = quantity_change
            qty_pieces = int(quantity_change) if currency_type == 'TL' else 0

            snap = StockSnapshot.objects.filter(product=target_product, store=store).first()
            unit_cost_hs = snap.weighted_avg_cost_hs if snap else Decimal('0')
            unit_cost_eur = snap.weighted_avg_cost_eur if snap else Decimal('0')

            if direction == 'payment_to_customer':
                # Müşteriye ödeme → stoktan çıkış
                StockService.record_exit(
                    product=target_product,
                    store=store,
                    quantity_gram=qty_gram,
                    quantity_pieces=qty_pieces,
                    reason=StockLedger.Reason.SALE,
                    ref_type='debt_collection',
                    ref_id=f"debt_{inv_process_no}",
                    unit_cost_hs=unit_cost_hs,
                    unit_cost_eur=unit_cost_eur,
                    hs_rate_eur=_hs_rate_eur,
                    user=user,
                    notes=f"Borç/Alacak İşlemi ({desc_prefix})",
                )
            else:
                # Tahsilat → stoğa giriş
                StockService.record_entry(
                    product=target_product,
                    store=store,
                    quantity_gram=qty_gram,
                    quantity_pieces=qty_pieces,
                    reason=StockLedger.Reason.PURCHASE,
                    ref_type='debt_collection',
                    ref_id=f"debt_{inv_process_no}",
                    unit_cost_hs=unit_cost_hs,
                    unit_cost_eur=unit_cost_eur,
                    hs_rate_eur=_hs_rate_eur,
                    user=user,
                    notes=f"Borç/Alacak İşlemi ({desc_prefix})",
                )

        return JsonResponse({'result': True, 'msg': 'İşlem, bakiye ve stok başarıyla güncellendi.'})

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': f'Hata oluştu: {str(e)}'})
