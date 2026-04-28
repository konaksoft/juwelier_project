from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from apps.accounts.views import reset_password_confirm_view
from apps.definitions.sms_profiles.models import *
from apps.activity_logs.views import write_log



@login_required()
def sms_profiles_view(request):
    context = {
        'title': 'SMSler',
    }
    write_log(request, 'SMS Profilleri', 'SMS Profilleri Görüntülendi.')
    return render(request, 'management/definitions/sms_profiles/index.html', context)


@login_required()
def add_sms_profile(request):
    context = {
        'title': 'SMS Profili Ekle',
    }
    if request.POST:
        record_id = request.POST.get('record_id')
        if record_id:
            record = get_object_or_404(SmsProfiles, id=record_id)
            record.name = request.POST.get('name')
            record.api_url = request.POST.get('api_url')
            record.username = request.POST.get('username')
            record.password = request.POST.get('password')
            record.sms_header = request.POST.get('sms_header')
            record.sms_provider = request.POST.get('sms_provider')
            record.description = request.POST.get('description')

        else:
            record = SmsProfiles()
            record.name = request.POST.get('name')
            record.api_url = request.POST.get('api_url')
            record.username = request.POST.get('username')
            record.password = request.POST.get('password')
            record.sms_header = request.POST.get('sms_header')
            record.sms_provider = request.POST.get('sms_provider')
            record.description = request.POST.get('description')
        try:
            record.save()
            write_log(request, 'SMS Profilleri', 'SMS Profili Eklendi. ID= ' + str(record.id).upper())
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})
    return render(request, 'management/definitions/sms_profiles/index.html', context)


@login_required(login_url='login')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = SmsProfiles.objects.filter(id__in=ids)
            for record in records:
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
            records = SmsProfiles.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})



@login_required(login_url='login')
def get_all(request):
    draw = int(request.GET['draw'])
    length = int(request.GET['length'])
    start = int(request.GET['start'])
    search_value = request.GET['search[value]']
    order_column = request.GET['columns[' + request.GET['order[0][column]'] + '][data]']
    order = request.GET['order[0][dir]']

    if order_column is None:
        order_column = "created_on"

    if order == 'desc':
        order_column = '-' + order_column

    queryset = SmsProfiles.objects.filter(is_deleted=False).values('id', 'name', 'username', 'password', 'api_url',
                                                                   'sms_header', 'sms_provider','description', 'is_active')

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(name__icontains=search_value)

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