from apps.roles.decorators import role_required
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import render
from apps.activity_logs.models import ActivityLogs
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
import json
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.ttfonts import TTFont
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
import random
import string
from django.shortcuts import render, redirect
import io
from django.db.models import Q
from datetime import datetime
from django.http import FileResponse
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from django.views.decorators.csrf import csrf_exempt
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib import messages
from apps.accounts.models import *
from django.core.files.base import ContentFile


@login_required()
@role_required('ACTIVITY_LOGS_ACTIVITY_LOGS_VIEW')
def activity_logs_view(request):
    context = {
        'title': 'Aktivite Logları',
        'ActivityLogs': ActivityLogs.objects.filter(is_deleted=False),
    }
    write_log(request, 'Aktivite Logları', ' Görüntülendi.')
    return render(request, 'management/activity_logs/index.html', context)


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


@login_required()
def write_log(request, title, description):
    new_record = ActivityLogs()
    new_record.title = title
    new_record.description = description
    new_record.ip_address = get_client_ip(request)
    new_record.created_by_id = request.user.id
    new_record.created_on = timezone.now()

    try:
        new_record.save()
        return True
    except Exception as e:
        return False



@login_required(login_url='login')
@role_required('ACTIVITY_LOGS_GET_ALL')
def get_all(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '')
    order_column_index = request.GET.get('order[0][column]', '0')
    order_column = request.GET.get(f'columns[{order_column_index}][data]', 'created_on')
    order = request.GET.get('order[0][dir]', 'asc')

    valid_order_columns = ['title', 'description', 'created_by__first_name', 'created_on']
    if order_column not in valid_order_columns:
        order_column = 'created_on'

    if order == 'desc':
        order_column = '-' + order_column

    queryset = ActivityLogs.objects.filter(is_deleted=False).values(
        'created_by__first_name', 'created_on', 'description', 'id', 'ip_address', 'title'
    )

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(title__icontains=search_value)

    count = queryset.count()

    if length != -1:
        queryset = queryset.order_by(order_column)[start:start + length]
    else:
        queryset = queryset.order_by(order_column)

    data = []
    for entry in queryset:
        created_on = entry['created_on']
        if created_on:
            entry['created_on'] = created_on.strftime('%d/%m/%Y %H:%M')
        data.append(entry)

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": data
    })

@login_required(login_url='login')
@role_required('ACTIVITY_LOGS_DELETE')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = ActivityLogs.objects.filter(id__in=ids)
            for record in records:
                record.is_deleted = True
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
@role_required('ACTIVITY_LOGS_CHANGE_STATUS')
def change_status(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        print(ids)
        try:
            records = ActivityLogs.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})