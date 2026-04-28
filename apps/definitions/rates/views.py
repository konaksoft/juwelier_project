from apps.definitions.rates.models import *
from apps.definitions.currencies.models import *
from django.http import JsonResponse, HttpResponseRedirect
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.shortcuts import redirect, render
from apps.activity_logs.views import write_log
from django.utils import timezone
import requests
from datetime import datetime
from django.utils.timezone import make_aware
from apps.definitions.rates.models import Rates
from apps.definitions.currencies.models import Currencies
from apps.definitions.categories.models import *
from apps.products.models import *
from django.utils.timezone import now
from django.contrib import messages



@login_required()
def rates_view(request):
    context = {
        'title': 'Kurlar',
        'currencies': Currencies.objects.filter(is_deleted=False, is_active=True),
    }
    write_log(request, 'Kurlar', 'Kurlar Görüntülendi.')
    return render(request, 'management/definitions/rates/index.html', context)


@login_required()
def add_rate(request):
    context = {
        'title': 'Kur Ekle',
        'currencies': Currencies.objects.filter(is_deleted=False, is_active=True),
    }
    if request.POST:
        record_id = request.POST.get('record_id')

        if record_id:
            record = get_object_or_404(Rates, id=record_id)
            record.currency_one_id = request.POST.get('currency_one_id')
            record.currency_two_id = request.POST.get('currency_two_id')
            record.buy_price = request.POST.get('buy_price')
            record.sale_price = request.POST.get('sale_price')
            record.market_time = request.POST.get('market_time')
            record.modified_on = timezone.now()
        else:
            record = Rates()
            record.currency_one_id = request.POST.get('currency_one_id')
            record.currency_two_id = request.POST.get('currency_two_id')
            record.buy_price = request.POST.get('buy_price')
            record.sale_price = request.POST.get('sale_price')
            record.market_time = request.POST.get('market_time')

        try:
            record.save()
            write_log(request, 'Kurlar', 'Kur Eklendi. ID= ' + str(record.id).upper())
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})

    return render(request, 'management/definitions/rates/index.html', context)


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

    queryset = Rates.objects.filter(is_deleted=False).values('id', 'name', 'currency_one__name',
                                                             'currency_two__name', 'currency_one__code',
                                                             'currency_two__code',
                                                             'buy_price', 'market_time',
                                                             'sale_price', 'modified_on',
                                                             'is_active')

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
            records = Rates.objects.filter(id__in=ids)
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
            records = Rates.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})
