import uuid
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from apps.contact_forms.models import ContactForms
from apps.activity_logs.views import write_log

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.templatetags.static import static as static_url
from django.core.mail import EmailMultiAlternatives
from django.contrib.staticfiles import finders
from mimetypes import guess_type

from email.mime.image import MIMEImage


def _send_contact_notification(cf: ContactForms, request=None):
    ctx = {
        "full_name": cf.full_name or "",
        "email": cf.email or "",
        "phone": cf.phone or "",
        "message": cf.message or "",
        "created_on": cf.created_on,
        "ip": request.META.get("REMOTE_ADDR") if request else "",
        "ua": request.META.get("HTTP_USER_AGENT") if request else "",
        "site_name": getattr(settings, "SITE_NAME", "Kuyum Plus"),
    }

    subject = "Yeni İletişim Formu Bildirimi"

    html_body = render_to_string(
        "management/contact_forms/contact_form_notification.html",
        {**ctx, "logo_cid": "kuyumplus_logo"}
    )
    text_body = strip_tags(html_body)

    from_addr = getattr(settings, "EMAIL_FROM_ADDRESS", getattr(settings, "DEFAULT_FROM_EMAIL", None))
    recipients = ["enes.karagoz@konasoft.com.tr", "yunus.konak@konasoft.com.tr"]

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_addr,
        to=recipients,
    )
    msg.attach_alternative(html_body, "text/html")

    logo_path = finders.find("theme/img/new_logo/1.png")
    if logo_path:
        with open(logo_path, "rb") as f:
            data = f.read()
        mime = guess_type(logo_path)[0] or "image/png"
        _maintype, subtype = mime.split("/", 1)
        img = MIMEImage(data, _subtype=subtype)
        img.add_header("Content-ID", "<kuyumplus_logo>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(img)
        msg.mixed_subtype = "related"

    msg.send(fail_silently=False)


def contact_page(request):
    success = False
    if request.method == 'POST':
        cf = ContactForms(
            full_name=(request.POST.get('full_name', '') or '').strip(),
            email=(request.POST.get('email', '') or '').strip(),
            phone=(request.POST.get('phone', '') or '').strip(),
            message=(request.POST.get('message', '') or '').strip(),
            created_on=timezone.now()
        )
        cf.save()
        write_log(request, 'public iletişim formu', f'{cf.full_name} mesaj gönderdi')
        try:
            _send_contact_notification(cf, request)
        except Exception as exc:
            write_log(request, 'mail', f'İletişim formu mail gönderilemedi: {exc}')
        success = True

    return render(request, 'theme/contact.html', {
        'title': 'İletişim',
        'success': success
    })


@login_required(login_url='login')
def contact_forms_view(request):
    context = {
        'title': 'İletişim Formu',
    }
    write_log(request, 'iletişim formu', 'İletişim formları görüntülendi.')
    return render(request, 'management/contact_forms/index.html', context)


@login_required(login_url='login')
def add_contact_form(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})

    record_id = request.POST.get('record_id')
    full_name = request.POST.get('full_name')
    email = request.POST.get('email')
    phone = request.POST.get('phone')
    message = request.POST.get('message')

    if record_id:
        record = get_object_or_404(ContactForms, id=record_id, is_deleted=False)
        record.full_name = full_name
        record.email = email
        record.phone = phone
        record.message = message
        action = 'güncellendi'
    else:
        record = ContactForms(
            full_name=full_name,
            email=email,
            phone=phone,
            message=message,
        )
        action = 'eklendi'

    try:
        record.save()
        write_log(request, 'iletişim formu', f'İletişim formu {action}. ID={record.id}')
        return JsonResponse({'result': True})
    except Exception as exc:  # pragma: no cover
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(exc)})


@login_required(login_url='login')
def get_all(request):
    draw = int(request.GET.get('draw', 0))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '')
    order_col_idx = request.GET.get('order[0][column]', '0')
    order_dir = request.GET.get('order[0][dir]', 'asc')

    order_column = request.GET.get(f'columns[{order_col_idx}][data]', 'created_on')
    if order_dir == 'desc':
        order_column = f'-{order_column}'

    qs = ContactForms.objects.filter(is_deleted=False).values(
        'id', 'full_name', 'phone', 'email', 'message',
        'is_active', 'created_on',
    )

    total = qs.count()

    if search_value:
        qs = qs.filter(full_name__icontains=search_value)

    count = qs.count()

    if length != -1:
        qs = qs.order_by(order_column)[start:start + length]
    else:
        qs = qs.order_by(order_column)

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total,
        'recordsFiltered': count,
        'data': list(qs),
    })


@login_required(login_url='login')
def delete(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})

    ids = request.POST.getlist('ids[]') or request.POST.getlist('ids')
    try:
        ContactForms.objects.filter(id__in=ids).update(is_deleted=True)
        return JsonResponse({'result': True})
    except Exception as exc:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(exc)})


@login_required(login_url='login')
def change_status(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})

    ids = request.POST.getlist('ids[]') or request.POST.getlist('ids')

    try:
        for cf in ContactForms.objects.filter(id__in=ids):
            cf.is_active = not cf.is_active
            cf.save()
        return JsonResponse({'result': True})
    except Exception as exc:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(exc)})
