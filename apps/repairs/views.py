# apps/repairs/views.py
import re
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from apps.settings.send_mail import EmailService

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from django.views.decorators.http import require_http_methods

from apps.activity_logs.views import write_log
from apps.customers.models import Customers
from apps.helpers.image_resize import process_image
from apps.repairs.models import Repairs, Workshops
from apps.roles.decorators import role_required
from apps.whatsapp.services import send_whatsapp_template_guarded, wa_preflight


# -------------------- TZ güvenli tarih biçimleyici --------------------
def fmt_local(dt, fmt="%d.%m.%Y %H:%M"):
    if not dt:
        return ""
    try:
        tz = timezone.get_default_timezone()
        if getattr(settings, "USE_TZ", False):
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, tz)
            dt = timezone.localtime(dt, tz)
            return dt.strftime(fmt)

        if getattr(dt, "tzinfo", None):
            dt = timezone.localtime(dt, tz)
        return dt.strftime(fmt)
    except Exception:
        try:
            return dt.strftime(fmt)
        except Exception:
            return ""


# -------------------- Yardımcılar --------------------
def generate_tracking_code(source_text: str) -> str:
    name = (source_text or "").strip() or "X"
    first_char = name[0].upper()
    last_char = name[-1].upper()
    prefix = f"{first_char}{last_char}"

    existing_codes = Repairs.objects.filter(
        tracking_code__startswith=prefix
    ).values_list("tracking_code", flat=True)

    max_number = 0
    for code in existing_codes:
        m = re.search(r"\d+$", code or "")
        if m:
            n = int(m.group())
            if n > max_number:
                max_number = n

    return f"{prefix}{str(max_number + 1).zfill(4)}"


def parse_decimal(value, default="0.00"):
    if value in (None, ""):
        return Decimal(default)
    try:
        value = str(value).replace(",", ".")
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def ensure_public_token(repair: Repairs) -> str:
    token = getattr(repair, "public_token", None)
    if not token:
        token = uuid4().hex
        Repairs.objects.filter(pk=repair.pk).update(public_token=token)
        repair.public_token = token
    return token


def build_public_repair_url(request, repair: Repairs) -> str:
    token = ensure_public_token(repair)
    return request.build_absolute_uri(reverse("repairs:public-detail", args=[token]))


# -------------------- Bildirim Yardımcıları (GÜNCELLENDİ) --------------------

def send_repair_created_whatsapp(request, repair: Repairs):
    """
    Müşteriye WhatsApp bildirimi gönderir.
    Sadece telefon numarası var VE doğrulanmışsa (is_phone_verified=True) gönderir.
    """
    try:
        cust = repair.customer

        # 1. Telefon var mı?
        if not getattr(cust, "phone", None):
            return False

        # 2. Telefon doğrulanmış mı? (İSTEK ÜZERİNE EKLENDİ)
        if not getattr(cust, "is_phone_verified", False):
            # Doğrulanmamışsa sessizce çık veya logla
            # write_log(request, "WA İptal", f"Müşteri ({cust}) telefonu doğrulanmadığı için mesaj atılmadı.")
            return False

        can, reason, chosen_lang = wa_preflight(request.user.store, "tamir_bilgi_min_v1", "tr_TR")
        if not can:
            return False

        token = ensure_public_token(repair)
        # _detail_url = build_public_repair_url(request, repair)

        customer_full = (f"{cust.first_name} {cust.last_name}".strip() or "Müşterimiz").strip()
        date_str = fmt_local(repair.created_at)
        status_text = repair.get_status_display()
        price_tl = f"{repair.price:.2f}".replace(".", ",")

        return send_whatsapp_template_guarded(
            store=request.user.store,
            user=request.user,
            customer=cust,
            to=cust.phone,
            template="tamir_bilgi_min_v1",
            language=chosen_lang,
            header_params=[repair.tracking_code or token],
            body_params=[customer_full, (repair.product_type or "Ürün"), status_text, price_tl, date_str],
            button_params=[token],
            validate=False,
        )
    except Exception as e:
        # WhatsApp hatası ana akışı bozmasın
        write_log(request, "WA Gönderim Hatası", f"Hata: {str(e)} - RepairID: {repair.id}")
        return False



