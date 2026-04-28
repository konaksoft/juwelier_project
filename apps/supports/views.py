import secrets
from django.template.loader import render_to_string
from asn1crypto.core import Null
from celery.worker.consumer.mingle import exception
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404

from apps.accounts.models import *
from apps.activity_logs.views import write_log
from apps.customers.models import Customers
from apps.supports.models import *
from apps.process.models import Process, Payment
from apps.roles.decorators import role_required
from apps.settings.send_mail import *
from django.db.models import Count, Q, Case, When, Value, BooleanField, Subquery, OuterRef
from django.db.models import Sum
from django.db.models import Exists
from datetime import datetime, timedelta

from apps.whatsapp.services import wa_preflight, send_whatsapp_template_guarded


def _is_support_admin(u):
    return u.is_superuser


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


def fmt_ts(dt):
    if not dt: return ''
    return dt.strftime('%d.%m.%Y %H:%M')


@login_required()
def supports_view(request):
    from apps.supports.models import TrainingVideo
    training_videos = TrainingVideo.objects.filter(is_active=True).order_by('module_name', 'order')

    context = {
        'title': 'Çözüm Merkezi',
        "staff": request.user.is_staff,
        'training_videos': training_videos,
    }

    write_log(request, 'Çözüm Merkezi', 'Çözüm Merkezi Görüntülendi.')
    return render(request, 'management/supports/index.html', context)


@login_required()
@user_passes_test(_is_support_admin)
def supports_admin_view(request):
    context = {
        'title': 'Müşteriler',
    }
    write_log(request, 'Müşteriler', 'Müşteriler Görüntülendi.')
    return render(request, 'management/supports/index-admin.html', context)


@login_required()
def supports_detail(request, id):
    support_obj = get_object_or_404(PersonelSupport, id=id)

    if not request.user.is_superuser:
        RequestMessage.objects.filter(
            request_id=id,
            is_new_message=True
        ).exclude(
            sender_id=request.user.id
        ).update(is_new_message=False)

    messages = RequestMessage.objects.filter(request_id=id).order_by("created_at").values("sender__username",
                                                                                          "created_at",
                                                                                          "request",
                                                                                          "request__status",
                                                                                          "sender__store_id"
                                                                                          , "message", "attachment",
                                                                                          "sender_id",
                                                                                          "is_internal_log")

    sessions = []
    total_minutes = 0
    if request.user.is_staff or request.user.is_superuser:
        raw_sessions = SupportWorkSession.objects.filter(request_id=id).order_by('start_time')
        for s in raw_sessions:
            duration = s.duration
            total_minutes += duration
            sessions.append({
                'start': s.start_time,
                'end': s.end_time,
                'duration': round(duration, 2),
                'is_active': s.end_time is None
            })

    hours = int(total_minutes // 60)
    minutes = int(total_minutes % 60)
    total_time_str = f"{hours}s {minutes}dk"

    context = {
        'messagesD': messages,
        'support': support_obj,
        'support_id': messages.first().get("request") if messages else id,
        'user_id': request.user.id,
        'statusD': support_obj.status,
        'is_superuser': request.user.is_superuser,
        'staff': request.user.is_staff,
        'work_sessions': sessions,
        'total_time': total_time_str
    }

    return render(request, 'management/supports/index-detail.html', context)


@login_required()
def add_supports(request):
    context = {
        'title': 'Talep Ekle',
    }

    if request.method == 'POST':
        category_val = request.POST.get('category')
        title = request.POST.get('title')
        message = request.POST.get('message')
        file = request.FILES.get("file")

        try:
            with transaction.atomic():
                perRequest = PersonelSupport()
                perRequest.personel_request = request.user
                perRequest.title = title
                perRequest.category = category_val
                perRequest.status = SupportStatus.PENDING
                perRequest.save()
                requestMessage = RequestMessage()
                requestMessage.request = perRequest
                requestMessage.sender = request.user
                requestMessage.message = message

                if file:
                    requestMessage.attachment = file

                requestMessage.save()

            return JsonResponse({'result': True})

        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})

    return render(request, 'your_template_path.html', context)


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


