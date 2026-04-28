from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.db.models import Q
# apps/masak/views.py
# apps/masak/views.py içine ekleyin

from django.shortcuts import render, redirect
from django.contrib import messages
from .utils import import_masak_csv
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from apps.customers.models import Customers
from .models import MasakBlacklist, MasakQueryLog, MasakOfficialList
from apps.masak.models import MasakBlacklist, MasakQueryLog

import base64
import uuid as _uuid_mod
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from apps.stores.models import Stores
from .forms import CustomerMasakPublicForm
from .models import CustomerMasakDeclaration


# Role decorator varsa import et, yoksa kaldırabilirsin
# from apps.roles.decorators import role_required 

@login_required(login_url='login')
def masak_index(request):
    """
    Ana panel sayfası
    """
    return render(request, 'management/masak/index.html', {
        'title': 'MASAK & Şüpheli İşlem Bildirimleri'
    })


@login_required(login_url='login')
def get_query_logs(request):
    """
    Sorgu Geçmişi (Logs) için DataTables API
    """
    try:
        draw = int(request.GET.get('draw', 1))
        start = int(request.GET.get('start', 0))
        length = int(request.GET.get('length', 10))
        search_value = (request.GET.get('search[value]', '') or '').strip()
        store = request.user.store

        qs = MasakQueryLog.objects.filter(store=store).select_related('performed_by')

        # Arama
        if search_value:
            qs = qs.filter(
                Q(queried_name__icontains=search_value) |
                Q(queried_tckn__icontains=search_value) |
                Q(result_message__icontains=search_value)
            )

        total_records = qs.count()  # Filtre öncesi toplam (basitleştirildi)
        filtered_records = qs.count()

        # Sıralama (Varsayılan tarih desc)
        qs = qs.order_by('-created_at')

        if length != -1:
            qs = qs[start:start + length]

        data = []
        for log in qs:
            # Badge rengi
            status_html = ""
            if log.result_status == 'SUSPICIOUS':
                status_html = '<span class="badge badge-light-danger">ŞÜPHELİ</span>'
            else:
                status_html = '<span class="badge badge-light-success">TEMİZ</span>'

            data.append({
                'created_at': log.created_at.strftime('%d.%m.%Y %H:%M'),
                'performed_by': log.performed_by.get_full_name() if log.performed_by else '-',
                'queried_name': log.queried_name,
                'queried_tckn': log.queried_tckn or '-',
                'status': status_html,
                'message': log.result_message or '-'
            })

        return JsonResponse({
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data
        })

    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


@login_required(login_url='login')
def get_blacklist(request):
    """
    Kara Liste (Blacklist) için DataTables API
    """
    try:
        draw = int(request.GET.get('draw', 1))
        start = int(request.GET.get('start', 0))
        length = int(request.GET.get('length', 10))
        search_value = (request.GET.get('search[value]', '') or '').strip()
        store = request.user.store

        qs = MasakBlacklist.objects.filter(store=store, is_active=True)

        if search_value:
            qs = qs.filter(
                Q(full_name__icontains=search_value) |
                Q(identification_number__icontains=search_value)
            )

        total_records = qs.count()
        filtered_records = qs.count()
        qs = qs.order_by('-created_at')

        if length != -1:
            qs = qs[start:start + length]

        data = []
        for item in qs:
            data.append({
                'id': str(item.id),
                'full_name': item.full_name,
                'identification_number': item.identification_number,
                'reason': item.reason,
                'created_at': item.created_at.strftime('%d.%m.%Y')
            })

        return JsonResponse({
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data
        })

    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
def add_blacklist_item(request):
    store = request.user.store
    full_name = request.POST.get('full_name')
    tckn = request.POST.get('identification_number')
    reason = request.POST.get('reason')

    if not tckn:
        return JsonResponse({'error': True, 'error_msg': 'TCKN zorunludur!'}, status=400)

    # Var mı kontrolü
    if MasakBlacklist.objects.filter(store=store, identification_number=tckn).exists():
        return JsonResponse({'error': True, 'error_msg': 'Bu TCKN zaten kara listede!'}, status=400)

    MasakBlacklist.objects.create(
        store=store,
        full_name=full_name,
        identification_number=tckn,
        reason=reason,
        created_by=request.user
    )
    return JsonResponse({'result': True})


@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
def delete_blacklist_item(request):
    ids = request.POST.getlist('ids[]') or []
    store = request.user.store
    try:
        MasakBlacklist.objects.filter(id__in=ids, store=store).delete()
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