def send_repair_created_mail(request, repair: Repairs):
    """
    Müşteriye tamir kaydı e-postası gönderir.
    Merkezi EmailService kullanır.
    """
    try:
        # 1. Email kontrolü (Service içinde de yapılıyor ama burada URL kurmadan önce çıkmak performans sağlar)
        customer_email = getattr(repair.customer, "email", None)
        if not customer_email or str(customer_email).strip() == "":
            return False

        subject = f"Tamir Kaydı • {repair.tracking_code}"
        detail_url = build_public_repair_url(request, repair)

        img_url = None
        try:
            if repair.image and repair.image.url:
                img_url = request.build_absolute_uri(repair.image.url)
        except Exception:
            pass

        ctx = {
            "subject": subject,
            "customer": repair.customer,
            "repair": repair,
            "repair_image_url": img_url,
            "workshop_name": getattr(repair.workshop, "company_name", None),
            "received_by_name": (
                    getattr(repair.received_by, "get_full_name", lambda: "")()
                    or f"{getattr(repair.received_by, 'first_name', '')} {getattr(repair.received_by, 'last_name', '')}".strip()
                    or "-"
            ),
            "delivered_by_name": (getattr(repair.delivered_by, "get_full_name", lambda: "")() or None),
            "received_at_str": fmt_local(repair.created_at),
            "updated_at_str": fmt_local(repair.updated_at),
            "detail_url": detail_url,
        }

        # 2. Merkezi Gönderim
        # config_key: Müşterinin tamir bildirimlerini alıp almama ayarı (notify_email_repair_updates)
        return EmailService.send(
            user=repair.customer,
            subject=subject,
            template_name="management/mail_templates/repair_created.html",
            context=ctx,
            config_key='notify_email_repair_updates'
        )

    except Exception as e:
        write_log(request, "Mail Gönderim Hatası", f"Hata: {str(e)} - RepairID: {repair.id}")
        return False

# -------------------- Görünümler --------------------

@login_required(login_url="login")
@role_required("REPAIRS_REPAIR_INDEX")
def repair_index(request):
    store = request.user.store
    customers = Customers.objects.filter(is_active=True, is_deleted=False, store=store)
    workshops = Workshops.objects.filter(is_active=True, is_deleted=False, store=store)
    context = {"customers": customers, "workshops": workshops, "title": "Tamir İşlemleri"}
    write_log(request, "Tamir İşlemleri", "Tamir kayıtları görüntülendi.")
    return render(request, "management/repairs/index.html", context)


@require_http_methods(["POST"])
@login_required(login_url="login")
def repair_add(request):
    record_id = request.POST.get("record_id")
    customer_id = request.POST.get("customer_id")
    workshop_id = request.POST.get("workshop_id")

    customer = get_object_or_404(Customers, id=customer_id)
    workshop = get_object_or_404(Workshops, id=workshop_id)

    product_type_list = request.POST.getlist("product_type")
    if not product_type_list:
        single_pt = request.POST.get("product_type")
        if single_pt:
            product_type_list = [single_pt]

    product_type_list = [pt for pt in product_type_list if pt]
    product_type_str = ", ".join(product_type_list)

    status = request.POST.get("status", "store")
    store = request.user.store

    try:
        # Veritabanı işlemleri atomik blok içinde
        with transaction.atomic():
            if record_id:
                record = get_object_or_404(Repairs, id=record_id, store=store, is_deleted=False)
            else:
                record = Repairs()
                tracking_source = product_type_list[0] if product_type_list else (product_type_str or "X")
                record.tracking_code = generate_tracking_code(tracking_source)

            record.product_type = product_type_str
            record.workshop = workshop
            record.customer = customer
            record.store = store

            record.gram = parse_decimal(request.POST.get("gram"), "0.000")
            record.price = parse_decimal(request.POST.get("price"), "0.00")

            record.product_description = request.POST.get("product_description")
            record.status = status

            if not record.received_by_id:
                record.received_by = request.user
                record.delivered_by = None
            else:
                if status == "delivered":
                    record.delivered_by = request.user

            image = request.FILES.get("image")
            if image:
                filename, processed_image = process_image(image)
                record.image.save(filename, processed_image, save=False)

            record.save()
            ensure_public_token(record)

            write_log(
                request,
                "Tamir İşlemi",
                f"Tamir kaydı {'Eklendi' if not record_id else 'Güncellendi'}. ID={record.id}",
            )

        # Transaction bittikten sonra bildirimleri gönder (Hata oluşursa DB etkilenmez)
        # Helper fonksiyonların içinde try-except ve verified kontrolü var
        send_repair_created_mail(request, record)
        send_repair_created_whatsapp(request, record)

        return JsonResponse(
            {
                "result": True,
                "message": f"Tamir kaydı {'Eklendi' if not record_id else 'Güncellendi'}.",
                "repair_id": record.id,
                "tracking_code": record.tracking_code,
                "status": record.get_status_display(),
            }
        )

    except Exception as e:
        # Sadece kritik hataları logla
        write_log(request, "Tamir İşlemi Hatası", f"Hata oluştu: {str(e)}")
        return JsonResponse({"error": True, "error_msg": str(e)})


