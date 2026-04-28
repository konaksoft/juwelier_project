import json
import logging
import re
import string
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q, Min
from django.http import JsonResponse, HttpResponseForbidden, HttpRequest, HttpResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from apps.accounts.views import normalize_tr_msisdn  # Telefon normalize fonksiyonu

from apps.activity_logs.views import write_log
from apps.counts.models import *  # Customers vb. için
from apps.definitions.contracts.models import *
# --- FAZ 4: StockSnapshot ve StockLedger entegrasyonu ---
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.orders.models import *
from apps.pavo.models import PavoTerminal
from apps.process.models import Process
from apps.products.models import Products
from apps.repairs.models import Repairs
from apps.chambers.models import Chambers
from apps.roles.decorators import role_required
from apps.scraps.models import Scraps
from apps.settings.send_mail import EmailService
from apps.stores.models import *
from apps.stores.models import Stores, StoreModule
from apps.stores.services import sync_store_modules, get_store_effective_permission_ids
from apps.crm.packages.models import SaaSModule
from apps.suppliers.models import Suppliers
from apps.whatsapp.models import *
from apps.whatsapp.services import get_usage_snapshot, send_whatsapp_template_guarded, wa_preflight
from apps.definitions.locations.models import *
from django.core.exceptions import ValidationError
from apps.settings.models import StoreLabelSettings

log = logging.getLogger(__name__)


def _can_admin_store(user):
    return user.is_superuser or user.is_staff


def _resolve_store_for_request(request):
    sid = request.GET.get("store_id") or request.POST.get("store_id")
    user_store = getattr(request.user, "store", None)
    if sid:
        try:
            target = Stores.objects.get(id=sid, is_deleted=False)
        except Stores.DoesNotExist:
            return None
        if request.user.is_superuser or (user_store and user_store.id == target.id):
            return target
        return None
    return user_store


def _generate_unique_store_id():
    while True:
        s_id = str(random.randint(10 ** 10, 10 ** 11 - 1))
        if not Stores.objects.filter(store_id=s_id).exists():
            return s_id


def _add_protected_products_to_inventory(store):
    """Korumalı ürünler için StockSnapshot kayıtları oluşturur."""
    protected_products = Products.objects.filter(is_protected=True)
    new_snapshot_records = []
    for product in protected_products:
        if not StockSnapshot.objects.filter(product=product, store=store).exists():
            stock_gram = Decimal('0.0000')
            stock_pieces = 0
            new_snapshot_records.append(
                StockSnapshot(product=product, store=store, stock_pieces=stock_pieces, stock_gram=stock_gram)
            )
    if new_snapshot_records:
        StockSnapshot.objects.bulk_create(new_snapshot_records)


@login_required(login_url='login')
@role_required('STORES_STORES_VIEW')
def stores_view(request):
    context = {
        'title': 'Mağaza Tanımları',
        'stores': Stores.objects.filter(is_deleted=False),
        'packages': Packages.objects.filter(is_active=True).order_by('order', 'name'),
        'available_chambers': Chambers.objects.filter(is_active=True, is_deleted=False).order_by('name'),
    }
    write_log(request, 'Mağazalar', 'Mağazalar Görüntülendi.')
    return render(request, 'management/stores/index.html', context)