@login_required
@require_POST
def check_customer_risk(request):
    """
    1. Mağaza Özel Kara Listesi
    2. MASAK Resmi Listesi (MasakOfficialList)
    """
    customer_id = request.POST.get('customer_id')

    if not customer_id:
        return JsonResponse({'status': 'error', 'message': 'Müşteri seçilmedi.'})

    try:
        customer = Customers.objects.get(id=customer_id)
    except Customers.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Müşteri bulunamadı.'})

    current_store = request.user.store

    # Müşteri verileri
    tckn = (customer.identification_number or '').strip()
    # İsim parçalarını birleştir ve büyük harfe çevir (normalize et)
    full_name_raw = f"{customer.first_name} {customer.last_name}"
    full_name = full_name_raw.strip()

    is_suspicious = False
    source = "Clean"
    message = "Temiz"
    reason = ""
    match_detail = {}

    # --- 1. ADIM: MAĞAZA ÖZEL KARA LİSTESİ ---
    if tckn:
        local_match = MasakBlacklist.objects.filter(
            store=current_store,
            identification_number=tckn,
            is_active=True
        ).first()

        if local_match:
            is_suspicious = True
            source = "Mağaza Kara Listesi"
            message = "DİKKAT! Bu kişi mağazanızın kara listesinde."
            reason = local_match.reason

    # --- 2. ADIM: MASAK RESMİ LİSTE KONTROLÜ ---
    # Eğer yerelde temizse ve TCKN varsa resmi listeye bak
    if not is_suspicious:
        official_match = None

        # A) TCKN ile Kesin Eşleşme (En Güvenilir)
        if tckn and len(tckn) > 5:  # Çok kısa no ile arama yapma
            # identity_info alanı metin olduğu için "contains" kullanıyoruz çünkü
            # csv'de "12345678901 - Pasaport: A123" gibi karmaşık veriler olabilir.
            official_match = MasakOfficialList.objects.filter(
                identity_info__contains=tckn
            ).first()

        # B) İsim ile Eşleşme (Eğer TCKN yoksa veya TCKN ile bulunamadıysa)
        # Not: İsim benzerliği riskli olabilir, o yüzden tam eşleşme arıyoruz.
        if not official_match and full_name:
            # İsim + Soyad tam eşleşme (Case insensitive)
            official_match = MasakOfficialList.objects.filter(
                full_name__iexact=full_name
            ).first()

        if official_match:
            is_suspicious = True

            source_map = {
                'BMGK': 'BMGK (Birleşmiş Milletler) Listesi',
                'FOREIGN': 'Yabancı Ülke Talebi',
                'INTERNAL': 'MASAK / İçişleri Bakanlığı Terör Listesi'
            }
            src_text = source_map.get(official_match.source_type, 'Resmi Liste')

            source = f"RESMİ LİSTE ({src_text})"
            message = "YASAL UYARI: Müşteri MASAK dondurma listesinde bulundu!"
            reason = f"Örgüt/Sebep: {official_match.organization or 'Belirtilmemiş'}\nDoğum Tarihi: {official_match.birth_date or '-'}"

    # --- 3. ADIM: LOGLAMA ---
    MasakQueryLog.objects.create(
        store=current_store,
        performed_by=request.user,
        customer=customer,
        queried_tckn=tckn,
        queried_name=full_name_raw,
        result_status='SUSPICIOUS' if is_suspicious else 'CLEAN',
        result_message=f"[{source}] {reason if reason else message}"
    )

    if is_suspicious:
        return JsonResponse({
            'status': 'suspicious',
            'title': 'RİSKLİ MÜŞTERİ TESPİTİ',
            'message': message,
            'reason': reason,
            'source': source
        })
    else:
        return JsonResponse({'status': 'clean'})


@login_required
def upload_masak_data(request):
    """
    Adminlerin Excel/CSV yükleyip veritabanını güncellediği sayfa
    """
    if not request.user.is_superuser:  # Sadece adminler
        return redirect('dashboard:index')

    if request.method == 'POST':
        file_a = request.FILES.get('file_a')  # BMGK
        file_b = request.FILES.get('file_b')  # Foreign
        file_c = request.FILES.get('file_c')  # Internal

        total_imported = 0

        try:
            if file_a:
                total_imported += import_masak_csv(file_a, 'BMGK')
            if file_b:
                total_imported += import_masak_csv(file_b, 'FOREIGN')
            if file_c:
                total_imported += import_masak_csv(file_c, 'INTERNAL')

            messages.success(request, f"Başarıyla {total_imported} kayıt güncellendi/eklendi.")
        except Exception as e:
            messages.error(request, f"Hata oluştu: {str(e)}")

        return redirect('masak:upload_page')

    return render(request, 'management/masak/upload.html')