def public_detail(request, token: str):
    repair = get_object_or_404(
        Repairs,
        public_token=token,
        is_deleted=False,
        is_active=True,
    )

    img_url = None
    try:
        if repair.image and repair.image.url:
            img_url = request.build_absolute_uri(repair.image.url)
    except Exception:
        pass

    ctx = {
        "header": {
            "title": "Tamir Detayı",
            "date_str": fmt_local(repair.updated_at),
            "tracking_code": repair.tracking_code or "-",
            "customer_full": f"{repair.customer.first_name} {repair.customer.last_name}".strip(),
            "customer_phone": getattr(repair.customer, "phone", "-") or "-",
            "employee": (getattr(repair.received_by, "get_full_name", lambda: "")() or "-"),
            "repair_token": token,
        },
        "badge": {
            "class": (
                "ok" if repair.status in ("ready_for_pickup", "delivered")
                else "warn" if repair.status in "workshop"
                else "muted"
            ),
            "kind": "Durum",
            "value": repair.get_status_display(),
        },
        "repair": repair,
        "image_url": img_url,
        "workshop_name": getattr(repair.workshop, "company_name", None),
        "times": {
            "received": fmt_local(repair.created_at),
            "workshop": fmt_local(repair.moved_to_workshop_at),
            "ready": fmt_local(repair.ready_for_pickup_at),
            "updated": fmt_local(repair.updated_at),
        },
    }
    return render(request, "management/repairs/public_detail.html", ctx)


@login_required(login_url="login")
def get_all(request):
    try:
        draw = int(request.GET.get("draw", 1))
        length = int(request.GET.get("length", 10))
        start = int(request.GET.get("start", 0))
        search_value = (request.GET.get("search[value]", "") or "").strip()
        order_column_index = request.GET.get("order[0][column]", "0")
        order_column = request.GET.get(f"columns[{order_column_index}][data]", "created_at")
        order_direction = request.GET.get("order[0][dir]", "asc")
        store = request.user.store

        if order_direction == "desc":
            order_column = f"-{order_column}"

        qs = Repairs.objects.filter(is_deleted=False, is_active=True, store=store)
        status_in = (request.GET.get("status__in", "") or "").strip()
        if status_in:
            status_list = [s.strip() for s in status_in.split(",") if s.strip()]
            qs = qs.filter(status__in=status_list)
        else:
            status_exact = (request.GET.get("status", "") or "").strip()
            if status_exact:
                qs = qs.filter(status=status_exact)

        total_records = qs.count()

        if search_value:
            qs = qs.filter(
                Q(tracking_code__icontains=search_value)
                | Q(product_type__icontains=search_value)
                | Q(customer__first_name__icontains=search_value)
                | Q(customer__last_name__icontains=search_value)
                | Q(product_description__icontains=search_value)
            )

        filtered_records = qs.count()
        qs = qs.order_by(order_column)
        if length != -1:
            qs = qs[start: start + length]

        data = []
        for r in qs:
            public_url = build_public_repair_url(request, r)
            data.append(
                {
                    "id": r.id,
                    "tracking_code": r.tracking_code,
                    "customer__id": r.customer.id,
                    "workshop__company_name": r.workshop.company_name if r.workshop else "",
                    "workshop__id": r.workshop.id if r.workshop else "",
                    "customer__first_name": r.customer.first_name,
                    "customer__last_name": r.customer.last_name,
                    "received_by__first_name": r.received_by.first_name if r.received_by else "",
                    "received_by__last_name": r.received_by.last_name if r.received_by else "",
                    "delivered_by__first_name": r.delivered_by.first_name if r.delivered_by else "",
                    "delivered_by__last_name": r.delivered_by.last_name if r.delivered_by else "",
                    "product_type": r.product_type,
                    "status_display": r.get_status_display(),
                    "status": r.status,
                    "price": float(r.price) if r.price else 0.00,
                    "created_at": fmt_local(r.created_at, "%Y-%m-%d %H:%M:%S"),
                    "is_completed": r.is_completed,
                    "public_token": r.public_token,
                    "public_url": public_url,
                }
            )

        return JsonResponse(
            {"draw": draw, "recordsFiltered": filtered_records, "recordsTotal": total_records, "data": data}
        )

    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