@login_required
def get_all(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()

    filter_type = request.GET.get('filter', 'all')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')

    qs = PersonelSupport.objects.filter(is_deleted=False)

    if request.user.is_staff and not request.user.is_superuser:
        qs = qs.filter(
            Q(assigned_staff=request.user) | Q(assigned_staff__isnull=True)
        )
    elif not request.user.is_staff:
        qs = qs.filter(personel_request=request.user)

    if start_date and end_date:
        try:
            sd = datetime.strptime(start_date, '%Y-%m-%d')
            ed = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            qs = qs.filter(created_at__range=[sd, ed])
        except ValueError:
            pass

    if filter_type == 'wait':
        qs = qs.filter(status__in=[SupportStatus.PENDING, SupportStatus.ASSIGNED, SupportStatus.WAITING_CUSTOMER])
    elif filter_type == 'start':
        qs = qs.filter(status__in=[SupportStatus.IN_PROGRESS, SupportStatus.STOPPED])
    elif filter_type == 'result':
        qs = qs.filter(status__in=[SupportStatus.RESOLVED, SupportStatus.CANCELLED])

    if search_value:
        qs = qs.filter(
            Q(title__icontains=search_value) |
            Q(ticket_no__icontains=search_value) |
            Q(personel_request__first_name__icontains=search_value) |
            Q(personel_request__last_name__icontains=search_value)
        )

    total_records = qs.count()
    qs = qs.order_by('-created_at')[start:start + length]

    data = []
    for item in qs:
        display_no = item.ticket_no if item.ticket_no else str(item.id)[:6]

        data.append({
            'id': str(item.id),
            'ticket_no': display_no,
            'status': item.status,
            'personel_request__store__store_id': item.personel_request.store.store_id if item.personel_request.store else "-",
            'personel_request__username': item.personel_request.username,
            'title': item.title,
            'priority': item.priority,
            'category': item.category,
            'created_at': item.created_at,
            'assigned_staff__username': item.assigned_staff.get_full_name() if item.assigned_staff else None,
            'is_self_assigned': True if item.assigned_staff == request.user else False,
            'unread_count': 0
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total_records,
        "recordsFiltered": total_records,
        "data": data
    })


# apps/supports/views.py içerisindeki get_all_admin fonksiyonu

@login_required
def get_all_admin(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()

    filter_type = request.GET.get('filter', 'all')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    filter_user = request.GET.get('filter_user')

    qs = PersonelSupport.objects.filter(is_deleted=False)

    if start_date and end_date:
        try:
            sd = datetime.strptime(start_date, '%Y-%m-%d')
            ed = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            qs = qs.filter(created_at__range=[sd, ed])
        except ValueError:
            pass

    if filter_user:
        qs = qs.filter(assigned_staff__id=filter_user)

    if filter_type == 'wait':
        qs = qs.filter(status__in=[SupportStatus.PENDING, SupportStatus.ASSIGNED, SupportStatus.WAITING_CUSTOMER])
    elif filter_type == 'start':
        qs = qs.filter(status__in=[SupportStatus.IN_PROGRESS, SupportStatus.STOPPED])
    elif filter_type == 'result':
        qs = qs.filter(status__in=[SupportStatus.RESOLVED, SupportStatus.CANCELLED])

    if search_value:
        qs = qs.filter(
            Q(title__icontains=search_value) |
            Q(ticket_no__icontains=search_value) |
            Q(personel_request__first_name__icontains=search_value) |
            Q(personel_request__last_name__icontains=search_value) |
            Q(assigned_staff__first_name__icontains=search_value)
        )

    total_records = qs.count()

    # --- ÖNEMLİ KISIM BAŞLANGIÇ ---
    # Log kayıtlarında "ÜSTLENME" kelimesi geçen bir sistem mesajı var mı kontrol ediyoruz.
    # Varsa is_self_assigned = True olacak.

    self_assign_check = RequestMessage.objects.filter(
        request=OuterRef('pk'),
        is_internal_log=True,
        message__contains="ÜSTLENME"  # set_personel'de kaydettiğimiz anahtar kelime
    )

    qs = qs.annotate(is_log_self_assigned=Exists(self_assign_check))
    # --- ÖNEMLİ KISIM BİTİŞ ---

    qs = qs.order_by('-created_at')[start:start + length]

    data = []
    for item in qs:
        display_no = item.ticket_no if item.ticket_no else str(item.id)[:6]

        data.append({
            'id': str(item.id),
            'ticket_no': display_no,
            'status': item.status,
            'personel_request__store__store_id': item.personel_request.store.store_id if item.personel_request.store else "-",
            'personel_request__username': item.personel_request.username,
            'title': item.title,
            'priority': item.priority,
            'category': item.category,
            'created_at': item.created_at,
            'assigned_staff__username': item.assigned_staff.get_full_name() if item.assigned_staff else None,

            # Burada log kontrolünden gelen sonucu gönderiyoruz
            'is_self_assigned': item.is_log_self_assigned,

            'unread_count': 0
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total_records,
        "recordsFiltered": total_records,
        "data": data
    })


@login_required(login_url='login')
def get_personels(request):
    users = Users.objects.filter(is_superuser=False, store_id=None, role__name="Geliştirme Ekibi").values('id',
                                                                                                          'username',
                                                                                                          'email',
                                                                                                          'first_name',
                                                                                                          'last_name')

    return JsonResponse({"data": list(users)})


@login_required(login_url='login')
def set_personel(request):
    if request.method == 'POST':
        request_id = request.POST.get('request_id')
        support = PersonelSupport.objects.get(id=request_id)
        priority_val = request.POST.get('priority')

        if request.user.is_superuser and 'personel' in request.POST:
            personel_id = request.POST['personel']
            support.assigned_staff_id = personel_id

            if priority_val:
                support.priority = priority_val

            assigned_user = Users.objects.get(id=personel_id)
            admin_name = f"{request.user.first_name} {request.user.last_name}"
            priority_display = dict(SupportPriority.choices).get(priority_val, 'Belirtilmedi') if priority_val else ""
            msg_content = f"<i class='fas fa-user-check me-1'></i> <strong>ATAMA:</strong> Talep, {admin_name} tarafından {assigned_user.first_name} {assigned_user.last_name} kişisine atandı."

            if priority_val:
                msg_content += f"<br><small class='text-muted'>Öncelik: {priority_display} olarak güncellendi.</small>"

            RequestMessage.objects.create(
                request=support,
                sender=request.user,
                message=f"<i class='fas fa-user-check me-1'></i> <strong>ATAMA:</strong> Talep, {admin_name} tarafından {assigned_user.first_name} {assigned_user.last_name} kişisine atandı.",
                is_internal_log=True,
                is_new_message=True
            )
        else:
            support.assigned_staff_id = request.user.id
            staff_name = f"{request.user.first_name} {request.user.last_name}"
            RequestMessage.objects.create(
                request=support,
                sender=request.user,
                message=f"<i class='fas fa-hand-paper me-1'></i> <strong>ÜSTLENME:</strong> {staff_name} bu destek talebini üzerine aldı.",
                is_internal_log=True,
                is_new_message=True
            )

        support.status = SupportStatus.IN_PROGRESS
        support.save()
        SupportWorkSession.objects.create(request=support)

        return JsonResponse({'result': True})

    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
def send_message(request):
    if request.method == 'POST':
        request_id = request.POST['request_id']
        message = request.POST['message']
        file = request.FILES.get("file")

        requestMessage = RequestMessage(
            sender=request.user,
            message=message,
            attachment=file,
            request_id=request_id

        )
        perRequest = PersonelSupport.objects.get(id=request_id)
        staus = perRequest.status
        if staus == SupportStatus.ASSIGNED and request.user.is_staff:
            perRequest.status = SupportStatus.IN_PROGRESS

        perRequest.save()
        try:
            requestMessage.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})

    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