@login_required(login_url='login')
@role_required("STORES_ADD_STORE")
def add_store(request):
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    if request.method != "POST":
        return JsonResponse({"error": True, "error_msg": "Geçersiz istek."})

    company_id = (request.POST.get("company_id") or "").strip() or None
    record_id = request.POST.get("record_id")

    title = (request.POST.get("title") or "").strip()
    email = (request.POST.get("email") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    subscription_start = (request.POST.get("subscription_start") or "").strip()
    description = (request.POST.get("description") or "").strip()
    avatar = request.FILES.get("avatar")
    package_id = request.POST.get("package_id")
    chamber_ids = request.POST.getlist("chambers")
    company_title = (request.POST.get("company_title") or title or "").strip()
    company_email = (request.POST.get("company_email") or email or "").strip()
    company_phone = (request.POST.get("company_phone") or phone or "").strip()
    tax_number = (request.POST.get("tax_number") or "").strip()
    e_invoice_type = (request.POST.get("e_invoice_type") or "TEMEL").strip()
    address = (request.POST.get("address") or "").strip()
    postal_code = (request.POST.get("postal_code") or "").strip()
    country = (request.POST.get("country") or "Türkiye").strip()
    req_city_id = request.POST.get("city")
    req_district_id = request.POST.get("district")
    req_tax_office_id = request.POST.get("tax_office")
    req_tax_office_code = request.POST.get("tax_office_code")
    city_val = ""
    district_val = ""
    tax_office_val = ""
    tax_office_code_val = req_tax_office_code

    if req_city_id:
        try:
            c_obj = City.objects.get(id=req_city_id)
            city_val = c_obj.name
        except (City.DoesNotExist, ValueError, ValidationError):
            city_val = req_city_id

    if req_district_id:
        try:
            d_obj = District.objects.get(id=req_district_id)
            district_val = d_obj.name
        except (District.DoesNotExist, ValueError, ValidationError):
            district_val = req_district_id

    if req_tax_office_id:
        try:
            t_obj = TaxOffice.objects.get(id=req_tax_office_id)
            tax_office_val = t_obj.name
            if t_obj.code:
                tax_office_code_val = t_obj.code
        except (TaxOffice.DoesNotExist, ValueError, ValidationError):
            tax_office_val = req_tax_office_id

    iban = (request.POST.get("iban") or "").strip()
    mersis_no = (request.POST.get("mersis_no") or "").strip()
    trade_registry_no = (request.POST.get("trade_registry_no") or "").strip()

    mersis_no_digits = re.sub(r"\D", "", mersis_no)
    if mersis_no and not (10 <= len(mersis_no_digits) <= 20):
        return JsonResponse({"error": True, "error_msg": "MERSİS No 10–20 hane olmalı."})

    if tax_office_code_val:
        tax_office_code_digits = re.sub(r"\D", "", str(tax_office_code_val))
    else:
        tax_office_code_digits = ""

    if record_id and not company_id:
        try:
            company = Company.objects.get(id=record_id, is_deleted=False)
        except Company.DoesNotExist:
            return JsonResponse({"error": True, "error_msg": "Firma bulunamadı."})

        if tax_number and Company.objects.filter(tax_number=tax_number).exclude(id=company.id).exists():
            return JsonResponse({"error": True, "error_msg": "Bu VKN/TCKN başka bir firmada kayıtlı."})

        company.title = company_title or company.title
        company.email = company_email or None
        company.phone = company_phone or None
        company.e_invoice_type = e_invoice_type or company.e_invoice_type or "TEMEL"

        if address: company.address = address
        if city_val: company.city = city_val
        if district_val: company.district = district_val
        if postal_code: company.postal_code = postal_code
        if country: company.country = country
        if tax_office_val: company.tax_office = tax_office_val
        if tax_office_code_digits: company.tax_office_code = tax_office_code_digits
        if iban: company.iban = iban
        if tax_number: company.tax_number = tax_number
        if mersis_no: company.mersis_no = mersis_no_digits
        if trade_registry_no: company.trade_registry_no = trade_registry_no
        if avatar: company.avatar = avatar
        if description: company.description = description

        company.save()
        company.chambers.set(chamber_ids)
        write_log(request, "Firmalar", "FİRMA GÜNCELLENDİ. ID=" + str(company.id))
        return JsonResponse({"result": True})

    if not record_id and not company_id:
        if tax_number and Company.objects.filter(tax_number=tax_number).exists():
            return JsonResponse({"error": True, "error_msg": "Bu VKN/TCKN zaten kayıtlı."})

        company = Company.objects.create(
            title=company_title or None,
            email=company_email or None,
            phone=company_phone or None,
            e_invoice_type=e_invoice_type or "TEMEL",
            tax_number=tax_number or None,
            description=description or None,
            avatar=avatar if avatar else None,
            address=address or None,
            city=city_val or None,
            district=district_val or None,
            postal_code=postal_code or None,
            country=country or "Türkiye",
            tax_office=tax_office_val or None,
            tax_office_code=tax_office_code_digits or None,
            iban=iban or None,
            mersis_no=mersis_no_digits or None,
            trade_registry_no=trade_registry_no or None,
        )
        if chamber_ids:
            company.chambers.set(chamber_ids)
        write_log(request, "Firmalar", "FİRMA EKLENDİ. ID=" + str(company.id))
        return JsonResponse({"result": True})

    company = get_object_or_404(Company, id=company_id, is_deleted=False)

    # Paket opsiyoneldir — müşteri sadece modül seçerek de mağaza açabilir
    package = None
    if package_id:
        try:
            package = Packages.objects.get(id=package_id, is_active=True)
        except Packages.DoesNotExist:
            return JsonResponse({"error": True, "error_msg": "Seçilen paket bulunamadı veya aktif değil."})

    # POST'tan gelen modül ID'leri (paket varsa ek modül, yoksa ana modül listesi)
    extra_module_ids = request.POST.getlist("extra_module_ids[]")

    if record_id:
        try:
            store = Stores.objects.get(id=record_id, is_deleted=False)
        except Stores.DoesNotExist:
            return JsonResponse({"error": True, "error_msg": "Mağaza bulunamadı."})

        if email and Stores.objects.filter(email=email).exclude(id=store.id).exists():
            return JsonResponse({"error": True, "error_msg": "Bu e-posta başka bir mağazada kayıtlı."})

        store.company = company
        if title: store.title = title
        if email: store.email = email
        if phone: store.phone = phone
        if description: store.description = description
        if subscription_start: store.subscription_start = subscription_start
        if avatar: store.avatar = avatar
        store.package = package
        store.save()

        # Modülleri senkronize et (paketsiz ise bu tek yetki kaynağı)
        sync_store_modules(store, extra_module_ids)

        write_log(request, "Mağazalar", "MAĞAZA GÜNCELLENDİ. ID=" + str(store.id))
        return JsonResponse({"result": True})

    if email and Stores.objects.filter(email=email).exists():
        return JsonResponse({"error": True, "error_msg": "Mağaza zaten kaydedilmiş."})

    try:
        store = Stores.objects.create(
            store_id=_generate_unique_store_id(),
            company=company,
            package=package,
            title=title or None,
            email=email or None,
            phone=phone or None,
            subscription_start=subscription_start or None,
            description=description or None,
            avatar=avatar if avatar else None,
            address=company.address,
            city=company.city,
            district=company.district,
            postal_code=company.postal_code,
            country=company.country,
            is_active=False,
        )

        _add_protected_products_to_inventory(store)

        # Modül ataması (paketsiz ise bu tek yetki kaynağı olur)
        if extra_module_ids:
            sync_store_modules(store, extra_module_ids)

        write_log(request, "Mağazalar", "MAĞAZA EKLENDİ. ID=" + str(store.id))

        if email:
            EmailService.send(
                user=store,
                subject="Yeni Mağaza Kaydı",
                template_name="management/mail_templates/record_mail.html",
                context={"store_id": store.store_id}
            )

        return JsonResponse({"result": True})
    except Exception as exc:
        return JsonResponse({"error": True, "error_msg": str(exc)})


# ─────────────────────────────────────────────────────────────────────────
# FAZ 19 — Hızlı Onboarding (Fast-Track) View'ları
#
# create_demo_store_view → DEMO mağaza açar (Konasoft personeli)
# demo_convert_view      → DEMO/PENDING → ACTIVE dönüşümü (gerçek paketle)
# demo_extend_view       → Demo süresini uzatır (1-90 gün)
#
# Hepsi @transaction.atomic koruması altında çalışan servislere delegasyon
# yapar. Yetki: yalnızca Konasoft personeli (is_superuser veya is_staff).
# ─────────────────────────────────────────────────────────────────────────
@login_required(login_url='login')
def create_demo_store_view(request):
    """
    POST /stores/create-demo
    Hızlı Onboarding ile DEMO mağaza açar.
    """
    if not _can_admin_store(request.user):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    if request.method == "GET":
        context = {
            "title": "Demo Mağaza Aç",
            "available_modules": SaaSModule.objects.filter(is_active=True)
                .exclude(slug='demo-access').order_by('order', 'name'),
            "default_duration": 14,
        }
        return render(request, 'management/stores/demo_create.html', context)

    if request.method != "POST":
        return JsonResponse({"error": True, "error_msg": "Geçersiz istek."})

    first_name    = (request.POST.get("first_name") or "").strip()
    last_name     = (request.POST.get("last_name") or "").strip()
    phone         = (request.POST.get("phone") or "").strip()
    email         = (request.POST.get("email") or "").strip()
    business_name = (request.POST.get("business_name") or "").strip()
    city          = (request.POST.get("city") or "").strip()
    module_ids    = request.POST.getlist("module_ids[]") or request.POST.getlist("module_ids")
    duration_days = request.POST.get("duration_days") or 14

    if not phone:
        return JsonResponse({"error": True, "error_msg": "Telefon zorunludur."})
    if not business_name:
        return JsonResponse({"error": True, "error_msg": "Firma/mağaza adı zorunludur."})

    try:
        duration_days = int(duration_days)
    except (TypeError, ValueError):
        duration_days = 14

    try:
        from apps.stores.services import create_demo_store
        result = create_demo_store(
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email=email or '',
            business_name=business_name,
            city=city or '',
            module_ids=module_ids or None,
            created_by=request.user,
            duration_days=duration_days,
        )
    except Exception as exc:
        log.exception("create_demo_store_view: servis çağrısı başarısız.")
        return JsonResponse({"error": True, "error_msg": str(exc)})

    if result.get('error'):
        return JsonResponse({"error": True, "error_msg": result['error']})

    store = result.get('store')
    if not store:
        return JsonResponse({"error": True, "error_msg": "Mağaza oluşturulamadı."})

    write_log(
        request, "Demo",
        f"DEMO MAĞAZA AÇILDI: {store.store_id} | Firma: {business_name} | "
        f"Süre: {duration_days} gün | Yeni: {result.get('is_new')}"
    )

    return JsonResponse({
        "result": True,
        "store_id": store.store_id,
        "store_uuid": str(store.id),
        "expires_at": store.demo_expires_at.isoformat() if store.demo_expires_at else None,
        "is_new": bool(result.get('is_new')),
    })


@login_required(login_url='login')
@require_POST
def demo_convert_view(request, store_id):
    """
    POST /stores/demo-convert/<store_id>
    DEMO/PENDING_PAYMENT mağazayı ücretli pakete (ACTIVE) dönüştürür.
    """
    if not _can_admin_store(request.user):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    try:
        store = Stores.objects.get(id=store_id, is_deleted=False)
    except Stores.DoesNotExist:
        return JsonResponse({"error": True, "error_msg": "Mağaza bulunamadı."})

    if store.status not in ('DEMO', 'PENDING_PAYMENT', 'EXPIRED'):
        return JsonResponse({
            "error": True,
            "error_msg": f"Bu mağaza dönüştürülemez (mevcut statü: {store.status})."
        })

    package_id          = (request.POST.get("package_id") or "").strip()
    module_ids          = request.POST.getlist("module_ids[]") or request.POST.getlist("module_ids")
    subscription_start  = (request.POST.get("subscription_start") or "").strip() or None

    if not package_id:
        return JsonResponse({"error": True, "error_msg": "Paket seçimi zorunludur."})

    try:
        package = Packages.objects.get(id=package_id, is_active=True, is_demo=False)
    except Packages.DoesNotExist:
        return JsonResponse({
            "error": True,
            "error_msg": "Seçilen paket bulunamadı, demo paketi veya pasif."
        })

    try:
        from apps.stores.services import convert_demo_to_active
        convert_demo_to_active(
            store=store,
            package=package,
            module_ids=module_ids or None,
            subscription_start=subscription_start,
            converted_by=request.user,
        )
    except ValueError as exc:
        return JsonResponse({"error": True, "error_msg": str(exc)})
    except Exception as exc:
        log.exception("demo_convert_view: dönüşüm başarısız. store_id=%s", store.store_id)
        return JsonResponse({"error": True, "error_msg": str(exc)})

    write_log(
        request, "Demo",
        f"DEMO → ACTIVE DÖNÜŞÜMÜ: {store.store_id} | Paket: {package.code}"
    )

    return JsonResponse({
        "result": True,
        "store_id": store.store_id,
        "package_code": package.code,
    })


@login_required(login_url='login')
@require_POST
def demo_extend_view(request, store_id):
    """
    POST /stores/demo-extend/<store_id>
    DEMO mağazanın süresini uzatır (1-90 gün arası).
    """
    if not _can_admin_store(request.user):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    try:
        store = Stores.objects.get(id=store_id, is_deleted=False)
    except Stores.DoesNotExist:
        return JsonResponse({"error": True, "error_msg": "Mağaza bulunamadı."})

    if store.status != 'DEMO':
        return JsonResponse({
            "error": True,
            "error_msg": "Sadece DEMO statüsündeki mağazalar uzatılabilir."
        })

    try:
        extra_days = int(request.POST.get("extra_days") or 0)
    except (TypeError, ValueError):
        return JsonResponse({"error": True, "error_msg": "Geçersiz gün sayısı."})

    if extra_days < 1 or extra_days > 90:
        return JsonResponse({
            "error": True,
            "error_msg": "Uzatma süresi 1 ile 90 gün arasında olmalıdır."
        })

    try:
        with transaction.atomic():
            base = store.demo_expires_at or timezone.now()
            # Süresi geçmişse şimdiden başlat
            if base < timezone.now():
                base = timezone.now()
            store.demo_expires_at = base + timedelta(days=extra_days)
            # Süresi dolmuşsa mağazayı yeniden DEMO'ya çek
            if not store.is_active:
                store.is_active = True
            store.save(update_fields=['demo_expires_at', 'is_active'])
    except Exception as exc:
        log.exception("demo_extend_view: uzatma başarısız. store_id=%s", store.store_id)
        return JsonResponse({"error": True, "error_msg": str(exc)})

    write_log(
        request, "Demo",
        f"DEMO UZATILDI: {store.store_id} | +{extra_days} gün | "
        f"Yeni Bitiş: {store.demo_expires_at.isoformat()}"
    )

    return JsonResponse({
        "result": True,
        "store_id": store.store_id,
        "new_expires_at": store.demo_expires_at.isoformat(),
        "days_remaining": store.demo_days_remaining,
    })


@login_required
@require_POST
@role_required('STORES_SEND_STORE_VERIFICATION')
def send_store_verification(request, store_id):
    store = get_object_or_404(Stores, pk=store_id, is_deleted=False)
    channel = request.POST.get('channel')

    if channel not in ('email', 'phone'):
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz kanal'})

    # 6 Haneli Kod Üret
    code = ''.join(random.choice(string.digits) for _ in range(6))

    # OTP Kaydı Oluştur
    OtpCode.objects.create(
        owner_type='store',
        owner_id=str(store.id),
        channel=channel,
        code=code,
        purpose=f'verify_{channel}',
        expires_at=timezone.now() + timedelta(minutes=10)
    )

    # Hedef Bilgiler
    target_email = store.email
    target_phone = store.phone

    if channel == 'email':
        if not target_email:
            return JsonResponse({'error': True, 'error_msg': 'E-posta bulunamadı'})

        # --- E-POSTA GÖNDERİMİ (MERKEZİ SERVİS) ---
        # İYS doğrulama olduğu için 'notify_email_contact_verify' anahtarını kullanıyoruz.
        EmailService.send(
            user=store,
            subject='İYS İletişim Onayı • Doğrulama Kodu',
            template_name='management/mail_templates/verify_contact_iys.html',
            context={
                'subject': 'İYS İletişim Onayı • Doğrulama Kodu',
                'otp_string': code,
                'user': store,
                'verify_url': request.build_absolute_uri('/verify'),
                'consent_scope': 'E-posta',
            },
            config_key='notify_email_contact_verify'
        )
        # ------------------------------------------

    else:
        # WhatsApp Gönderimi
        if not target_phone:
            return JsonResponse({'error': True, 'error_msg': 'Telefon bulunamadı'})

        to = normalize_tr_msisdn(target_phone)
        can, _, lang = wa_preflight(store, "verify_phone_v1", "tr_TR")

        if can:
            send_whatsapp_template_guarded(
                store=store,
                user=None,
                customer=None,
                to=to,
                template="verify_phone_v1",
                language=lang,
                header_params=None,
                body_params=[code],
                button_params=[code],
                validate=False
            )

    return JsonResponse({'result': True})


@login_required
@require_POST
@role_required('STORES_CONFIRM_STORE_VERIFICATION')
def confirm_store_verification(request, store_id):
    store = get_object_or_404(Stores, pk=store_id, is_deleted=False)
    channel = request.POST.get('channel')
    code = request.POST.get('code', '')
    now = timezone.now()
    ok = OtpCode.objects.filter(
        owner_type='store', owner_id=str(store.id), channel=channel,
        purpose=f'verify_{channel}', code=code, used=False, expires_at__gt=now
    ).exists()
    if not ok:
        return JsonResponse({'error': True, 'error_msg': 'Kod geçersiz veya süresi dolmuş.'})
    OtpCode.objects.filter(
        owner_type='store', owner_id=str(store.id), channel=channel,
        purpose=f'verify_{channel}', code=code
    ).update(used=True)
    if channel == 'email':
        store.is_email_verified = True
        store.save(update_fields=['is_email_verified'])
    else:
        store.is_phone_verified = True
        store.save(update_fields=['is_phone_verified'])
    return JsonResponse({'result': True})


def _get_single_branch_or_none(store: Stores):
    qs = store.branches.filter(is_deleted=False, is_active=True)
    return qs.first() if qs.count() == 1 else None


@login_required(login_url='login')
@role_required('STORES_DELETE')
@require_POST
@role_required('STORES_DELETE')
def delete(request):  # Firma soft delete
    if not request.user.is_superuser:
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")
    ids = request.POST.getlist('ids[]')
    try:
        records = Company.objects.filter(id__in=ids, is_deleted=False)
        for record in records:
            record.is_deleted = True
            record.save(update_fields=['is_deleted'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('STORES_DELETE')
@require_POST
@role_required('STORES_STORE_DELETE')
def store_delete(request):  # Mağaza soft delete (firma filtresiyle)
    if not request.user.is_superuser:
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")
    ids = request.POST.getlist('ids[]') or [request.POST.get('id')]
    company_id = (request.POST.get('company_id') or '').strip() or None
    try:
        qs = Stores.objects.filter(id__in=ids, is_deleted=False)
        if company_id:
            qs = qs.filter(company_id=company_id)
        updated = qs.update(is_deleted=True)
        return JsonResponse({'result': True, 'count': updated})
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@user_passes_test(lambda u: u.is_superuser)
@role_required('STORES_HARD_DELETE')
def hard_delete(request):  # Firma hard delete
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})
    id = request.POST.get('id')
    try:
        record = Company.objects.get(id=id)
        record.delete()
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


from django.db import transaction
from django.shortcuts import get_object_or_404


# Diğer importlar...

@login_required(login_url='login')
@user_passes_test(lambda u: u.is_superuser)
@require_POST
def hard_data_delete(request):
    """
    FAZ 9.1 — MAĞAZA SIFIRLAMA (Hard Reset)
    Mağaza kaydı kalır, içindeki tüm veriler silinir.

    Silme sırası (14 katman): Çocuk tablolar → Ebeveyn tablolar.
    ─────────────────────────────────────────────────────────────
    PROTECT FK'lar:
      • StockSnapshot.product → Products : PROTECT
      • StockLedger.product   → Products : PROTECT
      • Proposals.created_by  → Users    : PROTECT
    Bu FK'lar, sahip oldukları tablolar ilgili ebeveyn tablodan ÖNCE
    silinmezse ProtectedError fırlatır.

    Payment tablosu: process_group FK + process_no fallback (çift sorgu).
    Customers: M2M — exclusive sil, shared clear.
    Users: Süper admin, protected ve isteği yapan kullanıcı korunur.
    """
    store_id = request.POST.get('id')
    if not store_id:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'ID parametresi eksik.'})

    try:
        with transaction.atomic():
            store = get_object_or_404(Stores, id=store_id)

            # ── 1. PROTECT FK'lı tablolar ──────────────────────────────────
            #    StockSnapshot.product ve StockLedger.product = PROTECT.
            #    Hem store bazlı hem product bazlı silme: çapraz mağaza
            #    snapshot/ledger kayıtlarını da temizler (transfer senaryosu).
            store_product_ids = list(
                Products.objects.filter(store=store).values_list('id', flat=True)
            )

            StockLedger.objects.filter(store=store).delete()
            StockSnapshot.objects.filter(store=store).delete()

            if store_product_ids:
                StockLedger.objects.filter(product_id__in=store_product_ids).delete()
                StockSnapshot.objects.filter(product_id__in=store_product_ids).delete()

            # ── 2. Fatura Zinciri ───────────────────────────────────────────
            #    InvoiceSyncLog, InvoicePaymentAllocation, InvoiceItem
            #    hepsi Invoice'dan CASCADE. Güvenlik için açık sil.
            from apps.invoices.models import (
                Invoice, InvoiceSequence, InvoiceSyncLog,
                InvoicePaymentAllocation, StoreEInvoiceSettings,
                EInvoiceCreditRequest,
            )
            InvoiceSyncLog.objects.filter(store=store).delete()
            store_invoices = Invoice.objects.filter(store=store)
            InvoicePaymentAllocation.objects.filter(invoice__in=store_invoices).delete()
            store_invoices.delete()
            InvoiceSequence.objects.filter(store=store).delete()

            # ── 3. İşlem Zinciri (Payment → Process → ProcessGroup) ────────
            from apps.process.models import Payment, ProcessGroup

            # 3a. process_group FK'sı dolu olanlar
            Payment.objects.filter(process_group__store=store).delete()

            # 3b. process_group NULL olanlar (process_no fallback)
            store_process_nos = list(
                Process.objects.filter(store=store)
                .values_list('process_no', flat=True)
                .distinct()
            )
            if store_process_nos:
                Payment.objects.filter(
                    process_no__in=store_process_nos,
                    process_group__isnull=True,
                ).delete()

            Process.objects.filter(store=store).delete()
            ProcessGroup.objects.filter(store=store).delete()

            # ── 4. Ürüne bağlı modüller ────────────────────────────────────
            try:
                from apps.gold_purchases.models import GoldPurchases
                GoldPurchases.objects.filter(store=store).delete()
            except ImportError:
                pass

            Scraps.objects.filter(store=store).delete()

            try:
                from apps.bracelets.models import Bracelets
                Bracelets.objects.filter(store=store).delete()
            except ImportError:
                pass

            # ── 5. Eski (Legacy) Tablolar ───────────────────────────────────
            try:
                from apps.inventories.models import Inventories, InventoryMovement
                InventoryMovement.objects.filter(store=store).delete()
                Inventories.objects.filter(store=store).delete()
            except ImportError:
                pass

            # ── 6. Sayım Tabloları ──────────────────────────────────────────
            try:
                from apps.counts.models import InventoryCountSession
                InventoryCountSession.objects.filter(store=store).delete()
            except ImportError:
                pass

            # ── 7. Bankacılık Tabloları ─────────────────────────────────────
            try:
                from apps.banking.models import (
                    BankTransaction, BankAccount, EsurecTenantCredential,
                )
                BankTransaction.objects.filter(store=store).delete()
                BankAccount.objects.filter(store=store).delete()
                EsurecTenantCredential.objects.filter(store=store).delete()
            except ImportError:
                pass

            # ── 8. Müşteriye / Tedarikçiye bağlı modüller ──────────────────
            Repairs.objects.filter(store=store).delete()

            from apps.suppliers.models import SupplierLedger
            SupplierLedger.objects.filter(supplier__store=store).delete()

            # ── 9. Sipariş Tabloları (OrderItem CASCADE from Order) ─────────
            try:
                Order.objects.filter(store=store).delete()
            except Exception:
                pass

            # ── 10. E-Fatura Ayarları ───────────────────────────────────────
            try:
                StoreEInvoiceSettings.objects.filter(store=store).delete()
                EInvoiceCreditRequest.objects.filter(store=store).delete()
            except Exception:
                pass

            # ── 11. Ebeveyn: Products ───────────────────────────────────────
            Products.objects.filter(store=store).delete()

            # ── 12. Ebeveyn: Customers (M2M ilişki) ────────────────────────
            #    Customers.store = ManyToManyField. Doğrudan .delete()
            #    müşteriyi TÜM mağazalardan kaldırır.
            #    Çözüm: exclusive → sil, shared → M2M bağını kopar.
            from django.db.models import Count
            exclusive_customer_ids = list(
                Customers.objects.filter(store=store)
                .annotate(store_count=Count('store'))
                .filter(store_count=1)
                .values_list('id', flat=True)
            )
            store.customers.clear()
            if exclusive_customer_ids:
                Customers.objects.filter(id__in=exclusive_customer_ids).delete()

            # ── 13. Bağımsız modüller ───────────────────────────────────────
            Suppliers.objects.filter(store=store).delete()
            Workshops.objects.filter(store=store).delete()

            # ── 14. Personel (Users) ────────────────────────────────────────
            #    Proposals.created_by = PROTECT → Users silinmeden önce
            #    bu FK NULL yapılmalı, aksi halde ProtectedError fırlar.
            #    Korunan kullanıcılar: superuser, is_protected, request.user.
            from apps.accounts.models import Users
            users_to_delete = Users.objects.filter(
                store=store,
                is_superuser=False,
                is_protected=False,
            ).exclude(id=request.user.id)

            try:
                from apps.crm.proposals.models import Proposals
                Proposals.objects.filter(
                    created_by__in=users_to_delete
                ).update(created_by=None)
            except ImportError:
                pass

            users_to_delete.delete()

            write_log(request, "Mağazalar", f"MAĞAZA SIFIRLANDI. ID: {store.store_id}")

        return JsonResponse({'result': True})

    except Exception as e:
        log.exception("hard_data_delete hatası — store_id=%s", store_id)
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@user_passes_test(lambda u: u.is_superuser)
@require_POST
def store_hard_delete(request):
    """
    MAĞAZA KALICI SİLME: Mağazayı ve ona bağlı her şeyi veritabanından siler.
    """
    store_id = request.POST.get('id')
    if not store_id:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'ID parametresi eksik.'})

    try:
        with transaction.atomic():
            store = get_object_or_404(Stores, id=store_id)

            # Önce bağlı hareketleri temizle (Database integrity hatası almamak için)
            StockLedger.objects.filter(store=store).delete()
            # Diğer modellerde on_delete=CASCADE varsa store.delete() hepsini siler.
            # Ancak garanti olması için yukarıdaki gibi kritik tabloları temizleyebilirsiniz.

            title = store.title
            store.delete()

            write_log(request, "Mağazalar", f"MAĞAZA KALICI SİLİNDİ. Başlık: {title}")

        return JsonResponse({'result': True})

    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('STORES_CHANGE_STATUS')
