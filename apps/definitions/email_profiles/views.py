from apps.definitions.email_profiles.models import EmailProfiles
from django.http import JsonResponse, HttpResponseRedirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from apps.activity_logs.views import write_log



@login_required()
def email_profiles_view(request):
    context = {
        'title': 'E-Postalar',
    }
    write_log(request, 'E-Posta Profilleri', 'E-Posta Profilleri Görüntülendi.')
    return render(request, 'management/definitions/email_profiles/index.html', context)


@login_required()
def add_email_profile(request):
    context = {
        'title': 'E-Posta Ekle',
    }
    if request.POST:
        record_id = request.POST.get('record_id')
        if record_id:
            record = get_object_or_404(EmailProfiles, id=record_id)
            record.username = request.POST.get('username')
            record.name = request.POST.get('name')
            record.password = request.POST.get('password')
            record.server = request.POST.get('server')
            record.port = request.POST.get('port')
            record.sender = request.POST.get('sender')
            record.ssl = request.POST.get('ssl') == 'on'
            record.tls = request.POST.get('tls') == 'on'
        else:
            record = EmailProfiles()
            record.username = request.POST.get('username')
            record.name = request.POST.get('name')
            record.password = request.POST.get('password')
            record.server = request.POST.get('server')
            record.port = request.POST.get('port')
            record.sender = request.POST.get('sender')
            record.ssl = request.POST.get('ssl') == 'on'
            record.tls = request.POST.get('tls') == 'on'
        try:
            record.save()
            write_log(request, 'E-Posta Profilleri', 'E-Posta Profili Eklendi. ID= ' + str(record.id).upper())
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})
    return render(request, 'management/definitions/email_profiles/index.html', context)


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

    queryset = EmailProfiles.objects.filter(is_deleted=False).values('id', 'name', 'username',
                                                                     'password', 'server',
                                                                     'port', 'sender', 'ssl', 'tls', 'is_active')

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


@login_required(login_url='login')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            records = EmailProfiles.objects.filter(id__in=ids)
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
            records = EmailProfiles.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})