@role_required('CUSTOMERS_DELETE')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Customers.objects.filter(id__in=ids)
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
            records = Customers.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required
def get_message_detail(request):
    personal_request_id = request.GET.get('request_id')

    messages = RequestMessage.objects.filter(request_id=personal_request_id).order_by("-created_at").values(
        "sender__username",
        "created_at",
        "sender__store_id"
        , "message", "attachment",
        "sender_id", "is_internal_log")

    context = {
        'messages': messages,
        'request': personal_request_id,
        'user_id': request.user.id,
        'is_superuser': request.user.is_superuser
    }

    context = render_to_string('management/supports/message-content.html', context, request=request)

    return JsonResponse({"html": context})


@login_required
def support_end(request):
    if request.method == 'POST':
        request_id = request.POST.get("request_id")
        reason = request.POST.get("reason")
        description = request.POST.get("description")

        if not reason or not description:
            return JsonResponse({'result': False, 'error': True, 'error_msg': 'Lütfen sebep ve açıklama giriniz.'})

        try:
            perRequest = PersonelSupport.objects.get(id=request_id)
            perRequest.status = SupportStatus.RESOLVED
            perRequest.closing_reason = reason
            perRequest.closing_description = description
            perRequest.closed_at = timezone.now()
            perRequest.save()

            open_session = SupportWorkSession.objects.filter(request=perRequest, end_time__isnull=True).first()
            if open_session:
                open_session.end_time = timezone.now()
                open_session.save()

            RequestMessage.objects.create(
                request=perRequest,
                sender=request.user,
                message=f"<strong>DESTEK KAPATILDI</strong><br>Sebep: {perRequest.get_closing_reason_display()}<br>Açıklama: {description}",
                is_internal_log=True
            )

            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})

    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required
