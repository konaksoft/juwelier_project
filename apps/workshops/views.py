from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from apps.repairs.models import Repairs
from apps.roles.decorators import role_required
from apps.settings.send_mail import EmailService
from apps.workshops.models import *


@login_required()
@role_required('WORKSHOPS_WORKSHOPS_VIEW')
def workshops_view(request):
    context = {
        'title': 'Atolyeler',
    }
    return render(request, 'management/workshops/index.html', context)


@login_required
@role_required('WORKSHOPS_ADD_WORKSHOP')
def add_workshop(request):
    workshop_id = request.POST.get('workshop_id')

    if workshop_id:
        record = get_object_or_404(Workshops, id=workshop_id)
        record.company_name = request.POST.get("company_name")
        record.person_name = request.POST.get("person_name")
        record.person_surname = request.POST.get("person_surname")
        record.email = request.POST.get("email")
        record.phone = request.POST.get("phone")
        record.company_address = request.POST.get("company_address")
    else:
        record = Workshops()
        record.company_name = request.POST.get("company_name")
        record.person_name = request.POST.get("person_name")
        record.person_surname = request.POST.get("person_surname")
        record.email = request.POST.get("email")
        record.phone = request.POST.get("phone")
        record.company_address = request.POST.get("company_address")
        record.store_id = request.user.store_id
    try:
        record.save()
        return JsonResponse({'result': True, 'workshop_id': record.id})
    except Exception as e:
        return JsonResponse({'result': False, 'error': e})


@login_required(login_url='login')
@role_required('WORKSHOPS_DELETE')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Workshops.objects.filter(id__in=ids)
            for record in records:
                record.is_deleted = True
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
@role_required('WORKSHOPS_CHANGE_STATUS')
def change_status(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = Workshops.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
@role_required('WORKSHOPS_GET_ALL')
def get_all(request):
    draw = int(request.GET['draw'])
    length = int(request.GET['length'])
    start = int(request.GET['start'])
    search_value = request.GET.get('search[value]', '')
    order_column = request.GET['columns[' + request.GET['order[0][column]'] + '][data]']
    order = request.GET['order[0][dir]']
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    is_active = request.GET.get('is_active', '')

    if order_column is None:
        order_column = "company_name"

    if order == 'desc':
        order_column = '-' + order_column
    store_id = request.user.store_id
    queryset = Workshops.objects.filter(is_deleted=False, store_id=store_id).values(
        'id', 'company_name', 'person_name', 'person_surname', 'email', 'phone',
        'company_address', 'is_active',
    )

    if is_active != '':
        is_active = True if is_active.lower() == 'true' else False
        queryset = queryset.filter(is_active=is_active)

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(company_name__icontains=search_value)

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


@login_required()
@role_required('WORKSHOPS_WORKSHOP_DETAIL')
def workshop_detail(request, record_id):
    workshop = get_object_or_404(Workshops, id=record_id)

    context = {
        'title': 'Atolyeler',
        'record': workshop,

    }
    return render(request, 'management/workshops/detail.html', context)


def ensure_workshop_token(workshop):
    if not workshop.public_token:
        import secrets
        token = secrets.token_urlsafe(16)
        workshop.public_token = token
        workshop.save()
    return workshop.public_token


@login_required(login_url='login')
def send_bulk_report_mail(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz İstek'})

    period = request.POST.get('period')  # daily, weekly, monthly
    if not period:
        return JsonResponse({'result': False, 'error_msg': 'Periyot seçilmedi'})

    store = request.user.store

    # E-postası olan aktif atölyeleri getir
    active_workshops = Workshops.objects.filter(
        store=store,
        is_active=True,
        is_deleted=False
    ).exclude(email__isnull=True).exclude(email__exact='')

    sent_count = 0

    try:
        for workshop in active_workshops:
            # Token üretme fonksiyonunuzun (ensure_workshop_token) import edildiğinden emin olun
            token = ensure_workshop_token(workshop)

            base_url = request.build_absolute_uri(reverse('workshops:public-report', args=[token]))
            report_url = f"{base_url}?period={period}"

            subject = f"{workshop.company_name} - Bekleyen Ürün Raporu ({period})"

            period_display = 'Günlük' if period == 'daily' else 'Haftalık' if period == 'weekly' else 'Aylık'

            ctx = {
                'subject': subject,
                'workshop': workshop,
                'period_display': period_display,
                'report_url': report_url,
                'store': store
            }

            is_sent = EmailService.send(
                user=workshop,
                subject=subject,
                template_name='management/mail_templates/workshop_bulk_report.html',
                context=ctx,
                config_key='notify_email_workshops'
            )

            if is_sent:
                sent_count += 1
            # -----------------------------------------

        return JsonResponse({'result': True, 'message': f'{sent_count} atölyeye rapor maili gönderildi.'})

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})


def public_workshop_report_view(request, token):
    workshop = get_object_or_404(Workshops, public_token=token, is_deleted=False, is_active=True)

    period = request.GET.get('period', 'all')

    repairs_qs = Repairs.objects.filter(
        workshop=workshop,
        status='workshop',
        is_deleted=False
    )

    now = timezone.now()
    date_filter_text = "Tüm Zamanlar"

    if period == 'daily':
        start_date = now - timedelta(days=1)
        repairs_qs = repairs_qs.filter(moved_to_workshop_at__gte=start_date)
        date_filter_text = "Son 24 Saat"
    elif period == 'weekly':
        start_date = now - timedelta(weeks=1)
        repairs_qs = repairs_qs.filter(moved_to_workshop_at__gte=start_date)
        date_filter_text = "Son 1 Hafta"
    elif period == 'monthly':
        start_date = now - timedelta(days=30)
        repairs_qs = repairs_qs.filter(moved_to_workshop_at__gte=start_date)
        date_filter_text = "Son 1 Ay"

    total_gram = sum([r.gram for r in repairs_qs if r.gram])
    total_count = repairs_qs.count()

    context = {
        'workshop': workshop,
        'repairs': repairs_qs,
        'period': period,
        'date_filter_text': date_filter_text,
        'total_gram': total_gram,
        'total_count': total_count,
        'generated_at': now
    }

    return render(request, 'management/workshops/public_report.html', context)