# =====================================================================
# MASAK Müşteri Tanı Formu — Public (auth'suz) + Private (print/QR)
# =====================================================================

MASAK_THROTTLE_LIMIT = 3
MASAK_THROTTLE_WINDOW = 60  # saniye


def _masak_get_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _masak_throttled(ip):
    key = f'masak_pub_throttle_{ip}'
    count = cache.get(key, 0)
    if count >= MASAK_THROTTLE_LIMIT:
        return True
    cache.set(key, count + 1, MASAK_THROTTLE_WINDOW)
    return False


def _masak_base64_to_file(data_url, name_prefix):
    if not data_url or ';base64,' not in data_url:
        return None
    try:
        header, b64 = data_url.split(';base64,', 1)
        ext = header.split('/')[-1].lower()
        if ext not in ('jpeg', 'jpg', 'png', 'webp'):
            ext = 'jpg'
        raw = base64.b64decode(b64)
        if len(raw) > 5 * 1024 * 1024:  # 5 MB
            return None
        return ContentFile(raw, name=f'{name_prefix}_{_uuid_mod.uuid4().hex}.{ext}')
    except Exception:
        return None


def _masak_apply_declaration_fields(declaration, data):
    """Form cleaned_data'yı declaration objesine kopyalar (save etmez)."""
    declaration.customer_type = data.get('customer_type') or 'BIREYSEL'

    # Bireysel
    declaration.first_name = data.get('first_name') or None
    declaration.last_name = data.get('last_name') or None
    declaration.identity_number = data.get('identity_number') or None
    declaration.nationality = data.get('nationality') or None
    declaration.document_type = data.get('document_type') or None
    declaration.document_number = data.get('document_number') or None
    declaration.birth_place = data.get('birth_place') or None
    declaration.birth_date = data.get('birth_date') or None
    declaration.occupation = data.get('occupation') or None
    declaration.mother_name = data.get('mother_name') or None
    declaration.father_name = data.get('father_name') or None

    # Kurumsal
    declaration.company_title = data.get('company_title') or None
    declaration.tax_office = data.get('tax_office') or None
    declaration.tax_number = data.get('tax_number') or None
    declaration.mersis_number = data.get('mersis_number') or None
    declaration.trade_registry_number = data.get('trade_registry_number') or None
    declaration.activity_field = data.get('activity_field') or None
    declaration.company_address = data.get('company_address') or None
    declaration.rep_first_name = data.get('rep_first_name') or None
    declaration.rep_last_name = data.get('rep_last_name') or None
    declaration.rep_identity_number = data.get('rep_identity_number') or None
    declaration.rep_title = data.get('rep_title') or None
    declaration.beneficial_owner_name = data.get('beneficial_owner_name') or None
    declaration.beneficial_owner_identity = data.get('beneficial_owner_identity') or None
    declaration.beneficial_owner_share = data.get('beneficial_owner_share') or None

    # Ortak
    if declaration.customer_type == 'KURUMSAL':
        declaration.address = data.get('company_address') or None
    else:
        declaration.address = data.get('address') or None
    declaration.email = data.get('email') or None
    declaration.phone = data.get('phone') or None

    # İzinler
    declaration.consent_kvkk = data.get('consent_kvkk', False)
    declaration.consent_acik_riza = data.get('consent_acik_riza', False)
    declaration.consent_iys_sms = data.get('consent_iys_sms', False)
    declaration.consent_iys_email = data.get('consent_iys_email', False)
    declaration.consent_iys_call = data.get('consent_iys_call', False)


