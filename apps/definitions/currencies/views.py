from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import render
from apps.activity_logs.views import write_log
from apps.definitions.currencies.models import *
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages


@login_required()
def currencies_view(request):
    context = {
        'title': 'Para Birimleri',
        'languages': Currencies.objects.filter(is_deleted=False),
    }
    write_log(request, 'Para Birimi', 'Para Birimleri Görüntülendi.')
    return render(request, 'management/definitions/currencies/index.html', context)


@login_required()
def add_currency(request):
    context = {
        'title': 'Para Birimi Ekle',
    }
    if request.POST:
        record_id = request.POST.get('record_id')
        if record_id:
            record = get_object_or_404(Currencies, id=record_id)
            record.name = request.POST.get('name')
            record.code = request.POST.get('code')
            record.symbol = request.POST.get('symbol')
            record.digit_group_separator = request.POST.get('digit_group_separator')
            record.decimal_character = request.POST.get('decimal_character')
            record.round = request.POST.get('round')
            record.splice = request.POST.get('splice')
            record.after_comma = request.POST.get('after_comma')
            record.modified_by = request.user
            record.modified_on = timezone.now()
        else:
            record = Currencies()
            record.name = request.POST.get('name')
            record.code = request.POST.get('code')
            record.symbol = request.POST.get('symbol')
            record.digit_group_separator = request.POST.get('digit_group_separator')
            record.decimal_character = request.POST.get('decimal_character')
            record.round = request.POST.get('round')
            record.splice = request.POST.get('splice')
            record.after_comma = request.POST.get('after_comma')
            record.created_by = request.user
            record.created_on = timezone.now()

        try:
            record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})
    return render(request, 'management/definitions/currencies/index.html', context)


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

    queryset = Currencies.objects.filter(is_deleted=False).values('id', 'name', 'is_active', 'code', 'symbol',
                                                                  'digit_group_separator', 'decimal_character', 'round',
                                                                  'splice',
                                                                  'after_comma', 'created_by__username', 'created_on',
                                                                  'modified_by__username', 'modified_on', )

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
            records = Currencies.objects.filter(id__in=ids)
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
            records = Currencies.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})