def change_status(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})
    ids = request.POST.getlist('ids[]')
    try:
        for record in Company.objects.filter(id__in=ids, is_deleted=False):
            record.is_active = not record.is_active
            record.save(update_fields=['is_active'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('STORES_GET_ALL')
def get_all(request):
    draw = int(request.GET.get('draw', '1'))
    length = int(request.GET.get('length', '25'))
    start = int(request.GET.get('start', '0'))
    search_value = (request.GET.get('search[value]') or '').strip()
    order_column = request.GET.get('columns[' + request.GET.get('order[0][column]', '0') + '][data]')
    order = request.GET.get('order[0][dir]', 'desc')
    company_id = request.GET.get('company_id') or None

    # Varsayılan sıralama alanı: title
    if not order_column:
        order_column = "title"
    if order == 'desc':
        order_column = '-' + order_column

    if company_id:
        queryset = (Stores.objects
                    .filter(is_deleted=False, company_id=company_id)
                    .values('id', 'title', 'store_id', 'email', 'phone',
                            'subscription_start', 'is_active'))
        total = queryset.count()
        if search_value:
            queryset = queryset.filter(
                Q(store_id__icontains=search_value) |
                Q(title__icontains=search_value) |
                Q(email__icontains=search_value) |
                Q(phone__icontains=search_value)
            )
    else:
        queryset = (Company.objects
                    .filter(is_deleted=False)
                    .annotate(subscription_start=Min('stores__subscription_start'))
                    .values('id', 'title', 'email', 'phone', 'tax_number',
                            'is_active', 'avatar', 'description', 'subscription_start'))
        total = queryset.count()
        if search_value:
            queryset = queryset.filter(
                Q(title__icontains=search_value) |
                Q(email__icontains=search_value) |
                Q(phone__icontains=search_value) |
                Q(tax_number__icontains=search_value)
            )

    count = queryset.count()
    if str(length) == '-1':
        page_qs = queryset.order_by(order_column)
    else:
        page_qs = queryset.order_by(order_column)[start:start + length]

    page_list = list(page_qs)
    if not company_id:  # Sadece firma listesinde dernek gösterilir, mağaza listesinde değil
        company_ids = [item['id'] for item in page_list]
        companies_with_chambers = Company.objects.prefetch_related('chambers').filter(id__in=company_ids)
        chamber_map = {comp.id: ", ".join([c.name for c in comp.chambers.all()]) for comp in companies_with_chambers}

        for item in page_list:
            item['chamber_names'] = chamber_map.get(item['id']) or "-"

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": page_list
    })