@csrf_protect
@require_http_methods(['GET', 'POST'])
def masak_public_form(request, store_token):
    """QR ile açılan public MASAK formu. Auth GEREKTİRMEZ."""
    store = get_object_or_404(Stores, masak_public_token=store_token, is_active=True, is_deleted=False)

    if request.method == 'POST':
        ip = _masak_get_ip(request)
        if _masak_throttled(ip):
            messages.error(request, 'Çok fazla deneme yapıldı. Lütfen bir dakika sonra tekrar deneyiniz.')
            return redirect('masak:public_form', store_token=store.masak_public_token)

        form = CustomerMasakPublicForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            ctype = data.get('customer_type') or 'BIREYSEL'

            front_file = _masak_base64_to_file(
                data.get('id_front_image_data') or data.get('id_front_base64'),
                'id_front'
            ) or data.get('id_front_image_file')
            back_file = _masak_base64_to_file(
                data.get('id_back_image_data') or data.get('id_back_base64'),
                'id_back'
            ) or data.get('id_back_image_file')

            try:
                with transaction.atomic():
                    has_tax_number_attr = hasattr(Customers, 'tax_number')
                    has_is_corporate_attr = hasattr(Customers, 'is_corporate')

                    if ctype == 'KURUMSAL':
                        vkn = (data.get('tax_number') or '').strip()
                        existing = None
                        if has_tax_number_attr and vkn:
                            existing = Customers.objects.filter(
                                store=store,
                                tax_number=vkn,
                                is_deleted=False,
                            ).first()
                        if not existing and vkn:
                            # Fallback: identification_number alanında VKN tutuluyor olabilir
                            existing = Customers.objects.filter(
                                store=store,
                                identification_number=vkn,
                                is_deleted=False,
                            ).first()

                        company_title = data.get('company_title') or ''
                        phone = data.get('phone') or ''
                        email = data.get('email') or ''
                        company_address = data.get('company_address') or ''

                        if existing:
                            customer = existing
                            # Unvan alanları fallback (first_name/last_name)
                            customer.first_name = company_title[:100] if company_title else customer.first_name
                            customer.last_name = ''
                            if has_tax_number_attr:
                                setattr(customer, 'tax_number', vkn)
                            else:
                                customer.identification_number = vkn[:20] if vkn else customer.identification_number
                            if has_is_corporate_attr:
                                setattr(customer, 'is_corporate', True)
                            customer.phone = phone
                            customer.email = email
                            customer.address = company_address
                            customer.save()
                        else:
                            create_kwargs = dict(
                                first_name=company_title[:100],
                                last_name='',
                                phone=phone,
                                email=email,
                                address=company_address,
                                is_active=True,
                                is_deleted=False,
                            )
                            if has_tax_number_attr:
                                create_kwargs['tax_number'] = vkn
                                create_kwargs['identification_number'] = vkn[:20]
                            else:
                                create_kwargs['identification_number'] = vkn[:20]
                            if has_is_corporate_attr:
                                create_kwargs['is_corporate'] = True
                            customer = Customers.objects.create(**create_kwargs)
                            customer.store.add(store)

                    else:  # BIREYSEL
                        existing = Customers.objects.filter(
                            store=store,
                            identification_number=data['identity_number'],
                            is_deleted=False,
                        ).first()

                        if existing:
                            customer = existing
                            customer.first_name = data['first_name']
                            customer.last_name = data['last_name']
                            customer.phone = data.get('phone') or ''
                            customer.email = data.get('email') or ''
                            customer.address = data.get('address') or ''
                            if has_is_corporate_attr:
                                setattr(customer, 'is_corporate', False)
                            customer.save()
                        else:
                            create_kwargs = dict(
                                first_name=data['first_name'],
                                last_name=data['last_name'],
                                identification_number=(data['identity_number'] or '')[:11],
                                phone=data.get('phone') or '',
                                email=data.get('email') or '',
                                address=data.get('address') or '',
                                is_active=True,
                                is_deleted=False,
                            )
                            if has_is_corporate_attr:
                                create_kwargs['is_corporate'] = False
                            customer = Customers.objects.create(**create_kwargs)
                            customer.store.add(store)

                    declaration, _created = CustomerMasakDeclaration.objects.get_or_create(
                        customer=customer,
                        defaults={'store': store, 'customer_type': ctype}
                    )
                    declaration.store = store

                    _masak_apply_declaration_fields(declaration, data)

                    if ctype == 'BIREYSEL':
                        if front_file:
                            declaration.id_front_image.save(front_file.name, front_file, save=False)
                        if back_file:
                            declaration.id_back_image.save(back_file.name, back_file, save=False)

                    declaration.ip_address = ip or None
                    declaration.user_agent = request.META.get('HTTP_USER_AGENT', '')[:2000]
                    declaration.save()

                return redirect('masak:public_success', store_token=store.masak_public_token)
            except Exception as exc:
                messages.error(request, f'Kayıt sırasında bir hata oluştu: {exc}')
    else:
        form = CustomerMasakPublicForm()

    return render(request, 'masak/public/form.html', {'form': form, 'store': store})


@require_http_methods(['GET'])
def masak_public_success(request, store_token):
    store = get_object_or_404(Stores, masak_public_token=store_token)
    return render(request, 'masak/public/success.html', {'store': store})