def confirm_customer_verification(request, customer_id):
    """
    Müşterinin girdiği kodu doğrular ve veritabanını günceller.
    """
    c = get_object_or_404(Customers, pk=customer_id, is_deleted=False)
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
        "process_no",
        "transaction_type",
        "process_mileage",
        "employee__first_name",
        "product__name",
        "piece",
        "unit_price",
        "gram",
        "amount",
        "price_hs",
        "date",
        "is_status",
    ]
    order_column = columns[int(order_column_index)] if str(order_column_index).isdigit() else "date"
    if order_dir == 'desc':
        order_column = f"-{order_column}"

    qs = (Process.objects
          .filter(is_deleted=False,
                  store=request.user.store,
                  customer_id=customer_id,
                  transaction_type=tx)
          .select_related('product', 'employee')
          )

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
            Q(process_no__icontains=search_value) |
            Q(product__name__icontains=search_value) |
            Q(employee__first_name__icontains=search_value) |
            Q(employee__last_name__icontains=search_value)
        )

    total = qs.count()

    if length != -1:
        qs = qs.order_by(order_column)[start:start + length]
    else:
        qs = qs.order_by(order_column)

    data = []
    for r in qs:
        direction = 'Giriş' if r.transaction_type in ('PURCHASE', 'STOCK_IN', 'RETURN') else 'Çıkış'
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
            "date": r.date,
            "status": r.is_status or "-",
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data
    })


# apps/customers/views.py (En alta ekle)

@login_required(login_url='login')
def get_customer_detail_json(request):
    """
    AJAX ile tek bir müşterinin detaylarını JSON olarak döner.
    Resim URL'leri de dahil edildi.
    """
    customer_id = request.GET.get('id')
    if not customer_id:
        return JsonResponse({'result': False, 'error_msg': 'ID parametresi eksik.'}, status=400)

    try:
        customer = Customers.objects.get(id=customer_id, store=request.user.store, is_deleted=False)

        # Resim URL'lerini al (Varsa url property'si, yoksa None)
        front_img_url = customer.identification_front_image.url if customer.identification_front_image else None
        back_img_url = customer.identification_back_image.url if customer.identification_back_image else None

        data = {
            'id': str(customer.id),
            'first_name': customer.first_name or '',
            'last_name': customer.last_name or '',
            'identification_number': customer.identification_number or '',
            'phone': customer.phone or '',
            'email': customer.email or '',
            'gender': customer.gender or '',
            'address': customer.address or '',
            # Resim yolları
            'identification_front_image': front_img_url,
            'identification_back_image': back_img_url,
        }
        return JsonResponse({'result': True, 'data': data})

    except Customers.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Müşteri bulunamadı.'}, status=404)
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


@login_required
def toggle_support_status(request):
    if request.method == 'POST':
        request_id = request.POST.get('request_id')
        action = request.POST.get('action')

        support = get_object_or_404(PersonelSupport, id=request_id)
        user_name = f"{request.user.first_name} {request.user.last_name}"

        if action == 'stop':
            support.status = SupportStatus.STOPPED
            support.save()

            open_session = SupportWorkSession.objects.filter(request=support, end_time__isnull=True).first()
            if open_session:
                open_session.end_time = timezone.now()
                open_session.save()

            RequestMessage.objects.create(
                request=support,
                sender=request.user,
                message=f"<i class='fas fa-pause-circle text-warning me-1'></i> <strong>DURAKLATILDI:</strong> İşlem {user_name} tarafından geçici olarak durduruldu.",
                is_internal_log=True
            )

        elif action == 'resume':
            support.status = SupportStatus.IN_PROGRESS
            support.save()
            SupportWorkSession.objects.create(request=support)

            RequestMessage.objects.create(
                request=support,
                sender=request.user,
                message=f"<i class='fas fa-play-circle text-primary me-1'></i> <strong>DEVAM EDİYOR:</strong> İşlem {user_name} tarafından tekrar başlatıldı.",
                is_internal_log=True
            )

        return JsonResponse({'result': True})
    return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek'})