# apps/stores/views.py

@login_required(login_url='login')
@role_required('STORES_DETAIL_VIEW')
def detail_view(request, record_id):
    company = get_object_or_404(Company, id=record_id, is_deleted=False)

    # Sadece bu firmaya ait süreçleri getir
    contract_processes = ContractProcess.objects.filter(company=company).order_by('-created_at')

    # Sistemsel Sözleşme Şablonları (Aktif olanlar)
    available_contracts = Contracts.objects.filter(is_deleted=False, is_active=True)

    # Sadece bu firmaya ait ve silinmemiş teklifleri getir (Modal için)
    # Eğer Proposals modelinizde 'is_deleted' alanı varsa ekleyin
    company_proposals = company.proposals.filter(is_deleted=False).order_by('-created_at')

    # ── e-Süreç Entegrasyon Bilgileri (Automated Provisioning) ─────
    stores_integration = []
    if request.user.is_superuser:
        from apps.banking.models import EsurecTenantCredential
        for store in company.stores.filter(is_deleted=False).order_by('title'):
            cred = EsurecTenantCredential.objects.filter(store=store).first()
            stores_integration.append({
                'store': store,
                'credential': cred,
                'has_token': bool(cred and cred.tenant_token_enc),
                'is_active': bool(cred and cred.is_active),
                'token_valid': bool(cred and cred.is_token_valid),
                'token_expires_soon': bool(cred and cred.token_expires_soon),
                'health_status': (
                    cred.last_health_status if cred else 'NOT_CONFIGURED'
                ),
                'efatura_active': bool(cred and cred.efatura_active),
                'banking_active': bool(cred and cred.banking_active),
                'last_check': (
                    cred.last_health_check_at.strftime('%d.%m.%Y %H:%M')
                    if cred and cred.last_health_check_at else None
                ),
            })

    ctx = {
        "record": company,
        "available_contracts": available_contracts,
        "contract_processes": contract_processes,
        "company_proposals": company_proposals,  # Template'e gönderiyoruz
        "packages": Packages.objects.filter(is_active=True).order_by('order', 'name'),
        "available_chambers": Chambers.objects.filter(is_active=True, is_deleted=False).order_by('name'),
        "stores_integration": stores_integration,
        # Faz 12.3 Fix: Mağaza ekleme modalında ek modül seçimi için
        "all_modules": SaaSModule.objects.filter(is_active=True).order_by('order', 'name'),
    }
    return render(request, 'management/stores/detail.html', ctx)


@login_required(login_url='login')
@role_required('STORES_DETAIL_VIEW')
@require_GET
@role_required('STORES_WA_USAGE_ME')
def wa_usage_me(request):
    store = _resolve_store_for_request(request)
    if not store:
        return JsonResponse({
            "enabled": False,
            "daily": {"count": 0, "progress": 0, "limit": None, "remaining": None},
            "monthly": {"count": 0, "progress": 0, "limit": None, "remaining": None},
            "allowed_templates": []
        })
    snap = get_usage_snapshot(store)
    settings_obj, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
    snap["enabled"] = settings_obj.enabled
    snap["allowed_templates"] = settings_obj.allowed_templates or []
    return JsonResponse(snap)


TEMPLATE_FRIENDLY_NAMES = {
    "hello_world": "👋 Hoşgeldin Mesajı (Test)",
    "islem_ozeti_kp_min_v2": "📝 İşlem Özeti (Kuyum Plus)",
    "tamir_bilgi_min_v1": "🛠️ Tamir Bilgilendirme",
    "twofa_login_v1": "🔐 Giriş Doğrulama (2FA)",
    "verify_phone_v1": "📱 Telefon Numarası Doğrulama",
    "otp_generic": "🔢 Tek Kullanımlık Şifre",
    "appointment_reminder": "📅 Randevu Hatırlatma",
}


@login_required(login_url='login')
@require_GET
def wa_templates_me(request):
    store = _resolve_store_for_request(request)
    qs = WhatsAppTemplateCatalog.objects.filter(is_active=True).order_by('name')
    allowed = set()
    if store:
        settings_obj, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
        allowed = set(settings_obj.allowed_templates or [])
    items = []
    for t in qs:
        approved = False
        if isinstance(t.meta, dict):
            statuses = t.meta.get("statuses")
            if isinstance(statuses, dict):
                for v in statuses.values():
                    if (isinstance(v, str) and v == "APPROVED") or (
                            isinstance(v, dict) and v.get("status") == "APPROVED"):
                        approved = True
                        break
        if not approved:
            approved = True

        display_title = t.title
        if not display_title:
            display_title = TEMPLATE_FRIENDLY_NAMES.get(t.name, t.name)

        items.append({
            "name": t.name,
            "title": display_title,
            "category": t.category,
            "default_language": t.default_language,
            "languages": t.languages or [],
            "approved": approved,
            "allowed": (t.name in allowed),
        })
    return JsonResponse({"items": items})


@login_required(login_url='login')
@role_required('STORES_DETAIL_VIEW')
@require_POST
def wa_save_templates_me(request):
    store = _resolve_store_for_request(request)
    if not store:
        return JsonResponse({"error": True, "error_msg": "Mağaza bulunamadı veya yetki yok."})
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": True, "error_msg": "Geçersiz JSON."})
    settings_obj, _ = StoreWhatsAppSettings.objects.get_or_create(store=store)
    settings_obj.enabled = bool(body.get("enabled", True))
    allowed = [str(x) for x in (body.get("allowed") or []) if isinstance(x, str)]
    settings_obj.allowed_templates = allowed
    settings_obj.save(update_fields=["enabled", "allowed_templates", "updated_at"])
    write_log(request, "Mağazalar", f"WhatsApp ayarları güncellendi. Mağaza ID: {store.store_id}")
    return JsonResponse({"result": True})