@login_required(login_url="login")
def get_workshop_product(request):
    try:
        draw = int(request.GET.get("draw", 1))
        length = int(request.GET.get("length", 10))
        start = int(request.GET.get("start", 0))
        search_value = (request.GET.get("search[value]", "") or "").strip()
        order_column_index = request.GET.get("order[0][column]", "0")
        order_column = request.GET.get(f"columns[{order_column_index}][data]", "created_at")
        order_direction = request.GET.get("order[0][dir]", "asc")
        store = request.user.store
        record_id = request.GET.get("record_id", "")

        if order_direction == "desc":
            order_column = f"-{order_column}"

        qs = Repairs.objects.filter(is_deleted=False, workshop_id=record_id, is_active=True, store=store)

        status_in = request.GET.get("status__in", "")
        if status_in:
            status_list = [x.strip() for x in status_in.split(",") if x.strip()]
            qs = qs.filter(status__in=status_list)

        total_records = qs.count()

        if search_value:
            qs = qs.filter(
                Q(tracking_code__icontains=search_value)
                | Q(product_type__icontains=search_value)
                | Q(customer__first_name__icontains=search_value)
                | Q(customer__last_name__icontains=search_value)
            )

        filtered_records = qs.count()
        qs = qs.order_by(order_column)
        if length != -1:
            qs = qs[start: start + length]

        data = []
        for r in qs:
            data.append(
                {
                    "id": r.id,
                    "tracking_code": r.tracking_code,
                    "customer__id": r.customer.id,
                    "workshop__company_name": r.workshop.company_name if r.workshop else "",
                    "customer__first_name": r.customer.first_name,
                    "customer__last_name": r.customer.last_name,
                    "received_by__first_name": r.received_by.first_name if r.received_by else "",
                    "received_by__last_name": r.received_by.last_name if r.received_by else "",
                    "delivered_by__first_name": r.delivered_by.first_name if r.delivered_by else "",
                    "delivered_by__last_name": r.delivered_by.last_name if r.delivered_by else "",
                    "product_type": r.product_type,
                    "status_display": r.get_status_display(),
                    "status": r.status,
                    "price": float(r.price) if r.price else 0.00,
                    "created_at": fmt_local(r.created_at, "%Y-%m-%d %H:%M:%S"),
                    "is_completed": r.is_completed,
                }
            )

        return JsonResponse(
            {"draw": draw, "recordsFiltered": filtered_records, "recordsTotal": total_records, "data": data}
        )

    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


@login_required(login_url="login")
@role_required("REPAIRS_DELETE_REPAIRS")
def delete_repairs(request):
    if request.method == "POST":
        ids = request.POST.getlist("ids[]")
        try:
            with transaction.atomic():
                Repairs.objects.filter(id__in=ids).delete()
            return JsonResponse({"result": True})
        except Exception as e:
            return JsonResponse({"result": False, "error": True, "error_msg": str(e)})
    return JsonResponse({"result": False, "error": True, "error_msg": "Geçersiz istek."})


