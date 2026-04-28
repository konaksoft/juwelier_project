import json
import random
import string
import secrets
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404

from apps.accounts.models import *
from apps.activity_logs.views import write_log
from apps.customers.models import Customers
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


@login_required()
@role_required('CUSTOMERS_CUSTOMERS_VIEW')
def customers_view(request):
    has_product = Products.objects.filter(name__icontains='Has Altın').first()
    has_price = getattr(has_product, 'sale_price_tl', 1) if has_product else 1

    context = {
        'title': 'Müşteriler',
        'has_price': float(has_price),
        'store_masak_token': getattr(request.user.store, 'masak_public_token', ''),
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
        phone = normalize_tr_phone(raw_phone) if raw_phone else None

        if phone and (len(phone) != 10 or not phone.startswith('5')):
            return JsonResponse({'error': True, 'error_msg': 'Geçersiz telefon numarası. (5XX... formatında olmalı)'})
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

        if store_config.require_customer_tckn and not identification_number:
            return JsonResponse({
                'error': True,
                'error_msg': 'Bu mağazada T.C. / Vergi Kimlik Numarası zorunludur.'
            })

        # Kimlik numarası girildiyse algoritma + format kontrolü:
        # 11 hane → TCKN matematiksel doğrulama
        # 10 hane → VKN format kontrolü (kurumsal müşteri)
        # diğer  → reddedilir
        if identification_number:
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
    store = request.user.store
    # 'phone' ve 'tckn' alanlarını buraya ekledik
    customers = Customers.objects.filter(
        is_deleted=False,
        is_active=True,
        store=store
    ).values(
        'id',
        'first_name',
        'last_name',
        'receivable_hs',
        'payable_hs',
        'phone',  # Telefon alanı
        'identification_number'  # TC Kimlik No alanı (Modelinizdeki adı farklıysa burayı düzeltin)
    )
    return JsonResponse(list(customers), safe=False)


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

    queryset = Customers.objects.filter(
        is_deleted=False,
        store=user_store
    )

    if filter_type == 'debtors':
        queryset = queryset.filter(payable_hs__gt=F('receivable_hs'))
    elif filter_type == 'creditors':
        queryset = queryset.filter(receivable_hs__gt=F('payable_hs'))

    if masak_filter == 'filled':
        queryset = queryset.filter(masak_declaration__isnull=False)
    elif masak_filter == 'missing':
        queryset = queryset.filter(masak_declaration__isnull=True)

    queryset = queryset.values('id', 'store__store_id', 'first_name', 'last_name',
                               'identification_number', 'customer_number',
                               'phone', 'gender', 'email', 'address', 'is_active', 'receivable_hs', 'payable_hs',
                               'masak_declaration__id')

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(first_name__icontains=search_value)

    count = queryset.count()

    if str(length) == '-1':
        queryset = queryset.order_by(order_column)
    else:
        queryset = queryset.order_by(order_column)[start:start + length]

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": list(queryset)
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

    payments = (
        Payment.objects
        .filter(process_no__in=Process.objects.filter(customer=customer).values_list('process_no', flat=True))
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

    receivable_hs = Decimal(customer.receivable_hs or 0)
    payable_hs = Decimal(customer.payable_hs or 0)
    balance_hs = receivable_hs - payable_hs

    has_product = Products.objects.filter(name__icontains='Has Altın').first()
    has_price_val = getattr(has_product, 'sale_price_tl', 1) if has_product else 1
    has_price = Decimal(str(has_price_val))

    balance_tl = balance_hs * has_price

    if balance_hs > 0:
        balance_type = 'credit'
    elif balance_hs < 0:
        balance_type = 'debt'
    else:
        balance_type = 'zero'

    context = {
        'title': f'{(customer.first_name or "")} {(customer.last_name or "")}'.strip() or 'Müşteri',
        'customer': customer,
        'processes': processes,
        'payments': payments,
        'purchase_processes': purchase_processes,
        'sale_processes': sale_processes,
        'repairs': repairs,
        'balance_hs': abs(balance_hs),
        'balance_tl': abs(balance_tl),
        'balance_type': balance_type,
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
        balance_tl = net_amt - paid_amt

        if pno not in seen_pnos:
            seen_pnos.add(pno)
            show_checkbox = True

            # KUYUMCU MATEMATİĞİ: Bakiye TL / Net TL oranı kadar Has kalmıştır
            basket_remaining_hs = Decimal('0.000')
            if net_amt != Decimal('0'):
                basket_remaining_hs = (balance_tl / net_amt) * net_hs
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

        amount_tl = Decimal('0')
        amount_hs = Decimal('0')

        if currency_type == 'TL':
            amount_tl = amount_val
            if rate_val > 0:
                amount_hs = amount_tl / rate_val
        else:
            amount_hs = amount_val
            amount_tl = amount_hs * rate_val

        amount_tl = amount_tl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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

        if selected_process_nos:
            remaining_to_distribute_hs = amount_hs
            total_paid_hs = Decimal('0')

            unique_pnos = list(set(selected_process_nos))

            for pno in unique_pnos:
                if remaining_to_distribute_hs <= Decimal('0.001'):
                    break

                procs = Process.objects.filter(process_no=pno, customer=customer, store=store, is_deleted=False)
                if not procs.exists(): continue

                # Sepetin gerçek TL ve Has Değerleri
                sales_total = sum(
                    Decimal(str(p.amount or 0)) for p in procs if p.transaction_type in ('SALE', 'ORDER_IN'))
                purchases_total = sum(Decimal(str(p.amount or 0)) for p in procs if
                                      p.transaction_type in ('PURCHASE', 'RETURN', 'STOCK_IN'))
                net_basket_tl = sales_total - purchases_total

                sales_hs = sum(
                    Decimal(str(p.price_hs or 0)) for p in procs if p.transaction_type in ('SALE', 'ORDER_IN'))
                purchases_hs = sum(Decimal(str(p.price_hs or 0)) for p in procs if
                                   p.transaction_type in ('PURCHASE', 'RETURN', 'STOCK_IN'))
                net_basket_hs = sales_hs - purchases_hs

                pays = Payment.objects.filter(process_no=pno)
                paid_in = sum(Decimal(str(p.amount or 0)) for p in pays if not p.is_output)
                paid_out = sum(Decimal(str(p.amount or 0)) for p in pays if p.is_output)
                net_paid_tl = paid_in - paid_out

                balance_tl = net_basket_tl - net_paid_tl

                if abs(balance_tl) <= Decimal('0.01'):
                    continue

                    # Kalan Has Altın (Oransal)
                basket_hs = Decimal('0.000')
                if net_basket_tl != Decimal('0'):
                    basket_hs = (balance_tl / net_basket_tl) * net_basket_hs
                else:
                    basket_hs = net_basket_hs

                basket_hs_abs = abs(basket_hs)

                is_customer_debt = (balance_tl > 0)
                if direction == 'collection' and not is_customer_debt:
                    continue
                if direction == 'payment_to_customer' and is_customer_debt:
                    continue

                # Kapatılacak Has Miktarı
                alloc_hs = min(remaining_to_distribute_hs, basket_hs_abs)
                alloc_hs = alloc_hs.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

                # Has'tan TL'ye Zımni (Oransal) Dönüşüm
                if basket_hs_abs > Decimal('0'):
                    alloc_tl = (alloc_hs / basket_hs_abs) * abs(balance_tl)
                else:
                    alloc_tl = alloc_hs * rate_val

                alloc_tl = alloc_tl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                Payment.objects.create(
                    process_no=pno,
                    payment_type='CASH',
                    amount=alloc_tl,
                    date=timezone.now(),
                    is_output=(direction == 'payment_to_customer'),
                    reference=f"{desc_prefix} ({currency_type}) - {description}"[:99]
                )

                remaining_to_distribute_hs -= alloc_hs
                total_paid_hs += alloc_hs

            total_paid_hs = total_paid_hs.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        else:
            process = Process()
            process.customer = customer
            process.store = store
            process.employee = user
            process.process_type = 'FAST_PROCESS'
            process.transaction_type = 'PAYMENT'
            process.date = timezone.now()
            process.process_no = generate_process_no()
            process.amount = amount_tl
            process.price_hs = amount_hs
            process.hs_rate_sale_tl = rate_val
            process.save()

            payment = Payment()
            payment.process_no = process.process_no
            payment.payment_type = 'CASH'
            payment.amount = amount_tl
            payment.date = timezone.now()
            payment.is_output = (direction == 'payment_to_customer')
            payment.reference = f"{desc_prefix} ({currency_type}) - {description}"[:99]
            payment.save()

            total_paid_hs = amount_hs

        current_payable = customer.payable_hs or Decimal('0')
        current_receivable = customer.receivable_hs or Decimal('0')

        if direction == 'collection':
            if current_payable >= total_paid_hs:
                customer.payable_hs = current_payable - total_paid_hs
            else:
                remaining = total_paid_hs - current_payable
                customer.payable_hs = Decimal('0')
                customer.receivable_hs = current_receivable + remaining
        else:
            if current_receivable >= total_paid_hs:
                customer.receivable_hs = current_receivable - total_paid_hs
            else:
                remaining = total_paid_hs - current_receivable
                customer.receivable_hs = Decimal('0')
                customer.payable_hs = current_payable + remaining

        customer.save()

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

            quantity_change = amount_tl

        if target_product:
            # FAZ 4: StockService ile stok güncelleme
            _hs_rate_tl = Decimal('0')
            try:
                _hs_data = PriceService.get_price('GOLD_24K')
                _hs_rate_tl = Decimal(str(_hs_data.get('buy_tl', Decimal('0'))))
            except Exception:
                pass

            inv_process_no = selected_process_nos[0] if selected_process_nos else process.process_no

            qty_gram = quantity_change
            qty_pieces = int(quantity_change) if currency_type == 'TL' else 0

            snap = StockSnapshot.objects.filter(product=target_product, store=store).first()
            unit_cost_hs = snap.weighted_avg_cost_hs if snap else Decimal('0')
            unit_cost_tl = snap.weighted_avg_cost_tl if snap else Decimal('0')

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
                    unit_cost_tl=unit_cost_tl,
                    hs_rate_tl=_hs_rate_tl,
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
                    unit_cost_tl=unit_cost_tl,
                    hs_rate_tl=_hs_rate_tl,
                    user=user,
                    notes=f"Borç/Alacak İşlemi ({desc_prefix})",
                )

        return JsonResponse({'result': True, 'msg': 'İşlem, bakiye ve stok başarıyla güncellendi.'})

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': f'Hata oluştu: {str(e)}'})