@login_required(login_url='login')
@role_required('STORES_DETAIL_VIEW')
@require_GET
@role_required('STORES_WA_CHAT_ME')
def wa_chat_me(request):
    store = _resolve_store_for_request(request)
    if not store:
        return JsonResponse({"data": []})
    q = (request.GET.get("q") or "").strip()
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    limit = int(request.GET.get("limit", 500))
    qs = (WhatsAppChatMessage.objects
          .filter(store=store)
          .select_related("conversation", "user", "customer")
          .order_by("-timestamp"))
    if date_from:
        qs = qs.filter(timestamp__date__gte=date_from)
    if date_to:
        qs = qs.filter(timestamp__date__lte=date_to)
    if q:
        from django.db.models import Q as _Q
        q_lower = q.lower()
        qs = qs.filter(
            _Q(customer__first_name__icontains=q_lower) |
            _Q(customer__last_name__icontains=q_lower) |
            _Q(customer__mobile_phone__icontains=q_lower) |
            _Q(customer__phone__icontains=q_lower) |
            _Q(from_number__icontains=q_lower) |
            _Q(to_number__icontains=q_lower) |
            _Q(text__icontains=q_lower) |
            _Q(template_name__icontains=q_lower) |
            _Q(user__first_name__icontains=q_lower) |
            _Q(user__last_name__icontains=q_lower)
        )
    data = []
    for m in qs[:limit]:
        cust_name, cust_id, cust_phone = "", None, ""
        if getattr(m, "customer_id", None):
            fn = getattr(m.customer, "first_name", "") or ""
            ln = getattr(m.customer, "last_name", "") or ""
            cust_name = (fn + " " + ln).strip()
            cust_id = m.customer.id
            cust_phone = getattr(m.customer, "mobile_phone", "") or getattr(m.customer, "phone", "") or ""
        else:
            cust_phone = m.to_number or m.from_number
        data.append({
            "ts": m.timestamp.isoformat() if m.timestamp else "",
            "dir": m.direction,
            "from": m.from_number,
            "to": m.to_number,
            "status": m.status or "",
            "error": m.error or "",
            "template": m.template_name or "",
            "text": m.text or "",
            "user_name": (m.user and m.user.get_full_name()) or "",
            "user_id": (m.user and m.user.id) or None,
            "customer_name": cust_name,
            "customer_id": cust_id,
            "customer_phone": cust_phone,
        })
    return JsonResponse({"data": data})


@login_required(login_url='login')
@role_required('STORES_DETAIL_VIEW')
def store_detail_view(request, store_id):
    store = get_object_or_404(Stores, id=store_id, is_deleted=False)
    try:
        StoreLabelSettings.objects.get_or_create(store=store)
    except Exception as e:
        print(f"Ayar oluşturma hatası: {e}")

    User = get_user_model()
    try:
        personnel_count = User.objects.filter(store=store, is_active=True).count()
    except Exception:
        personnel_count = 0

    # ── Faz 12.4: Store-scoped rol listesi ──
    # Global STORE roller (store=NULL) + bu mağazaya ait izole roller
    roles = []
    try:
        from apps.roles.models import Roles
        roles = Roles.objects.filter(
            is_active=True, is_deleted=False, category='STORE'
        ).filter(
            Q(store__isnull=True) | Q(store=store)
        ).order_by('name')
    except Exception:
        pass

    term = (PavoTerminal.objects
            .filter(store=store, is_active=True)
            .order_by("-updated_at")
            .first())

    # ── Faz 12.3: Modül & Yetki bilgileri ──
    # Paketten gelen modüller
    from apps.crm.packages.models import PackageModule
    package_module_ids = set()
    if store.package_id:
        package_module_ids = set(
            PackageModule.objects.filter(package_id=store.package_id)
            .values_list('module_id', flat=True)
        )

    # Çekirdek modüller
    core_module_ids = set(
        SaaSModule.objects.filter(is_core=True, is_active=True)
        .values_list('id', flat=True)
    )

    # Ek modüller (StoreModule tablosundan)
    extra_module_ids = set(
        StoreModule.objects.filter(store=store)
        .values_list('module_id', flat=True)
    )

    # Tüm aktif modüller (UI'da gösterim için)
    all_modules = SaaSModule.objects.filter(is_active=True).order_by('order', 'name')

    # Efektif yetki sayısı
    effective_perm_count = len(get_store_effective_permission_ids(store))

    # ── Faz 12.4: İzole rol sayısı ──
    try:
        from apps.roles.models import Roles as RolesModel
        store_isolated_role_count = RolesModel.objects.filter(
            store=store, is_active=True, is_deleted=False
        ).count()
    except Exception:
        store_isolated_role_count = 0

    # ── Açık Bankacılık Fatura Ayarları (2026-04) ──
    # StoreEInvoiceSettings kaydı yoksa varsayılan değerlerle oluştur
    # (Mağaza detay sayfasındaki "Açık Bankacılık Fatura Ayarları" kartı için)
    try:
        from apps.invoices.models import StoreEInvoiceSettings
        einvoice_settings, _ = StoreEInvoiceSettings.objects.get_or_create(store=store)
        karat_choices = StoreEInvoiceSettings.Karat.choices
        labor_type_choices = StoreEInvoiceSettings.LaborType.choices
    except Exception:
        einvoice_settings = None
        karat_choices = []
        labor_type_choices = []

    ctx = {
        "record": store,
        "personnel_count": personnel_count,
        "roles": roles,
        "term": term,
        # Faz 12.3: Modül bilgileri
        "all_modules": all_modules,
        "package_module_ids": package_module_ids | core_module_ids,
        "core_module_ids": core_module_ids,
        "extra_module_ids": extra_module_ids,
        "effective_perm_count": effective_perm_count,
        # Açık Bankacılık Fatura Ayarları
        "einvoice_settings": einvoice_settings,
        "karat_choices": karat_choices,
        "labor_type_choices": labor_type_choices,
        # Faz 12.4: Rol izolasyon bilgileri
        "store_isolated_role_count": store_isolated_role_count,
        # Paket/Modül güncelleme için
        "packages": Packages.objects.filter(is_active=True).order_by('order', 'name'),
    }
    ctx["credit_packages"] = [
        {"amount": 50, "price": 50},
        {"amount": 100, "price": 100},
        {"amount": 250, "price": 250},
        {"amount": 500, "price": 450},
        {"amount": 1000, "price": 900},
        {"amount": 2000, "price": 1800},
    ]

    return render(request, 'management/stores/store_detail.html', ctx)


def _user_store(request: HttpRequest) -> Stores:
    st = getattr(request.user, "store", None)
    if st:
        return st
    sid = getattr(request.user, "store_id", None) or request.session.get("active_store_id")
    if sid:
        try:
            return Stores.objects.get(id=sid)
        except Stores.DoesNotExist:
            pass
    try:
        return Stores.objects.get()
    except Stores.DoesNotExist:
        raise ValueError("Kullanıcıya bağlı mağaza bulunamadı.")


@login_required(login_url="login")
@require_http_methods(["GET", "POST"])
@role_required('STORES_STORE_PAVO_SETTINGS_VIEW')
def store_pavo_settings_view(request: HttpRequest) -> HttpResponse:
    store = _user_store(request)
    term = (PavoTerminal.objects
            .filter(store=store, is_active=True)
            .order_by("-updated_at")
            .first())

    if request.method == "POST":
        title = (request.POST.get("title") or "Terminal").strip()
        ip = (request.POST.get("ip") or "").strip()
        secure = bool(request.POST.get("secure"))
        port_raw = (request.POST.get("port") or "").strip()
        port = int(port_raw) if port_raw else None
        serial_number = (request.POST.get("serial_number") or "").strip()
        fingerprint = (request.POST.get("fingerprint") or "").strip()

        if not ip or not serial_number or not fingerprint:
            messages.error(request, "IP, Seri No ve Fingerprint zorunlu alanlardır.")
        else:
            if term is None:
                term = PavoTerminal(store=store)
            term.title = title or term.title
            term.ip = ip
            term.secure = secure
            term.port = port
            term.serial_number = serial_number
            term.fingerprint = fingerprint
            term.is_active = True
            term.save()
            messages.success(request, "Pavo terminal ayarları kaydedildi.")
            return redirect("stores:store-detail", store_id=store.id)

    return redirect("stores:store-detail", store_id=store.id)


# apps/stores/views.py

def update_labor_setting(request):
    if request.method == 'POST':
        store_id = request.POST.get('store_id')
        use_avg = request.POST.get('use_average_labor') == 'true'

        try:
            store = Stores.objects.get(id=store_id)
            store.use_average_labor = use_avg
            store.save()
            return JsonResponse({'success': True})
        except Stores.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Mağaza bulunamadı'})
    return JsonResponse({'success': False})


@login_required
@require_POST
@role_required('STORES_ADD_STORE')
def update_store_package_modules(request, store_id):
    """
    Mağazanın paket ve modül bilgilerini günceller.

    Yalnızca superuser / is_staff kullanabilir.

    POST parametreleri:
        package_id:          Yeni paket UUID (opsiyonel — boş bırakılabilir)
        extra_module_ids[]:  Modül UUID listesi (paketsiz ise ana yetki kaynağı)

    İş mantığı:
        1. Paket varsa günceller, yoksa NULL yapar.
        2. sync_store_modules() ile modülleri senkronize eder.
        3. Paketsiz mağazalarda seçilen modüller tek yetki kaynağıdır.

    Returns:
        JsonResponse: {'result': True} veya {'error': True, 'error_msg': str}
    """
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    store = get_object_or_404(Stores, id=store_id, is_deleted=False)

    package_id = (request.POST.get('package_id') or '').strip()
    package = None
    package_name = "Paket Yok"
    if package_id:
        try:
            package = Packages.objects.get(id=package_id, is_active=True)
            package_name = package.name
        except Packages.DoesNotExist:
            return JsonResponse({'error': True, 'error_msg': 'Seçilen paket bulunamadı veya aktif değil.'})

    extra_module_ids = request.POST.getlist('extra_module_ids[]')

    try:
        # 1. Paket güncelle (None olabilir)
        store.package = package
        store.save(update_fields=['package'])

        # 2. Modülleri senkronize et
        sync_result = sync_store_modules(store, extra_module_ids)

        write_log(
            request, "Mağazalar",
            f"PAKET/MODÜL GÜNCELLENDİ. Mağaza: {store.title} | "
            f"Paket: {package_name} | "
            f"Modül eklenen: {sync_result['added']}, silinen: {sync_result['removed']}"
        )

        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': str(e)})