@login_required
def reactivate_support(request):
    if request.method == 'POST':
        request_id = request.POST.get('request_id')
        support = get_object_or_404(PersonelSupport, id=request_id)
        user_name = f"{request.user.first_name} {request.user.last_name}"

        support.status = SupportStatus.IN_PROGRESS
        support.closed_at = None
        support.closing_reason = None
        support.save()
        SupportWorkSession.objects.create(request=support)

        RequestMessage.objects.create(
            request=support,
            sender=request.user,
            message=f"<i class='fas fa-sync-alt text-success me-1'></i> <strong>TEKRAR AKTİF:</strong> Kapalı talep {user_name} tarafından tekrar işleme alındı.",
            is_internal_log=True
        )

        return JsonResponse({'result': True})
    return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek'})


def _extract_youtube_id(url_or_id):
    """
    YouTube URL'sinden veya ham ID'den video ID'sini çıkarır.
    Desteklenen formatlar:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://www.youtube.com/watch?si=xyz&v=VIDEO_ID   (parametre sırası farklı)
      - https://youtu.be/VIDEO_ID?si=tracking
      - https://www.youtube.com/embed/VIDEO_ID
      - https://www.youtube.com/shorts/VIDEO_ID
      - https://m.youtube.com/watch?v=VIDEO_ID
      - Ham ID: VIDEO_ID  veya  VIDEO_ID?si=tracking
    """
    from urllib.parse import urlparse, parse_qs

    if not url_or_id:
        return None

    url_or_id = url_or_id.strip()

    # --- 1) URL olarak ayrıştırmayı dene ---
    try:
        parsed = urlparse(url_or_id)
        host = (parsed.hostname or '').lower().replace('www.', '').replace('m.', '')

        # youtube.com/watch?v=...  (parametre sırası fark etmez)
        if host in ('youtube.com',) and parsed.path in ('/watch',):
            qs = parse_qs(parsed.query)
            v_list = qs.get('v')
            if v_list:
                return v_list[0]

        # youtu.be/VIDEO_ID  |  youtube.com/embed/VIDEO_ID  |  youtube.com/shorts/VIDEO_ID
        if host in ('youtube.com', 'youtube-nocookie.com', 'youtu.be'):
            path = parsed.path.strip('/')
            # youtu.be kısa linkleri
            if host == 'youtu.be' and path:
                return path.split('/')[0]
            # /embed/VIDEO_ID  veya  /shorts/VIDEO_ID
            for prefix in ('embed/', 'shorts/', 'v/'):
                if path.startswith(prefix):
                    return path[len(prefix):].split('/')[0]
    except Exception:
        pass

    # --- 2) URL parse başarısız veya tanınmayan format → ham ID olarak değerlendir ---
    # "VIDEO_ID?si=tracking" gibi artık parametreleri temizle
    clean = re.sub(r'[?&].*$', '', url_or_id)

    # Geçerli YouTube video ID: 11 karakter, harf/rakam/tire/alt çizgi
    if re.fullmatch(r'[\w-]{10,14}', clean):
        return clean

    return None


@login_required
@user_passes_test(_is_support_admin)
def add_training_video(request):
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        youtube_url = request.POST.get('youtube_url', '').strip()
        module_name = request.POST.get('module_name', '').strip()
        order = request.POST.get('order', '0')

        if not title or not youtube_url or not module_name:
            return JsonResponse({'result': False, 'error_msg': 'Tüm zorunlu alanları doldurunuz.'})

        youtube_id = _extract_youtube_id(youtube_url)
        if not youtube_id:
            return JsonResponse({'result': False, 'error_msg': 'Geçerli bir YouTube linki veya ID giriniz.'})

        try:
            order_val = int(order)
        except (ValueError, TypeError):
            order_val = 0

        try:
            video = TrainingVideo.objects.create(
                title=title,
                youtube_id=youtube_id,
                module_name=module_name,
                order=order_val
            )
            return JsonResponse({
                'result': True,
                'video': {
                    'id': video.id,
                    'title': video.title,
                    'youtube_id': video.youtube_id,
                    'module_name': video.module_name,
                    'order': video.order
                }
            })
        except Exception as e:
            return JsonResponse({'result': False, 'error_msg': str(e)})

    return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek.'})


@login_required
@user_passes_test(_is_support_admin)
def delete_training_video(request):
    if request.method == 'POST':
        video_id = request.POST.get('video_id')
        if not video_id:
            return JsonResponse({'result': False, 'error_msg': 'Video ID eksik.'})

        try:
            video = TrainingVideo.objects.get(id=video_id)
            video.delete()
            return JsonResponse({'result': True})
        except TrainingVideo.DoesNotExist:
            return JsonResponse({'result': False, 'error_msg': 'Video bulunamadı.'})
        except Exception as e:
            return JsonResponse({'result': False, 'error_msg': str(e)})

    return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek.'})