@login_required(login_url='login')
def masak_print_view(request, customer_id):
    """Doldurulmuş MASAK formunu A4 çıktı görünümünde render eder."""
    customer = get_object_or_404(
        Customers.objects.prefetch_related('store'),
        id=customer_id,
        is_deleted=False,
    )
    # Mağaza erişim kontrolü (tenant izolasyonu)
    user_store = request.user.store
    if not customer.store.filter(id=user_store.id).exists():
        messages.error(request, 'Bu müşteri mağazanıza ait değil.')
        return redirect('customers:index')

    declaration = getattr(customer, 'masak_declaration', None)
    if declaration is None:
        messages.warning(request, 'Bu müşterinin henüz MASAK beyan formu bulunmuyor.')
        return redirect('customers:index')

    return render(request, 'management/masak/print.html', {
        'customer': customer,
        'declaration': declaration,
        'store': user_store,
        'auto_print': request.GET.get('auto') == '1',
    })


@login_required(login_url='login')
def masak_qr_modal(request):
    """Kuyumcunun mağazası için public MASAK form QR'ını gösteren modal içeriği."""
    store = request.user.store
    # masak_public_token yoksa (eski kayıt) otomatik üret
    if not getattr(store, 'masak_public_token', None):
        store.masak_public_token = _uuid_mod.uuid4()
        store.save(update_fields=['masak_public_token'])

    public_url = request.build_absolute_uri(
        reverse('masak:public_form', kwargs={'store_token': store.masak_public_token})
    )
    return render(request, 'management/masak/qr_modal.html', {
        'store': store,
        'public_url': public_url,
    })


@login_required(login_url='login')
@require_POST
def masak_toggle_iys_consent(request):
    """
    Kuyumcunun müşteri detay/güncelleme ekranından İYS (Ticari İleti) iznini
    tek bir toggle ile açıp kapatmasını sağlar.

    POST parametreleri:
        customer_id : Customers.id (UUID)
        approved    : '1' | '0' | 'true' | 'false'

    Bu endpoint, mevcut `CustomerMasakDeclaration` kaydındaki üç İYS alanını
    (sms, email, call) aynı değere set eder. Eğer müşterinin henüz bir beyan
    formu yoksa, temel alanları müşteri bilgilerinden doldurarak minimum bir
    beyan kaydı oluşturur (tüm kimlik alanları opsiyonel/boş kalır).
    """
    customer_id = request.POST.get('customer_id')
    approved_raw = (request.POST.get('approved') or '').strip().lower()
    if not customer_id:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri ID zorunludur.'}, status=400)

    approved = approved_raw in ('1', 'true', 'on', 'yes')

    try:
        customer = Customers.objects.get(
            id=customer_id,
            store=request.user.store,
            is_deleted=False,
        )
    except Customers.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri bulunamadı.'}, status=404)

    declaration = getattr(customer, 'masak_declaration', None)
    if declaration is None:
        # Minimum kayıt oluştur — sadece İYS izin durumu tutulur,
        # diğer alanlar müşteriden kopyalanır (varsa).
        declaration = CustomerMasakDeclaration(
            customer=customer,
            store=request.user.store,
            first_name=customer.first_name or '-',
            last_name=customer.last_name or '-',
            identity_number=customer.identification_number or '-',
            nationality='T.C.',
            document_type='TC',
            document_number=customer.identification_number or '-',
            birth_place='-',
            birth_date='1900-01-01',
            address=customer.address or '-',
            email=customer.email or 'bilinmiyor@example.com',
            phone=customer.phone or '-',
            occupation='-',
            mother_name='-',
            father_name='-',
            consent_kvkk=False,
            consent_acik_riza=False,
        )
        # Kimlik görselleri zorunlu olduğu için ImageField'ler boş kalmasın
        # diye bir placeholder dosyası atanmaz; DB seviyesinde null=False
        # olduğundan kullanıcı görseli daha sonra form üzerinden yüklemek
        # zorundadır. Bu endpoint yalnızca İYS toggle amaçlı kullanıldığı
        # için minimum kaydı save etmek yerine update edilen tek alanı
        # işlemek üzere farklı bir yaklaşım kullanıyoruz:
        return JsonResponse({
            'result': False,
            'error_msg': 'Bu müşterinin henüz MASAK beyan formu bulunmuyor. '
                         'Lütfen müşteriden önce MASAK formunu doldurmasını rica ediniz.'
        }, status=400)

    declaration.consent_iys_sms = approved
    declaration.consent_iys_email = approved
    declaration.consent_iys_call = approved
    declaration.save(update_fields=[
        'consent_iys_sms', 'consent_iys_email', 'consent_iys_call', 'updated_at'
    ])

    return JsonResponse({
        'result': True,
        'iys_approved': approved,
        'message': 'İYS izni güncellendi.',
    })