# apps/stores/views.py

@login_required
@require_POST
def update_store_setting(request):
    """
    Mağazaya ait boolean ayarları günceller.
    Beklenen POST verisi: 'store_id', 'key', 'value' (true/false)
    """
    store_id = request.POST.get('store_id')
    key = request.POST.get('key')
    value = request.POST.get('value') == 'true'

    # Güvenlik: Sadece belirli alanların değiştirilmesine izin ver
    ALLOWED_SETTINGS = ['use_average_labor', 'apply_masak_rules']

    if key not in ALLOWED_SETTINGS:
        return JsonResponse({'success': False, 'error': 'Geçersiz ayar anahtarı.'})

    try:
        # Yetki kontrolü (Personel kendi mağazasını veya Admin herhangi bir mağazayı)
        if request.user.is_superuser:
            store = Stores.objects.get(id=store_id)
        else:
            store = getattr(request.user, 'store', None)
            if not store or str(store.id) != str(store_id):
                return JsonResponse({'success': False, 'error': 'Yetkisiz işlem.'})

        # Dinamik olarak alanı güncelle
        setattr(store, key, value)
        store.save(update_fields=[key])

        # Loglama
        write_log(request, "Mağazalar", f"Ayar Güncellendi: {key} -> {value} (Mağaza ID: {store.id})")

        return JsonResponse({'success': True})

    except Stores.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Mağaza bulunamadı'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


# ──────────────────────────────────────────────────────
# e-SÜREÇ ENTEGRASYON YÖNETİMİ — Automated Provisioning
# ──────────────────────────────────────────────────────

@login_required(login_url='login')
@require_POST
def esurec_provision_store(request):
    """
    Belirtilen mağaza için e-Süreç'ten otomatik token üretir ve kaydeder.
    Yalnızca superuser kullanabilir.

    POST body (JSON): { "store_id": "uuid" }
    """
    if not request.user.is_superuser:
        return JsonResponse({'result': False, 'msg': 'Bu işlem için yetkiniz yok.'}, status=403)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = {}

    store_id = body.get('store_id') or request.POST.get('store_id', '')
    if not store_id:
        return JsonResponse({'result': False, 'msg': 'store_id parametresi zorunludur.'})

    try:
        store = Stores.objects.get(id=store_id, is_deleted=False)
    except Stores.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Mağaza bulunamadı.'})

    from apps.banking.services import EsurecProvisioningService

    try:
        service = EsurecProvisioningService()
        result = service.provision(store)
        return JsonResponse(result)
    except Exception as e:
        log.exception("esurec_provision_store beklenmeyen hata: store=%s hata=%s", store_id, e)
        return JsonResponse({
            'result': False,
            'msg': 'Aktivasyon işlemi sırasında beklenmeyen bir hata oluştu.',
        })


@login_required(login_url='login')
@require_POST
def esurec_test_connection(request):
    """
    Belirtilen mağazanın e-Süreç bağlantısını test eder (health check).
    Yalnızca superuser kullanabilir.

    POST body (JSON): { "store_id": "uuid" }
    """
    if not request.user.is_superuser:
        return JsonResponse({'result': False, 'msg': 'Bu işlem için yetkiniz yok.'}, status=403)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = {}

    store_id = body.get('store_id') or request.POST.get('store_id', '')
    if not store_id:
        return JsonResponse({'result': False, 'msg': 'store_id parametresi zorunludur.'})

    try:
        store = Stores.objects.get(id=store_id, is_deleted=False)
    except Stores.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Mağaza bulunamadı.'})

    from apps.banking.services import EsurecHealthCheckService

    service = EsurecHealthCheckService(store)
    result = service.check(force=True)
    return JsonResponse(result)


@login_required(login_url='login')
@require_POST
def esurec_deactivate_store(request):
    """
    Belirtilen mağazanın e-Süreç entegrasyonunu devre dışı bırakır.
    Yalnızca superuser kullanabilir.

    POST body (JSON): { "store_id": "uuid" }
    """
    if not request.user.is_superuser:
        return JsonResponse({'result': False, 'msg': 'Bu işlem için yetkiniz yok.'}, status=403)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        body = {}

    store_id = body.get('store_id') or request.POST.get('store_id', '')
    if not store_id:
        return JsonResponse({'result': False, 'msg': 'store_id parametresi zorunludur.'})

    from apps.banking.models import EsurecTenantCredential

    updated = EsurecTenantCredential.objects.filter(
        store_id=store_id, is_active=True,
    ).update(is_active=False)

    if updated:
        return JsonResponse({'result': True, 'msg': 'e-Süreç entegrasyonu devre dışı bırakıldı.'})
    return JsonResponse({'result': False, 'msg': 'Aktif entegrasyon bulunamadı.'})


# ─────────────────────────────────────────────────────────────────────────────
#  FAZ 4-5: Mağaza Bazlı Rol Yönetimi (Store Role Management)
# ─────────────────────────────────────────────────────────────────────────────
#  Mağaza yöneticilerinin KENDİ personelleri için özel roller oluşturabilmesi,
#  düzenleyebilmesi ve silebilmesi. Yetki havuzu KESİNLİKLE mağazanın
#  "Efektif Yetki Havuzu" ile sınırlandırılır.
#
#  Güvenlik kuralları:
#    - request.user.store zorunlu (store yoksa erişim yok)
#    - Yalnızca store=request.user.store olan roller görünür
#    - Permission listesi: get_store_effective_permission_ids() ∩ is_system_only=False
#    - POST validasyonu: gelen permission_ids efektif havuz ile kesiştirilir
#    - role.store ve role.category otomatik atanır (kullanıcı değiştiremez)
# ─────────────────────────────────────────────────────────────────────────────

import uuid as _uuid_mod
from django.db import transaction as _db_transaction
from apps.roles.models import Roles, Permission, RoleDetail
from apps.accounts.models import Users
from apps.roles.management.commands._permission_utils import (
    PERM_GROUP_OVERRIDES,
    APP_GROUP_OVERRIDES,
    ABC_PATTERN,
)


def _get_store_or_forbid(request):
    """
    request.user.store döndürür; yoksa HttpResponseForbidden.
    Tüm store role view'ları bu fonksiyonu kullanır.
    """
    user = request.user
    store = getattr(user, 'store', None)
    if not store:
        return None
    return store


def _get_allowed_permissions(store):
    """
    Mağazanın efektif yetki havuzundaki, is_system_only=False olan
    Permission queryset'ini döndürür.

    Bu liste:
      - Create/Update formlarında gösterilecek yetki seçeneklerini belirler
      - POST validasyonunda güvenlik sınırı olarak kullanılır
    """
    effective_perm_ids = get_store_effective_permission_ids(store)
    if not effective_perm_ids:
        return Permission.objects.none()
    return Permission.objects.filter(
        id__in=effective_perm_ids,
        is_system_only=False,
    ).order_by('group', 'order', 'code')


# ─────────────────────────────────────────────────────────────────────────────
#  RESTRICTION MODEL — Grup Bazlı Yetki Yönetimi Sabitleri
# ─────────────────────────────────────────────────────────────────────────────
# Kuyumcu bireysel yetki kodlarını görmez; yalnızca özellik gruplarını
# (modülleri) personeline açar veya kapatır.

GROUP_LABELS = {
    'dashboard': 'Kontrol Paneli',
    'products': 'Ürün Yönetimi',
    'gold_purchases': 'Barkodlama',
    'scraps': 'Hurda İşlemleri',
    'repairs': 'Tamir İşlemleri',
    'counts': 'Sayım İşlemleri',
    'suppliers': 'Tedarikçi Yönetimi',
    'custody': 'Emanet İşlemleri',
    'banking': 'Banka İşlemleri',
    'workshops': 'Atölye Yönetimi',
    'customers': 'Müşteri Yönetimi',
    'orders': 'Sipariş Yönetimi',
    'cash_management': 'Kasa Yönetimi',
    'bracelets': 'Bilezik İşlemleri',
    'transactions_board_fast': 'Hızlı İşlem',
    'transactions_board_retail': 'Perakende İşlem',
    'transactions_board_wholesale': 'Toptan İşlem',
    'transactions_board_process': 'İşlemler',
    'invoices': 'Fatura Yönetimi',
    'settings': 'Mağaza Ayarları',
    'requests': 'Talepler',
}

GROUP_ICONS = {
    'dashboard': 'ki-chart-simple',
    'products': 'ki-shop',
    'gold_purchases': 'ki-wallet',
    'scraps': 'ki-abstract-26',
    'repairs': 'ki-wrench',
    'counts': 'ki-calculator',
    'suppliers': 'ki-truck',
    'custody': 'ki-safe',
    'banking': 'ki-bank',
    'workshops': 'ki-gear',
    'customers': 'ki-people',
    'cash_management': 'ki-vault',
    'orders': 'ki-clipboard',
    'bracelets': 'ki-diamond',
    'transactions_board_fast': 'ki-element-11',
    'transactions_board_retail': 'ki-element-11',
    'transactions_board_wholesale': 'ki-element-11',
    'transactions_board_process': 'ki-element-11',
    'invoices': 'ki-file-sheet',
    'settings': 'ki-setting-2',
    'requests': 'ki-message-text',
}