@login_required(login_url="login")
def change_repair_status(request):
    if request.method == "POST":
        ids = request.POST.getlist("ids[]")
        try:
            records = Repairs.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save(update_fields=["is_active"])
            return JsonResponse({"result": True})
        except Exception as e:
            return JsonResponse({"result": False, "error": True, "error_msg": str(e)})
    return JsonResponse({"result": False, "error": True, "error_msg": "Geçersiz istek."})


@login_required(login_url="login")
def change_status(request):
    if request.method != "POST":
        return JsonResponse({"result": False, "error": True, "error_msg": "Geçersiz istek."})

    rec_id = request.POST.get("repair_id")
    status = request.POST.get("status")

    try:
        record = Repairs.objects.get(id=rec_id)
        if record.is_completed:
            return JsonResponse({
                "result": False, "error": True,
                "error_msg": "Tamamlanan işlemler için durum değişikliği yapılamaz!"
            })

        prev_status = record.status
        now = timezone.now()

        # Durumu ata
        record.status = status

        update_fields = ["status", "updated_at"]

        if status == "workshop":
            if not getattr(record, "moved_to_workshop_at", None):
                record.moved_to_workshop_at = now
                update_fields.append("moved_to_workshop_at")
            if hasattr(record, "moved_to_workshop_by") and not record.moved_to_workshop_by_id:
                record.moved_to_workshop_by = request.user
                update_fields.append("moved_to_workshop_by")

        elif status == "ready_for_pickup":
            if not getattr(record, "ready_for_pickup_at", None):
                record.ready_for_pickup_at = now
                update_fields.append("ready_for_pickup_at")
            if hasattr(record, "ready_for_pickup_by") and not record.ready_for_pickup_by_id:
                record.ready_for_pickup_by = request.user
                update_fields.append("ready_for_pickup_by")

        elif status == "delivered":
            if not getattr(record, "updated_at", None):
                record.updated_at = now
                update_fields.append("updated_at")
            record.delivered_by = request.user
            record.is_completed = True
            update_fields += ["delivered_by", "is_completed"]

        record.save(update_fields=update_fields)

        # Sadece mağazaya geldiyse (ready_for_pickup) bildir
        if status == "ready_for_pickup" and prev_status != "ready_for_pickup":
            try:
                ensure_public_token(record)
            except Exception:
                pass

            # Helper fonksiyonlar hata yönetimini kendi içinde yapar
            send_repair_created_mail(request, record)
            send_repair_created_whatsapp(request, record)

        return JsonResponse({"result": True, "new_status": status})

    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})


@login_required(login_url="login")
@require_http_methods(["POST"])
def change_status_bulk(request):
    ids = request.POST.getlist("ids[]")
    status = request.POST.get("status")
    if not ids or not status:
        return JsonResponse({"result": False, "error": True, "error_msg": "Eksik parametre."})
    try:
        now = timezone.now()
        processed_records = []

        with transaction.atomic():
            qs = Repairs.objects.filter(id__in=ids, is_deleted=False)
            for r in qs:
                if r.is_completed:
                    continue

                prev_status = r.status
                r.status = status
                update_fields = ["status", "updated_at"]

                if status == "workshop":
                    if not getattr(r, "moved_to_workshop_at", None):
                        r.moved_to_workshop_at = now
                        update_fields.append("moved_to_workshop_at")
                    if hasattr(r, "moved_to_workshop_by") and not r.moved_to_workshop_by_id:
                        r.moved_to_workshop_by = request.user
                        update_fields.append("moved_to_workshop_by")

                elif status == "ready_for_pickup":
                    if not getattr(r, "ready_for_pickup_at", None):
                        r.ready_for_pickup_at = now
                        update_fields.append("ready_for_pickup_at")
                    if hasattr(r, "ready_for_pickup_by") and not r.ready_for_pickup_by_id:
                        r.ready_for_pickup_by = request.user
                        update_fields.append("ready_for_pickup_by")

                elif status == "delivered":
                    if not getattr(r, "updated_at", None):
                        r.updated_at = now
                        update_fields.append("updated_at")
                    r.delivered_by = request.user
                    r.is_completed = True
                    update_fields += ["delivered_by", "is_completed"]

                r.save(update_fields=update_fields)

                # Eğer bildirim gönderilmesi gereken bir durum değişimi ise listeye ekle
                if status == "ready_for_pickup" and prev_status != "ready_for_pickup":
                    processed_records.append(r)

        # Transaction bittikten sonra toplu bildirimleri döngüyle gönder
        for r in processed_records:
            # Token olduğundan emin ol
            ensure_public_token(r)
            # Helperlar hataları yutar
            send_repair_created_mail(request, r)
            send_repair_created_whatsapp(request, r)

        return JsonResponse({"result": True, "new_status": status})
    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})


