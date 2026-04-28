from datetime import datetime
from datetime import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import user_passes_test, login_required
from django.contrib.auth.hashers import make_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.cache import cache
from django.core.validators import validate_email
from django.db.models import Sum, Q
from django.http import JsonResponse
from django.shortcuts import redirect, get_object_or_404, render
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.http import require_POST, require_GET

from django.db import transaction

from apps.activity_logs.views import write_log
from apps.chambers.models import Chambers
from apps.helpers.image_resize import process_image
from apps.process.models import Process, Payment
from apps.roles.decorators import role_required
from apps.roles.models import Roles
from apps.settings.send_mail import *


# --- YARDIMCI SABİTLER VE FONKSİYONLAR ---

def random_string(string_length=6):
    letters = string.digits
    return ''.join(random.choice(letters) for i in range(string_length))


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def normalize_tr_msisdn(phone: str) -> str:
    """Basit TR normalize: harfleri sil, baştaki 0'ı at, +90 ekle."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if not digits.startswith("90"):
        digits = "90" + digits
    return f"+{digits}"


def _gen_code(n=6):
    return ''.join(random.choice(string.digits) for _ in range(n))


def _resolve_post_login_redirect(user_info):
    """
    Başarılı giriş sonrası rol bazlı yönlendirme.

    Kurallar:
        - Rol kategorisi 'CHAMBER' ise → Dernek paneli.
        - Rolü admin (mağaza sahibi) veya superuser ise → Dashboard (Raporlar).
        - Diğer kullanıcılar (personel vb.) → Mevcut profil sayfası.
    """
    role = getattr(user_info, 'role', None)
    if role and getattr(role, 'category', None) == 'CHAMBER':
        return redirect('chambers:dashboard')

    is_admin_role = bool(role and (role.name or '').strip().lower() == 'admin')
    if user_info.is_superuser or is_admin_role:
        return redirect('dashboard:index')

    return redirect('accounts:profile')


@login_required
def user_verify_state(request, user_id):
    user = get_object_or_404(Users, pk=user_id, is_deleted=False)
    return JsonResponse({
        'phone': {
            'value': user.mobile_phone or '',
            'verified': bool(user.is_phone_verified),
        },
        'email': {
            'value': user.email or '',
            'verified': bool(user.is_email_verified),
        }
    })


@login_required
def send_user_verification(request, user_id):
    user = get_object_or_404(Users, pk=user_id, is_deleted=False)
    channel = request.POST.get('channel')  # 'email' | 'phone'
    if channel not in ('email', 'phone'):
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz kanal'})

    code = _gen_code()
    OtpCode.objects.create(
        owner_type='user', owner_id=str(user.id), channel=channel,
        code=code, purpose=f'verify_{channel}', expires_at=timezone.now() + timedelta(minutes=10)
    )

    if channel == 'email':
        if not user.email:
            return JsonResponse({'error': True, 'error_msg': 'E-posta yok'})

        # --- GÜNCELLENDİ: EmailService Kullanımı ---
        EmailService.send(
            user=user,
            subject='İYS İletişim Onayı • Doğrulama Kodu',
            template_name='management/mail_templates/verify_contact_iys.html',
            context={
                'subject': 'İYS İletişim Onayı • Doğrulama Kodu',
                'otp_string': code,
                'user': user,
                'verify_url': request.build_absolute_uri('/verify'),
                'consent_scope': 'E-posta',
            },
            config_key=None  # Bu işlem kritik olduğu için kullanıcı ayarına bakmaksızın gönderilir
        )
        # -------------------------------------------

    else:
        to_raw = user.mobile_phone or ''
        if not to_raw:
            return JsonResponse({'error': True, 'error_msg': 'Telefon yok'})

        to = normalize_tr_msisdn(to_raw)
        can, reason, lang = wa_preflight(user.store, "verify_phone_v1", "tr_TR")
        if can:
            send_whatsapp_template_guarded(
                store=user.store, user=user, customer=None, to=to,
                template="verify_phone_v1", language=lang, header_params=None,
                body_params=[code], button_params=[code], validate=False
            )
    return JsonResponse({'result': True})


@login_required
def confirm_user_verification(request, user_id):
    user = get_object_or_404(Users, pk=user_id, is_deleted=False)
    channel = request.POST.get('channel')
    code = request.POST.get('code', '')
    consent_iys = request.POST.get('consent_iys') == '1'
    now = timezone.now()

    ok = OtpCode.objects.filter(
        owner_type='user', owner_id=str(user.id), channel=channel,
        purpose=f'verify_{channel}', code=code, used=False, expires_at__gt=now
    ).exists()
    if not ok:
        return JsonResponse({'error': True, 'error_msg': 'Kod geçersiz veya süresi dolmuş.'})

    OtpCode.objects.filter(
        owner_type='user', owner_id=str(user.id), channel=channel,
        purpose=f'verify_{channel}', code=code
    ).update(used=True)

    if channel == 'email':
        user.is_email_verified = True
    else:
        user.is_phone_verified = True
    user.save(update_fields=['is_email_verified', 'is_phone_verified'])

    if consent_iys:
        ContactConsent.objects.update_or_create(
            owner_type='user', owner_id=str(user.id), channel=channel,
            defaults={
                'is_consented': True,
                'consented_at': timezone.now(),
                'ip_address': _client_ip(request),
                'source': 'otp_verify',
                'iys_status': 'pending',
                'iys_ref': None
            }
        )

    return JsonResponse({'result': True})


class ImageUploadForm(forms.Form):
    image = forms.ImageField(
        label='Resim Yükle',
        help_text='Desteklenen formatlar: .png, .jpg, .jpeg'
    )


def login_view(request):
    context = {
        'title': 'Giriş',
    }
    messages.error(request, '')

    if request.POST:

        client_ip = _client_ip(request)
        ip_cache_key = f"login_fail_ip_{client_ip}"

        ip_fail_count = cache.get(ip_cache_key, 0)

        if ip_fail_count >= 20:
            messages.error(request,
                           'Çok fazla başarısız giriş denemesi. IP adresiniz güvenlik nedeniyle geçici olarak engellenmiştir.')
            return render(request, 'management/accounts/login.html', context)

        raw_login = (request.POST.get('username') or '').strip()
        password = (request.POST.get('password') or '').strip()

        token = request.POST.get('token')
        code = request.POST.get('code')
        cipher = AESCipher(settings.SECRET_KEY)

        if token:
            token = str(token).replace('b\'', '').replace('\'', '').encode('utf-8')
            raw_data = cipher.decrypt(token)
            data = raw_data.split('##')
            user_info = authenticate(username=str(data[0]), password=str(data[1]))
            token = cipher.decrypt(str(data[2]).replace('b\'', '').replace('\'', '').encode('utf-8'))

            if user_info is not None and token == code:
                try:
                    user_info.failed_login_attempts = 0
                    user_info.blocked_until = None
                    user_info.save(update_fields=['failed_login_attempts', 'blocked_until'])
                except Exception:
                    pass

                login(request, user_info)
                return _resolve_post_login_redirect(user_info)
            else:
                messages.error(request, 'SMS Kodu Hatalı')
                return redirect('login')
        else:
            try:
                user = Users.objects.get(
                    (Q(username__iexact=raw_login) | Q(email__iexact=raw_login)),
                    is_deleted=False
                )
            except ObjectDoesNotExist:
                cache.set(ip_cache_key, ip_fail_count + 1, 900)  # 900 sn = 15 dk

                messages.error(request, 'Kullanıcı bulunamadı!')
                return redirect('login')

            if user.blocked_until and user.blocked_until > timezone.now():
                wait_minutes = int((user.blocked_until - timezone.now()).total_seconds() / 60)
                if wait_minutes < 1: wait_minutes = 1

                messages.error(request,
                               f'Çok fazla hatalı giriş. Hesabınız {wait_minutes} dakika süreyle kilitlenmiştir.')
                return render(request, 'management/accounts/login.html', context)

            user_info = authenticate(request, username=user.username, password=password)

            if not user_info:

                cache.set(ip_cache_key, ip_fail_count + 1, 900)

                user.failed_login_attempts += 1
                MAX_ATTEMPTS = 5

                if user.failed_login_attempts >= MAX_ATTEMPTS:
                    user.blocked_until = timezone.now() + timedelta(minutes=15)

                    user.save(update_fields=['failed_login_attempts', 'blocked_until'])

                    messages.error(request, '5 defa hatalı giriş yaptınız. Hesabınız 15 dakika süreyle kilitlenmiştir.')
                else:
                    user.save(update_fields=['failed_login_attempts'])
                    remaining = MAX_ATTEMPTS - user.failed_login_attempts

                    if not user.is_active:
                        messages.error(request, 'Hesabınız pasif. Lütfen yöneticinizle iletişime geçin!')
                    else:
                        messages.error(request,
                                       f'Kullanıcı adı/e-posta veya parola hatalı. Kalan Hakkınız: {remaining}')

                return redirect('login')

            else:
                # Başarılı şifre girişi sonrası işlemler
                user.failed_login_attempts = 0
                user.blocked_until = None
                user.save(update_fields=['failed_login_attempts', 'blocked_until'])

                # --- 2FA KONTROLÜ ---
                if user.activate_2fa:
                    email = user.email
                    otp_string = random_string()
                    cipher = AESCipher(settings.SECRET_KEY)
                    token = cipher.encrypt(otp_string)

                    try:
                        user = Users.objects.get(email=email, is_deleted=False)
                    except Users.DoesNotExist:
                        # Kullanıcı oturumu açılmış olsa bile veritabanında bulunamama ihtimaline karşı
                        return redirect('login')

                        # 1. E-POSTA GÖNDERİMİ (MERKEZİ SERVİS)
                    # ----------------------------------------------------------------
                    EmailService.send(
                        user=user,
                        subject='Giriş Kodu',
                        template_name='management/mail_templates/mail_template_2fa.html',
                        context={
                            'subject': 'Giriş Kodu',
                            'otp_string': otp_string,
                            'user': {'username': user.username}
                        },
                        config_key='notify_email_2fa'
                    )

                    # 2. WHATSAPP GÖNDERİMİ (DÜZELTİLDİ)
                    # ----------------------------------------------------------------
                    # Config kontrolü
                    config = EmailService._get_store_config(user)
                    allow_wa = getattr(config, 'notify_wa_2fa', True) if config else True

                    if allow_wa:
                        # Fonksiyon tanımlamak yerine doğrudan mantığı çalıştırıyoruz
                        try:
                            store = getattr(user, "store", None)
                            to_raw = getattr(user, "mobile_phone", None) or getattr(user, "phone", None)

                            if store and to_raw:
                                # Normalizasyon fonksiyonunuzu kullanıyoruz
                                to_wa = normalize_tr_msisdn(to_raw)

                                can_send, reason, chosen_lang = wa_preflight(store, "twofa_login_v1", "tr_TR")

                                if can_send:
                                    send_whatsapp_template_guarded(
                                        store=store,
                                        user=user,
                                        customer=None,
                                        to=to_wa,
                                        template="twofa_login_v1",
                                        language=chosen_lang,
                                        header_params=None,
                                        body_params=[otp_string],
                                        button_params=[otp_string],
                                        validate=False
                                    )
                                else:
                                    print(f"WA Engellendi: {reason}")  # Loglama yapılabilir

                        except Exception as e:
                            print(f"WA 2FA Hatası: {e}")  # Loglama yapılabilir
                    # ----------------------------------------------------------------

                    data = cipher.encrypt(user.username + '##' + password + '##' + str(token) + '##')
                    context['token'] = str(data)
                    return render(request, 'management/accounts/login.html', context)
                else:
                    # 2FA KAPALI İSE DİREKT GİRİŞ
                    login(request, user_info)
                    print(f"Giriş başarılı: {user_info.username}")

                    # PERSONEL GİRİŞ BİLDİRİMİ (Yöneticiye Mail)
                    if hasattr(user_info, 'role'):
                        if user_info.role and user_info.role.name.lower() == "personel":
                            if user_info.store:
                                # Adminleri bul
                                admins = Users.objects.filter(store=user_info.store, role__name__iexact="admin",
                                                              is_active=True)
                                login_time = timezone.now().strftime('%Y-%m-%d %H:%M:%S')

                                for admin in admins:
                                    EmailService.send(
                                        user=admin,
                                        subject=f"Mağaza Giriş Bildirimi: {user_info.get_full_name()}",
                                        template_name='management/mail_templates/store_login_notification.html',
                                        context={
                                            'staff_member': user_info,
                                            'login_time': login_time,
                                            'user': admin
                                        },
                                        config_key='notify_email_staff_login'
                                    )

                    return _resolve_post_login_redirect(user_info)
    else:
        return render(request, 'management/accounts/login.html', context)


@login_required()
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required(login_url='login')
@role_required('ACCOUNTS_INDEX_VIEW')
def index_view(request):
    context = {
        "title": "",
    }
    write_log(request, 'Kullanıcılar', 'Kullanıcılar Görüntülendi.')
    return render(request, 'management/accounts/index.html', context)


REASON_MAP = {
    "pkg_missing": {
        "title": "Paket Bulunamadı",
        "summary": "Hesabınıza bağlı aktif bir paket bulunamadı ya da paketin süresi dolmuş.",
        "image": "/static/management/media/auth/403.png",
        "title_class": "text-warning",
    },
    "pkg_denied": {
        "title": "Paket Kapsamı Dışı",
        "summary": "Kullandığınız paket bu özelliği kapsamıyor.",
        "image": "/static/management/media/auth/403.png",
        "title_class": "text-warning",
    },
    "role_missing": {
        "title": "Rol Atanmamış",
        "summary": "Hesabınıza bir rol atanmamış görünüyor.",
        "image": "/static/management/media/auth/403.png",
        "title_class": "text-danger",
    },
    "perm_denied": {
        "title": "Yetkiniz Yok",
        "summary": "Bu işlem için gerekli rol yetkiniz bulunmuyor.",
        "image": "/static/management/media/auth/403.png",
        "title_class": "text-danger",
    },
}


@login_required
def access_error(request):
    reason = request.GET.get("reason") or ""
    cfg = REASON_MAP.get(reason, {})

    storage = messages.get_messages(request)
    error_messages = [m for m in storage]

    next_url = request.GET.get("next") or request.META.get("HTTP_REFERER") or reverse("dashboard:index")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse("dashboard:index")

    ctx = {
        "error_title": cfg.get("title", "Erişim Engellendi"),
        "error_summary": cfg.get("summary", ""),
        "error_image": cfg.get("image", "/static/management/media/auth/404-error.png"),
        "title_class": cfg.get("title_class", "text-danger"),
        "error_messages": error_messages,
        "next_url": next_url,
        "support_text": "Hatanın devam etmesi halinde lütfen sistem yöneticinizle iletişime geçin.",
    }
    return render(request, "management/accounts/error.html", ctx, status=403)


@login_required(login_url="login")
@role_required('ACCOUNTS_EMPLOYEE_DETAIL_VIEW')
def employee_detail_view(request, user_id):
    employee = get_object_or_404(Users, id=user_id, is_deleted=False)

    now = timezone.now()
    if timezone.is_naive(now):
        today = now.date()
    else:
        today = timezone.localtime(now).date()

    start_of_week = today - timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)

    processes = Process.objects.filter(employee=employee, is_deleted=False)
    payment_process_nos = processes.values_list('process_no', flat=True)
    payments = Payment.objects.filter(process_no__in=payment_process_nos)

    sale = Sum("amount", filter=Q(transaction_type="SALE"))
    buy = Sum("amount", filter=Q(transaction_type__in=["PURCHASE", "RETURN"]))

    daily_earnings = (processes.filter(date__date=today).aggregate(sale=sale, buy=buy))
    weekly_earnings = (processes.filter(date__date__gte=start_of_week).aggregate(sale=sale, buy=buy))
    monthly_earnings = (processes.filter(date__date__gte=start_of_month).aggregate(sale=sale, buy=buy))

    roles = []
    if hasattr(employee, "roles"):
        roles = list(employee.roles.values_list("name", flat=True))

    context = {
        "employee": employee,
        "processes": processes[:1],
        "payments": payments.order_by("-date")[:100],
        "daily_earnings": (daily_earnings.get("sale") or 0) - (daily_earnings.get("buy") or 0),
        "weekly_earnings": (weekly_earnings.get("sale") or 0) - (weekly_earnings.get("buy") or 0),
        "monthly_earnings": (monthly_earnings.get("sale") or 0) - (monthly_earnings.get("buy") or 0),
        "roles": roles,
        "title": f"{employee.first_name} {employee.last_name} - Detay",
    }
    return render(request, "management/accounts/detail.html", context)


@login_required(login_url='login')
def profile_view(request):
    user = request.user
    record = Users.objects.get(id=user.id)
    store = user.store
    if store:
        store = {
            'phone': store.phone,
            'address': store.address,
            'avatar': store.avatar,
            'description': store.description,
            'subscription_start': store.subscription_start,
        }
    else:
        store = {}

    context = {
        'title': 'Profilim',
        'record': record,
        'store': store,
    }
    if request.POST:
        if not request.user.is_superuser:
            record.type = request.POST.get('type')
            password = request.POST.get("password")
            if password:
                record.set_password(password)
        else:
            record.company_name = request.POST.get('company_name')
            record.tax = request.POST.get('tax')
            record.tax_number = request.POST.get('tax_number')
            record.display_name = record.company_name
            type_checked = request.POST.get('type') == 'on'
            record.type = type_checked
            record.collected = request.POST.get('collected') == 'on'
            record.discount = request.POST.get('discount')
            record.payment_terms = request.POST.get('payment_terms')
            record.delivery_terms = request.POST.get('delivery_terms')
            record.salutation = request.POST.get('salutation')
            record.contact_name = request.POST.get('contact_name')
            record.contact_surname = request.POST.get('contact_surname')
            record.contact_person = request.POST.get('contact_person') == 'on'
            record.contact_phone = request.POST.get('contact_phone')
            record.contact_mail = request.POST.get('contact_mail')
            record.first_name = request.POST.get("first_name")
            record.last_name = request.POST.get("last_name")
            record.company_name = request.POST.get("company_name")
            record.mobile_phone = request.POST.get("mobile_phone")
            record.type = request.POST.get('type')
            password = request.POST.get("password")
            if password:
                record.set_password(password)
            image = request.FILES.get('avatar')
            if image:
                filename, processed = process_image(image)
                record.avatar.save(filename, processed, save=False)

        try:
            record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})

    return render(request, 'management/accounts/profile.html', context)


def generate_random_password(length=10):
    chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(random.choice(chars) for _ in range(length))


@login_required(login_url='login')
def add_view(request):
    context = {
        "title": "Kullanıcı Ekle / Düzenle",
        "roles": Roles.objects.filter(is_deleted=False, is_active=True).order_by('name'),
        "stores": Stores.objects.filter(is_deleted=False, is_active=True),
        "chambers": Chambers.objects.filter(is_deleted=False, is_active=True).order_by('name'),
    }

    record_id = request.POST.get("record_id") if request.method == "POST" else request.GET.get("id")
    record = get_object_or_404(Users, pk=record_id) if record_id else Users()

    if record_id and record.pk:
        current_ch = Chambers.objects.filter(president_user=record, is_deleted=False).first()
        context["current_chamber_id"] = str(current_ch.id) if current_ch else ""

    if request.method == "POST":
        email = request.POST.get("email")
        first_name = request.POST.get("first_name")
        last_name = request.POST.get("last_name")
        job_title = request.POST.get("job_title")
        username = request.POST.get("username")
        mobile_phone = request.POST.get("mobile_phone")
        personal_type = request.POST.get("personal_type")
        role_id = request.POST.get("role_id")
        store_id = request.POST.get("store_id")
        password = request.POST.get("password")
        avatar = request.FILES.get("avatar")

        try:
            validate_email(email)
        except ValidationError:
            write_log(request, "Kullanıcılar", f"E-posta doğrulama hatası: {email}")
            return JsonResponse({"error": True, "error_msg": "Geçersiz e-posta adresi."})

        email = (email or "").strip()

        print(email)

        should_protect = Stores.objects.filter(
            email=email,
            is_active=True,
            is_deleted=False
        ).exists()

        effective_store_id = store_id or (record.store_id if record_id else None)
        if effective_store_id:
            target_store = get_object_or_404(Stores, id=effective_store_id, is_deleted=False)
            if not target_store.is_active:
                return JsonResponse({
                    "error": True,
                    "error_code": "STORE_INACTIVE",
                    "error_msg": "Ödeme yapılmadığı için mağazanız pasif. Personel eklemek için önce aboneliği/aboneliği aktifleştirin."
                })

            # ─────────────────────────────────────────────────────────────
            # FAZ 19 — Demo Mağaza Personel Limiti
            #
            # DEMO statüsündeki mağazalar en fazla DEMO_MAX_USERS kadar
            # personel barındırabilir. Konasoft personeli (is_staff) bu
            # limite tabi değildir; admin tarafından demo mağazaya destek
            # amaçlı kullanıcı eklenebilmelidir.
            # Limit yalnızca YENİ kullanıcı eklerken kontrol edilir; mevcut
            # kullanıcının güncellenmesi sayıyı artırmaz.
            # ─────────────────────────────────────────────────────────────
            DEMO_MAX_USERS = 3
            if (
                getattr(target_store, 'status', None) == 'DEMO'
                and not record_id
                and not getattr(request.user, 'is_staff', False)
            ):
                current_user_count = Users.objects.filter(
                    store=target_store, is_deleted=False
                ).count()
                if current_user_count >= DEMO_MAX_USERS:
                    return JsonResponse({
                        "error": True,
                        "error_code": "DEMO_USER_LIMIT",
                        "error_msg": (
                            f"Demo mağazalarda en fazla {DEMO_MAX_USERS} personel "
                            f"eklenebilir. Limit dolmuş ({current_user_count}/{DEMO_MAX_USERS}). "
                            f"Daha fazla personel için aboneliği aktif pakete dönüştürün."
                        ),
                    })

        if should_protect:
            print("-" * 100)
        if not record_id:
            random_password = generate_random_password()
            send_password = password or random_password

            record.username = username
            record.email = email
            record.first_name = first_name
            record.last_name = last_name
            record.job_title = job_title
            record.mobile_phone = mobile_phone
            record.personal_type = personal_type
            record.role_id = role_id
            record.store_id = store_id
            record.password = make_password(send_password)

            if should_protect:
                record.is_protected = True

            if avatar:
                record.avatar = avatar

            try:
                with transaction.atomic():
                    record.save()

                    chamber_id = request.POST.get("chamber_id")
                    if chamber_id:
                        Chambers.objects.filter(president_user=record).update(president_user=None)
                        Chambers.objects.filter(id=chamber_id, is_deleted=False).update(president_user=record)

                EmailService.send(
                    user=record,
                    subject="Yeni Kullanıcı Bilgileri",
                    template_name="management/mail_templates/new_user_mail.html",
                    context={
                        "username": record.username,
                        "password": send_password,
                        "email": record.email,
                    },
                    config_key=None
                )

                write_log(request, "Kullanıcılar", f"Yeni kullanıcı eklendi. ID= {record.id}")
                return JsonResponse({"result": True})

            except Exception as e:
                write_log(request, "Kullanıcılar", f"Kayıt hatası: {e}")
                es = str(e)
                if 'unique constraint "Users_username_key"' in es:
                    return JsonResponse({"error": True, "error_msg": "Bu kullanıcı adı zaten kullanılıyor."})
                if 'unique constraint "Users_email_key"' in es:
                    return JsonResponse({"error": True, "error_msg": "Bu e-posta adresi zaten kullanılıyor."})
                return JsonResponse({"error": True, "error_msg": es})

        else:
            # UPDATE
            if username:
                record.username = username
            if email:
                record.email = email
            if first_name:
                record.first_name = first_name
            if last_name:
                record.last_name = last_name
            if job_title:
                record.job_title = job_title
            if mobile_phone:
                record.mobile_phone = mobile_phone
            if personal_type:
                record.personal_type = personal_type
            if role_id:
                record.role_id = role_id
            if store_id:
                record.store_id = store_id
            if password:
                record.set_password(password)
            if avatar:
                record.avatar = avatar

            if should_protect:
                record.is_protected = True

            try:
                with transaction.atomic():
                    record.save()

                    chamber_id = request.POST.get("chamber_id")
                    selected_role = Roles.objects.filter(id=role_id).first() if role_id else None
                    if selected_role and selected_role.category == 'CHAMBER':
                        Chambers.objects.filter(president_user=record).update(president_user=None)
                        if chamber_id:
                            Chambers.objects.filter(id=chamber_id, is_deleted=False).update(president_user=record)
                    else:
                        Chambers.objects.filter(president_user=record).update(president_user=None)

                write_log(request, "Kullanıcılar", f"Kullanıcı güncellendi. ID= {record.id}")
                return JsonResponse({"result": True})
            except Exception as e:
                write_log(request, "Kullanıcılar", f"Güncelleme hatası: {e}")
                es = str(e)
                if 'unique constraint "Users_username_key"' in es:
                    return JsonResponse({"error": True, "error_msg": "Bu kullanıcı adı zaten kullanılıyor."})
                if 'unique constraint "Users_email_key"' in es:
                    return JsonResponse({"error": True, "error_msg": "Bu e-posta adresi zaten kullanılıyor."})
                return JsonResponse({"error": True, "error_msg": es})

    context["record"] = record
    return render(request, "management/accounts/add.html", context)


@login_required(login_url='login')
def get_all(request):
    draw = int(request.GET['draw'])
    length = int(request.GET['length'])
    start = int(request.GET['start'])
    search_value = request.GET.get('search[value]', '')
    order_column = request.GET['columns[' + request.GET['order[0][column]'] + '][data]']
    order = request.GET['order[0][dir]']
    type = request.GET.get('type', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    is_active = request.GET.get('is_active', '')
    username = request.GET.get('username', '')

    if order_column is None:
        order_column = "created_on"

    if order == 'desc':
        order_column = '-' + order_column

    def translate_fields(queryset):
        for ad in queryset:
            ad['date_joined'] = ad['date_joined'].strftime('%d-%m-%Y')
        return queryset

    queryset = Users.objects.filter(is_deleted=False).filter(is_staff=True).values(
        'id', 'avatar', 'date_joined', 'first_name', 'username',
        'mobile_phone', 'is_superuser', 'is_staff', 'role__name',
        'is_active',
        'last_name', 'email',
        'customer_number')

    if type and type != 'all':
        queryset = queryset.filter(type=type)

    if date_from:
        date_from = datetime.strptime(date_from, '%d/%m/%Y')
        queryset = queryset.filter(date_joined__gte=date_from)

    if date_to:
        date_to = datetime.strptime(date_to, '%d/%m/%Y')
        queryset = queryset.filter(date_joined__lte=date_to)

    if is_active != '':
        is_active = True if is_active.lower() == 'true' else False
        queryset = queryset.filter(is_active=is_active)

    if username:
        queryset = queryset.filter(username__icontains=username)

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(
            Q(company_name__icontains=search_value) |
            Q(tax_number__icontains=search_value) |
            Q(tax__icontains=search_value) |
            Q(customer_number__icontains=search_value)
        )

    count = queryset.count()

    if str(length) == '-1':
        queryset = queryset.order_by(order_column)
    else:
        queryset = queryset.order_by(order_column)[start:start + length]

    queryset = translate_fields(queryset)
    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": list(queryset)
    })


@login_required(login_url='login')
def change_two_factor(request, record_id):
    record = Users.objects.get(id=record_id)

    if record.activate_2fa:
        record.activate_2fa = False
    else:
        record.activate_2fa = True
    try:
        record.save()
        write_log(request, 'İki Faktörlü Doğrulama',
                  'İki Faktörlü Doğrulama Tercihi Değiştirildi. ID= ' + str(record.id).upper())
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'error': str(e)})


@login_required(login_url='login')
def get_all_accounts(request, record_id):
    draw = int(request.GET.get('draw', '1'))
    length = int(request.GET.get('length', '25'))
    start = int(request.GET.get('start', '0'))
    search_value = request.GET.get('search[value]', '').strip()
    order_column = request.GET.get('columns[' + request.GET.get('order[0][column]', '0') + '][data]', 'date_joined')
    order = request.GET.get('order[0][dir]', 'desc')

    if order == 'desc':
        order_column = '-' + order_column

    qs = Users.objects.filter(
        is_deleted=False,
        store_id=record_id
    ).values(
        'id', 'mobile_phone', 'avatar', 'date_joined', 'first_name', 'is_staff',
        'role__name', 'role_id', 'last_name', 'email', 'identification_number', 'is_active', 'username'
    )

    total = qs.count()

    if search_value:
        qs = qs.filter(
            Q(username__icontains=search_value) |
            Q(email__icontains=search_value) |
            Q(first_name__icontains=search_value) |
            Q(last_name__icontains=search_value) |
            Q(mobile_phone__icontains=search_value)
        )

    count = qs.count()
    if str(length) != '-1':
        qs = qs.order_by(order_column)[start:start + length]
    else:
        qs = qs.order_by(order_column)

    data = list(qs)
    for r in data:
        r['date_joined'] = r['date_joined'].strftime('%d-%m-%Y') if r['date_joined'] else ''
        # UUID → string dönüşümü (JSON serileştirme için)
        if r.get('role_id'):
            r['role_id'] = str(r['role_id'])

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": data
    })


def email_verify(request, uidb64, token):
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = Users.objects.get(pk=uid)

        token_generator = PasswordResetTokenGenerator()
        if token_generator.check_token(user, token):
            user.is_email_verified = True
            user.save(update_fields=['is_email_verified'])
            user.save()

            return redirect('')
        else:
            return render(request, 'invalid_token.html')
    except (TypeError, ValueError, OverflowError, Users.DoesNotExist, ValidationError):
        return render(request, 'invalid_link.html')


def reset_password_view(request):
    context = {
        "title": "Şifremi Unuttum",
        "forgot": "enable",
    }

    if request.method == 'POST':
        email = request.POST.get('forgot_email')
        user = Users.objects.filter(email=email, is_deleted=False).first()

        if user:

            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = token_generator.make_token(user)

            # URL oluştururken request yoksa hardcode domain kullanılabilir
            if request:
                reset_url = request.build_absolute_uri(
                    reverse('accounts:reset-password-confirm', kwargs={'uidb64': uidb64, 'token': token}))
            else:
                # Fallback (örnek)
                reset_url = f"https://kuyumplus.com/accounts/reset/{uidb64}/{token}/"

            EmailService.send(
                user=user,
                subject="Şifre Sıfırlama Talebi",
                template_name='management/mail_templates/forgot_password_template.html',
                context={
                    "user": user,
                    "reset_url": reset_url,
                    "company_name": "Kuyum Plus",
                },
                config_key=None
            )

            messages.success(request, "Şifre sıfırlama linki e-posta adresinize gönderildi.", extra_tags='success')
            return redirect('login')
            # -----------------------------------------------------------------------
        else:
            messages.error(request, "Bu e-posta adresi ile kayıtlı kullanıcı bulunamadı.", extra_tags='danger')
            return redirect('accounts:reset-password')

    return render(request, 'management/accounts/forgot_password.html', context)


def reset_password_confirm_view(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = Users.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, Users.DoesNotExist):
        user = None

    if user is not None and token_generator.check_token(user, token):
        ctx = {'uidb64': uidb64, 'token': token}

        if request.method == 'POST':
            password = (request.POST.get('new_password') or '').strip()
            password_confirm = (request.POST.get('new_password_confirm') or '').strip()

            if len(password) < 8:
                ctx['pw_error'] = "Şifre en az 8 karakter olmalıdır."
                return render(request, 'management/accounts/reset_password_confirm.html', ctx, status=400)

            if password != password_confirm:
                ctx['pw2_error'] = "Şifreler uyuşmuyor."
                return render(request, 'management/accounts/reset_password_confirm.html', ctx, status=400)

            user.set_password(password)
            user.save(update_fields=['password'])
            messages.success(request, "Şifreniz başarıyla değiştirildi.", extra_tags='success')
            return redirect('login')

        return render(request, 'management/accounts/reset_password_confirm.html', ctx)

    else:
        messages.error(request, "Şifre sıfırlama bağlantısı geçersiz ya da süresi dolmuş.", extra_tags='danger')
        return redirect('accounts:reset-password')


@login_required(login_url='login')
@role_required('ACCOUNTS_DELETE')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Users.objects.filter(id__in=ids)
            for record in records:
                if record.is_protected:
                    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Korunan kullanıcı silinemez.'})
                record.is_deleted = True
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
def change_status(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Users.objects.filter(id__in=ids)
            for record in records:
                if record.is_protected:
                    return JsonResponse(
                        {'result': False, 'error': True, 'error_msg': 'Korunan kullanıcının durumu değiştirilemez.'})
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
@role_required('ACCOUNTS_STAFF_MANAGEMENT_VIEW')
def staff_management_view(request):
    if request.user.store:
        store_id = request.user.store.id
        return redirect('stores:detail', record_id=store_id)
    else:
        return redirect('dashboard:index')


def _admin_or_staff(u):
    return u.is_authenticated and (u.is_superuser or u.is_staff)


def _fmt_ts(dt):
    if not dt:
        return ''
    try:
        if getattr(settings, 'USE_TZ', False):
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            dt = timezone.localtime(dt)
        return dt.strftime('%d.%m.%Y %H:%M')
    except Exception:
        try:
            return dt.strftime('%d.%m.%Y %H:%M')
        except Exception:
            return ''


# apps/accounts/views.py içerisinde ilgili kısımları güncelle:

@login_required(login_url='login')
def staffs_index_view(request):
    """
    Konasoft Personel Yönetimi Ana Sayfası.
    Sadece 'SYSTEM' kategorisindeki roller listelenir.
    """
    ctx = {
        "title": "Konasoft Personel Yönetimi",
        # Sadece Konasoft (Sistem) rollerini getiriyoruz.
        "roles": Roles.objects.filter(
            is_deleted=False,
            is_active=True,
            category='SYSTEM',
        ).order_by('name')
    }
    write_log(request, "Kullanıcılar", "Personel listesi görüntülendi.")
    return render(request, "management/accounts/staffs_index.html", ctx)


@login_required(login_url='login')
@require_GET
def staffs_get_all(request):
    """
    Datatables için JSON veri kaynağı.
    Konasoft personellerini (Store ID'si olmayan, is_staff=True) getirir.
    """
    draw = int(request.GET.get('draw', '1'))
    length = int(request.GET.get('length', '25'))
    start = int(request.GET.get('start', '0'))
    search_value = request.GET.get('search[value]', '').strip().lower()

    order_column = request.GET.get('columns[' + request.GET.get('order[0][column]', '0') + '][data]')
    order_dir = request.GET.get('order[0][dir]', 'desc')

    if not order_column:
        order_column = "date_joined"
    order_by = ('-' if order_dir == 'desc' else '') + order_column

    # Sadece Konasoft Personelleri: is_staff=True, store_id=None
    qs = (Users.objects
          .filter(is_deleted=False, is_staff=True, is_superuser=False, store_id__isnull=True)
          .values('id', 'username', 'first_name', 'last_name', 'email',
                  'mobile_phone', 'avatar', 'date_joined',
                  'is_active', 'role__name', 'role_id'))  # role_id'yi edit için ekledik

    total = qs.count()

    if search_value:
        qs = qs.filter(
            Q(username__icontains=search_value) |
            Q(email__icontains=search_value) |
            Q(first_name__icontains=search_value) |
            Q(last_name__icontains=search_value)
        )

    count = qs.count()

    if str(length) == '-1':
        data = list(qs.order_by(order_by))
    else:
        data = list(qs.order_by(order_by)[start:start + length])

    # Tarih formatlama
    for row in data:
        row['date_joined'] = _fmt_ts(row.get('date_joined'))

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": count,
        "data": data
    })


@login_required(login_url='login')
@require_POST
def staffs_add(request):
    """
    Personel Ekleme / Düzenleme
    """
    record_id = request.POST.get('record_id')

    first_name = (request.POST.get('first_name') or '').strip()
    last_name = (request.POST.get('last_name') or '').strip()
    username = (request.POST.get('username') or '').strip()
    email = (request.POST.get('email') or '').strip()
    mobile = (request.POST.get('mobile_phone') or '').strip()
    role_id = request.POST.get('role_id')
    password = (request.POST.get('password') or '').strip()
    avatar = request.FILES.get('avatar')

    if not username or not email:
        return JsonResponse({"error": True, "error_msg": "Kullanıcı adı ve e-posta zorunludur."})

    if not role_id:
        return JsonResponse({"error": True, "error_msg": "Lütfen bir rol seçiniz."})

    # --- DÜZENLEME İŞLEMİ ---
    if record_id:
        user = get_object_or_404(Users, id=record_id, is_deleted=False)

        # Unique kontrolü (kendisi hariç)
        if Users.objects.filter(username=username).exclude(id=user.id).exists():
            return JsonResponse({"error": True, "error_msg": "Bu kullanıcı adı kullanımda."})
        if Users.objects.filter(email=email).exclude(id=user.id).exists():
            return JsonResponse({"error": True, "error_msg": "Bu e-posta kullanımda."})

        user.first_name = first_name
        user.last_name = last_name
        user.username = username
        user.email = email
        user.mobile_phone = mobile
        user.is_staff = True  # Panel erişimi için zorunlu
        user.role_id = role_id  # Rol ataması

        if avatar:
            user.avatar = avatar

        # Şifre sadece girildiyse güncellenir
        if password:
            user.set_password(password)

        user.save()
        write_log(request, "Kullanıcılar", f"Personel güncellendi: {user.username}")
        return JsonResponse({"result": True})

    # --- YENİ KAYIT İŞLEMİ ---
    if Users.objects.filter(username=username).exists():
        return JsonResponse({"error": True, "error_msg": "Bu kullanıcı adı zaten var."})
    if Users.objects.filter(email=email).exists():
        return JsonResponse({"error": True, "error_msg": "Bu e-posta zaten kayıtlı."})
    if not password:
        return JsonResponse({"error": True, "error_msg": "Yeni personel için şifre zorunludur."})

    user = Users(
        first_name=first_name,
        last_name=last_name,
        username=username,
        email=email,
        mobile_phone=mobile,
        is_active=True,
        is_staff=True,  # Panele girebilir
        is_superuser=False,  # Ama superuser değil
        is_deleted=False,
        role_id=role_id,  # Seçilen rol
        store_id=None  # Konasoft personeli olduğu için store yok
    )
    if avatar:
        user.avatar = avatar
    user.set_password(password)
    user.save()

    write_log(request, "Kullanıcılar", f"Yeni personel eklendi: {user.username}")
    return JsonResponse({"result": True})


@login_required(login_url='login')
@require_POST
def staffs_delete(request):
    ids = request.POST.getlist('ids[]')
    try:
        Users.objects.filter(id__in=ids).update(is_deleted=True, is_active=False)
        return JsonResponse({"result": True})
    except Exception as exc:
        return JsonResponse({"error": True, "error_msg": str(exc)})


@login_required(login_url='login')
@require_POST
def staffs_change_status(request):
    ids = request.POST.getlist('ids[]')
    try:
        for u in Users.objects.filter(id__in=ids, is_deleted=False):
            u.is_active = not u.is_active
            u.save(update_fields=['is_active'])
        return JsonResponse({"result": True})
    except Exception as exc:
        return JsonResponse({"error": True, "error_msg": str(exc)})


@login_required(login_url='login')
def staffs_detail_view(request, user_id: int):
    user = get_object_or_404(Users, id=user_id, is_deleted=False, is_staff=True)
    ctx = {
        "title": "Kullanıcı Detayı",
        "record": user,
    }
    write_log(request, "Kullanıcılar", f"Detay görüntülendi • id={user.id}")
    return render(request, "management/accounts/staffs_detail.html", ctx)