# ─────────────────────────────────────────────────────────────────────────────
#  MENÜ GÖRÜNÜRLÜk KODLARI (ABC-pattern) → İşlevsel Grup Eşleştirmesi
# ─────────────────────────────────────────────────────────────────────────────
# base.html'de sol menü render kontrolü için kullanılan ABC kodları,
# Permission.group alanı üzerinden işlevsel gruplara dahil olmadığı için
# _resolve_groups_to_perm_ids tarafından atlanıyordu.
# Bu sözlük, her grup seçildiğinde ilgili menü görünürlük kodlarının da
# role eklenmesini sağlar.
GROUP_MENU_CODES = {
    'transactions_board_fast': ['ABC1001D'],
    'transactions_board_retail': [ 'ABC1310D'],
    'transactions_board_wholesale': [ 'ABC1030D'],
    'transactions_board_process': ['ABC1062D'],
    'products': ['ABC1003D'],
    'gold_purchases': ['ABC1007D'],
    'scraps': ['ABC1008D'],
    'bracelets': ['ABC1908D'],
    'repairs': ['ABC1006D'],
    'counts': ['ABC1009D'],
    'dashboard': ['ABC1002D'],
    'invoices': ['ABC1706D', 'ABC1010D'],
    'banking': ['ABC1706D'],
    'cash_management': ['ABC1017D'],
    'suppliers': ['ABC1011D'],
    'custody': ['ABC1544D'],
    'workshops': ['ABC1012D'],
    'customers': ['ABC1015D'],
    'settings': ['ABC1016D'],
    'orders': ['ABC9008D'],
    'requests': ['ABCD005D'],
}


def _resolve_perm_group(perm):
    """
    Bir Permission kaydının DB'deki group değerini, override sözlüklerini
    uygulayarak doğru GROUP_LABELS slug'ına çevirir.

    Öncelik:
      1. PERM_GROUP_OVERRIDES (permission.code bazlı)
      2. APP_GROUP_OVERRIDES  (permission.group bazlı)
      3. DB'deki orijinal group değeri

    Örnek:
      - code='TRANSACTIONS_BOARD_FAST_INDEX_VIEW', group='transactions_board'
        → 'transactions_board_fast'  (PERM override)
      - code='BANK_MANAGEMENT_...', group='bank_management'
        → 'cash_management'  (APP override)
    """
    code = perm.code or ''
    db_group = perm.group or '__none__'

    if code in PERM_GROUP_OVERRIDES:
        return PERM_GROUP_OVERRIDES[code]

    if db_group in APP_GROUP_OVERRIDES:
        return APP_GROUP_OVERRIDES[db_group]

    return db_group


def _get_feature_groups(store, checked_perm_ids=None):
    """
    FAZ 4-5 Ek 8 — Veritabanı group alanından TAMAMEN bağımsız.

    Switch UI'da gösterilecek özellik kartlarını oluşturur.

    ÖNEMLİ: Bu fonksiyon veritabanındaki Permission.group alanını
    ASLA okumaz ve o alana GÜVENMez. Kart listesi tamamen Python
    sözlüklerinden (GROUP_MENU_CODES, GROUP_LABELS, GROUP_ICONS)
    türetilir. Veritabanına yalnızca ABC kodlarının Permission ID'lerini
    almak için TEK bir sorgu yapılır.

    Mantık:
      1. GROUP_MENU_CODES sözlüğünün anahtarları (slug'lar) üzerinde döngü
      2. Kart başlığı → GROUP_LABELS[slug]
      3. Kart ikonu  → GROUP_ICONS[slug]
      4. Switch durumu (is_checked) → Grubun ABC kodlarına ait Permission
         ID'lerinden herhangi biri checked_perm_ids içinde mi?
      5. perm_count → Grubun ABC kod sayısı

    Args:
        store: Store instance (imza uyumluluğu için korundu, kullanılmıyor)
        checked_perm_ids: Mevcut role atanmış Permission ID'leri seti
                          (RoleDetail.permission_id değerleri — UUID)

    Returns:
        list[dict]: Her dict şu anahtarları içerir:
            - slug (str): Grup slug'ı (örn. 'banking')
            - label (str): Kart başlığı (örn. 'Banka İşlemleri')
            - icon (str): KI ikon sınıfı (örn. 'ki-bank')
            - perm_count (int): Grubun ABC menü kodu sayısı
            - is_checked (bool): Bu grubun switch'i açık mı?
    """
    if checked_perm_ids is None:
        checked_perm_ids = set()

    # ── 1. Tüm ABC kodlarını topla (GROUP_MENU_CODES'daki tüm değerler) ──
    all_abc_codes = set()
    for codes in GROUP_MENU_CODES.values():
        all_abc_codes.update(codes)

    # ── 2. TEK DB sorgusu: ABC kodu → Permission ID eşlemesi ──
    # Veritabanından SADECE code ve id çekilir.
    # Permission.group alanı OKUNMAZ.
    abc_code_to_id = {}
    if all_abc_codes:
        abc_code_to_id = dict(
            Permission.objects.filter(code__in=all_abc_codes)
            .values_list('code', 'id')
        )

    # ── 3. Her grup slug'ı için kart oluştur (sıralı) ──
    # Kaynak: YALNIZCA GROUP_MENU_CODES, GROUP_LABELS, GROUP_ICONS
    # Veritabanındaki perm.group alanı hiçbir şekilde kullanılmaz.
    feature_groups = []

    for slug in sorted(GROUP_MENU_CODES.keys()):
        # GROUP_LABELS'da tanımlı olmayan slug → kart oluşturma (güvenlik)
        label = GROUP_LABELS.get(slug)
        if not label:
            continue

        # Bu grubun ABC menü kodları (Python sözlüğünden, DB'den değil)
        group_abc_codes = GROUP_MENU_CODES[slug]

        # ABC kodlarına karşılık gelen Permission ID'leri
        group_perm_ids = set()
        for abc_code in group_abc_codes:
            perm_id = abc_code_to_id.get(abc_code)
            if perm_id is not None:
                group_perm_ids.add(perm_id)

        # Switch durumu: Grubun ABC kodlarından herhangi birinin
        # Permission ID'si checked_perm_ids içindeyse → açık
        is_checked = bool(group_perm_ids & checked_perm_ids)

        feature_groups.append({
            'slug': slug,
            'label': label,
            'icon': GROUP_ICONS.get(slug, 'ki-abstract-26'),
            'perm_count': len(group_abc_codes),
            'is_checked': is_checked,
        })

    return feature_groups


def _resolve_groups_to_perm_ids(store, selected_groups):
    """
    FAZ 4-5 Ek 6 — Sadeleştirilmiş.

    Seçilen grup slug'larını SADECE ABC menü kodu Permission ID'lerine
    dönüştürür. Artık işlevsel yetkiler (Tip A) role kaydedilmez;
    bunlar mağazanın efektif havuzundan runtime'da kontrol edilir.

    Args:
        store: Store instance (artık kullanılmıyor ama imza korundu)
        selected_groups: list[str] — seçilen grup slug'ları

    Returns:
        set[UUID]: Seçilen gruplara ait ABC Permission ID'leri
    """
    if not selected_groups:
        return set()

    selected_set = set(selected_groups)

    # Seçili gruplara ait ABC menü kodlarını topla
    abc_codes_needed = set()
    for group_slug in selected_set:
        abc_codes_needed.update(GROUP_MENU_CODES.get(group_slug, []))

    if not abc_codes_needed:
        return set()

    # ABC kodlarının Permission ID'lerini getir
    return set(
        Permission.objects.filter(code__in=abc_codes_needed)
        .values_list('id', flat=True)
    )


@login_required
@require_http_methods(['GET'])
def store_roles_list(request):
    """
    FAZ 4-5 — Mağaza rol listeleme.

    Yalnızca request.user.store'a ait izole roller gösterilir.
    Global roller (store=NULL) bu listede YER ALMAZ.
    """
    store = _get_store_or_forbid(request)
    if store is None:
        return HttpResponseForbidden("Mağaza bilgisi bulunamadı.")

    roles = Roles.objects.filter(
        store=store,
        category='STORE',
        is_deleted=False,
    ).order_by('-is_active', 'name')

    write_log(request, 'Mağaza Rolleri', 'Görüntülendi.')

    return render(request, 'management/stores/store_roles_list.html', {
        'roles': roles,
        'store': store,
        'title': 'Mağaza Rolleri',
    })


@login_required
@require_http_methods(['GET', 'POST'])
def store_role_create(request):
    """
    FAZ 4-5 — Yeni mağaza rolü oluşturma (Restriction Model).

    Kuyumcu bireysel yetki kodlarını görmez. Yalnızca özellik
    gruplarını (modülleri) açıp kapatır. Seçilen gruptaki TÜM
    yetkiler otomatik olarak role atanır.

    GET: Boş form + efektif havuzdaki özellik gruplarını gösterir.
    POST: Yeni Roles + seçili gruplara ait tüm RoleDetail kayıtlarını oluşturur.

    Güvenlik:
      - role.store = request.user.store (otomatik)
      - role.category = 'STORE' (otomatik)
      - Seçilen gruplar efektif havuz ile çapraz doğrulanır
    """
    store = _get_store_or_forbid(request)
    if store is None:
        return HttpResponseForbidden("Mağaza bilgisi bulunamadı.")

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        description = (request.POST.get('description') or '').strip()

        if not name:
            messages.error(request, 'Rol adı zorunludur.')
            return render(request, 'management/stores/store_role_form.html', {
                'feature_groups': _get_feature_groups(store),
                'store': store,
                'title': 'Yeni Mağaza Rolü',
                'form_action': 'create',
            })

        # POST'tan gelen grup slug'ları
        selected_groups = (
            request.POST.getlist('group_names[]')
            or request.POST.getlist('group_names')
        )

        # GÜVENLİK: Seçilen grupları efektif havuzdaki yetkilere dönüştür
        valid_ids = _resolve_groups_to_perm_ids(store, selected_groups)

        try:
            with _db_transaction.atomic():
                role = Roles.objects.create(
                    name=name,
                    description=description,
                    category='STORE',
                    store=store,
                    is_active=True,
                )

                if valid_ids:
                    RoleDetail.objects.bulk_create(
                        [RoleDetail(role=role, permission_id=pid, status=True)
                         for pid in valid_ids],
                        ignore_conflicts=True,
                    )

            write_log(request, 'Mağaza Rolleri', f'Yeni rol oluşturuldu: {name}')
            messages.success(request, f'"{name}" rolü başarıyla oluşturuldu.')
            return redirect('stores:store-roles-list')

        except Exception as e:
            messages.error(request, f'Rol oluşturulurken hata: {e}')

    return render(request, 'management/stores/store_role_form.html', {
        'feature_groups': _get_feature_groups(store),
        'store': store,
        'title': 'Yeni Mağaza Rolü',
        'form_action': 'create',
    })