@login_required(login_url="login")
def get_repair_details(request, repair_id):
    if not repair_id:
        return JsonResponse({"result": False, "error": True, "error_msg": "Kayıt ID eksik."})
    try:
        r = get_object_or_404(Repairs, id=repair_id)

        data = {
            "id": str(r.id),
            "tracking_code": r.tracking_code or "",
            "received_by__first_name": r.received_by.first_name if r.received_by else "",
            "received_by__last_name": r.received_by.last_name if r.received_by else "",
            "delivered_by__first_name": r.delivered_by.first_name if r.delivered_by else "",
            "delivered_by__last_name": r.delivered_by.last_name if r.delivered_by else "",
            "workshop__id": r.workshop.id if r.workshop else "",
            "workshop__company_name": r.workshop.company_name if r.workshop else "",
            "customer__id": r.customer.id if r.customer else "",
            "customer__first_name": r.customer.first_name if r.customer else "",
            "customer__last_name": r.customer.last_name if r.customer else "",
            "customer__phone": r.customer.phone if r.customer and hasattr(r.customer, "phone") else "",
            "product_type": r.product_type or "",
            "product_description": r.product_description or "",
            "status": r.status or "",
            "status_display": r.get_status_display() or "",
            "gram": str(r.gram) if r.gram else "0.000",
            "price": str(r.price) if r.price else "0.00",
            "created_at": fmt_local(r.created_at, "%Y-%m-%d %H:%M:%S"),
            "updated_at": fmt_local(r.updated_at, "%Y-%m-%d %H:%M:%S"),
        }
        return JsonResponse({"result": True, "repair": data})
    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})


@login_required(login_url="login")
@role_required("REPAIRS_REPAIR_INDEX")
def repair_receipt_view(request, repair_id):
    r = get_object_or_404(Repairs, id=repair_id, is_deleted=False)
    paper = "58" if request.GET.get("paper") == "58" else "80"
    auto_print = (request.GET.get("print") in ["1", "true", "yes"])

    store = r.store
    company = getattr(store, "company", None)

    store_meta = {
        "company_title": getattr(company, "title", None),
        "branch_title": getattr(store, "title", None),
        "phone": getattr(store, "phone", None),
        "address": getattr(store, "address", None),
    }

    ctx = {
        "paper": paper,
        "auto_print": auto_print,
        "store_meta": store_meta,
        "workshop_name": getattr(r.workshop, "company_name", None),
        "status_display": r.get_status_display(),
        "received_by_name": (
                getattr(r.received_by, "get_full_name", lambda: "")() or
                f"{getattr(r.received_by, 'first_name', '')} {getattr(r.received_by, 'last_name', '')}".strip() or "-"
        ),
        "delivered_by_name": (
                getattr(r.delivered_by, "get_full_name", lambda: "")() or None
        ),
        "header": {
            "tracking_code": r.tracking_code or r.public_token,
            "date_str": fmt_local(r.created_at),
            "customer_full": f"{r.customer.first_name} {r.customer.last_name}".strip(),
            "customer_phone": getattr(r.customer, "phone", "-") or "-",
        },
        "product": {
            "product_type": r.get_product_type_display() if hasattr(r, "get_product_type_display") else (
                    r.product_type or "-"),
            "gram": r.gram or "0.000",
            "price": f"{Decimal(r.price or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
            "description": r.product_description,
        },
    }
    return render(request, "management/repairs/repair_thermal.html", ctx)