@login_required
@require_http_methods(['GET', 'POST'])
def store_role_update(request, role_id):
    """
    FAZ 4-5 — Mağaza rolü düzenleme (Restriction Model).

    Kuyumcu bireysel yetki kodlarını görmez. Yalnızca özellik
    gruplarını açıp kapatır. Seçilen gruptaki TÜM yetkiler
    otomatik olarak role atanır; seçilmeyen grupların yetkileri kaldırılır.

    GET: Mevcut rol bilgileri ve özellik grubu durumlarını gösterir.
    POST: Rol bilgilerini günceller ve RoleDetail kayıtlarını yeniden oluşturur.

    Güvenlik:
      - Rol request.user.store'a ait olmalı (yoksa 404)
      - Seçilen gruplar efektif havuz ile çapraz doğrulanır
      - role.store ve role.category değiştirilemez
    """
    store = _get_store_or_forbid(request)
    if store is None:
        return HttpResponseForbidden("Mağaza bilgisi bulunamadı.")

    from django.http import Http404
    try:
        role = Roles.objects.get(
            id=role_id,
            store=store,
            category='STORE',
            is_deleted=False,
        )
    except Roles.DoesNotExist:
        raise Http404("Bu rol mağazanıza ait değil veya bulunamadı.")

    # Mevcut seçili yetki id'leri (grup durumunu belirlemek için)
    checked_perm_ids = set(
        RoleDetail.objects.filter(role=role, status=True)
        .values_list('permission_id', flat=True)
    )

    # Bu role atanmış kullanıcılar (sadece bu mağazadakiler)
    role_users = Users.objects.filter(
        role=role,
        store=store,
        is_active=True,
    ).order_by('first_name')

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        description = (request.POST.get('description') or '').strip()

        if not name:
            messages.error(request, 'Rol adı zorunludur.')
            return render(request, 'management/stores/store_role_form.html', {
                'record': role,
                'feature_groups': _get_feature_groups(store, checked_perm_ids),
                'role_users': role_users,
                'store': store,
                'title': f'Rol Düzenle — {role.name}',
                'form_action': 'update',
            })

        # POST'tan gelen grup slug'ları
        selected_groups = (
            request.POST.getlist('group_names[]')
            or request.POST.getlist('group_names')
        )

        # GÜVENLİK: Seçilen grupları efektif havuzdaki yetkilere dönüştür
        valid_ids = _resolve_groups_to_perm_ids(store, selected_groups)

        try:
            with _db_transaction.atomic():
                role.name = name
                role.description = description
                role.save(update_fields=['name', 'description'])

                # Mevcut RoleDetail kayıtlarını sil ve yeniden oluştur
                RoleDetail.objects.filter(role=role).delete()
                if valid_ids:
                    RoleDetail.objects.bulk_create(
                        [RoleDetail(role=role, permission_id=pid, status=True)
                         for pid in valid_ids],
                        ignore_conflicts=True,
                    )

            write_log(request, 'Mağaza Rolleri', f'Rol güncellendi: {name}')
            messages.success(request, f'"{name}" rolü başarıyla güncellendi.')
            return redirect('stores:store-roles-list')

        except Exception as e:
            messages.error(request, f'Rol güncellenirken hata: {e}')
            # Hata durumunda mevcut seçimleri koru
            checked_perm_ids = valid_ids

    return render(request, 'management/stores/store_role_form.html', {
        'record': role,
        'feature_groups': _get_feature_groups(store, checked_perm_ids),
        'role_users': role_users,
        'store': store,
        'title': f'Rol Düzenle — {role.name}',
        'form_action': 'update',
    })


@login_required
@require_http_methods(['POST'])
def store_role_delete(request, role_id):
    """
    FAZ 4-5 — Mağaza rolü silme (soft delete).

    Güvenlik:
      - Rol request.user.store'a ait olmalı
      - Soft delete (is_deleted=True)
      - Rolü kullanan aktif kullanıcılar varsa uyarı

    AJAX ve form POST destekler.
    """
    store = _get_store_or_forbid(request)
    if store is None:
        return HttpResponseForbidden("Mağaza bilgisi bulunamadı.")

    from django.http import Http404
    try:
        role = Roles.objects.get(
            id=role_id,
            store=store,
            category='STORE',
            is_deleted=False,
        )
    except Roles.DoesNotExist:
        raise Http404("Bu rol mağazanıza ait değil veya bulunamadı.")

    # Bu role atanmış aktif kullanıcı sayısı
    active_user_count = Users.objects.filter(
        role=role,
        store=store,
        is_active=True,
    ).count()

    role_name = role.name

    # Soft delete
    role.is_deleted = True
    role.is_active = False
    role.save(update_fields=['is_deleted', 'is_active'])

    write_log(request, 'Mağaza Rolleri', f'Rol silindi: {role_name}')

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    if active_user_count > 0:
        warn_msg = (
            f'"{role_name}" rolü silindi. '
            f'Ancak bu role atanmış {active_user_count} aktif kullanıcı var. '
            f'Lütfen bu kullanıcılara yeni bir rol atayın.'
        )
        if is_ajax:
            return JsonResponse({'result': True, 'warning': warn_msg})
        messages.warning(request, warn_msg)
    else:
        success_msg = f'"{role_name}" rolü başarıyla silindi.'
        if is_ajax:
            return JsonResponse({'result': True, 'message': success_msg})
        messages.success(request, success_msg)

    return redirect('stores:store-roles-list')


@login_required
@require_http_methods(['POST'])
def store_role_toggle_status(request, role_id):
    """
    FAZ 4-5 — Mağaza rolü aktif/pasif durumu değiştirme.

    AJAX endpoint. Rol durumunu tersine çevirir.
    """
    store = _get_store_or_forbid(request)
    if store is None:
        return JsonResponse({'result': False, 'msg': 'Mağaza bilgisi bulunamadı.'}, status=403)

    try:
        role = Roles.objects.get(
            id=role_id,
            store=store,
            category='STORE',
            is_deleted=False,
        )
    except Roles.DoesNotExist:
        return JsonResponse({'result': False, 'msg': 'Rol bulunamadı.'}, status=404)

    role.is_active = not role.is_active
    role.save(update_fields=['is_active'])

    status_text = 'aktif' if role.is_active else 'pasif'
    write_log(request, 'Mağaza Rolleri', f'Rol durumu değiştirildi: {role.name} → {status_text}')

    return JsonResponse({
        'result': True,
        'is_active': role.is_active,
        'message': f'"{role.name}" rolü {status_text} yapıldı.',
    })


# ─────────────────────────────────────────────────────────────────────────────
#  FAZ 4-5 Ek 3 — Personel Rol Atama
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(['POST'])
def store_personnel_change_role(request):
    """
    Mağaza yöneticisinin bir personelin rolünü değiştirmesini sağlar.

    POST parametreleri:
        user_id  — Hedef personelin UUID'si
        role_id  — Atanacak rolün UUID'si (boş string = rol kaldır)

    Güvenlik:
      - Hedef personel request.user.store ile aynı mağazada olmalı
      - Atanacak rol ya bu mağazanın izole rolü ya da global STORE rolü olmalı
    """
    store = _get_store_or_forbid(request)
    if store is None:
        return JsonResponse({'result': False, 'error_msg': 'Mağaza bilgisi bulunamadı.'}, status=403)

    user_id = (request.POST.get('user_id') or '').strip()
    role_id = (request.POST.get('role_id') or '').strip()

    if not user_id:
        return JsonResponse({'result': False, 'error_msg': 'Personel bilgisi eksik.'})

    # Hedef personeli bul — aynı mağazaya ait olmalı
    try:
        target_user = Users.objects.get(id=user_id, store=store)
    except Users.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Personel bu mağazada bulunamadı.'})

    # Rol atama veya kaldırma
    if not role_id:
        # Rol kaldır
        old_role_name = target_user.role.name if target_user.role else '-'
        target_user.role = None
        target_user.save(update_fields=['role'])
        write_log(request, 'Personel Rol Atama', f'{target_user.username} rolü kaldırıldı (eski: {old_role_name})')
        return JsonResponse({'result': True, 'message': f'{target_user.username} kullanıcısının rolü kaldırıldı.'})

    # Atanacak rolü bul — global STORE veya bu mağazanın izole rolü
    try:
        new_role = Roles.objects.get(
            id=role_id,
            category='STORE',
            is_active=True,
            is_deleted=False,
        )
    except Roles.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Geçerli bir rol bulunamadı.'})

    # Güvenlik: Rol bu mağazaya ait mi veya global mi?
    if new_role.store is not None and new_role.store_id != store.id:
        return JsonResponse({'result': False, 'error_msg': 'Bu rol mağazanıza ait değil.'})

    old_role_name = target_user.role.name if target_user.role else '-'
    target_user.role = new_role
    target_user.save(update_fields=['role'])

    write_log(
        request, 'Personel Rol Atama',
        f'{target_user.username}: {old_role_name} → {new_role.name}'
    )

    return JsonResponse({
        'result': True,
        'message': f'{target_user.username} kullanıcısına "{new_role.name}" rolü atandı.',
    })
