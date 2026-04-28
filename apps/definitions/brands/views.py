from apps.definitions.brands.models import Brands
from django.http import JsonResponse, HttpResponseRedirect
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.shortcuts import redirect, render
from apps.activity_logs.views import write_log
from django.utils import timezone
from django.contrib import messages


@login_required()
def brands_view(request):
    context = {
        'title': 'Markalar',
    }
    write_log(request, 'Markalar', 'Markalar Görüntülendi.')
    return render(request, 'management/definitions/brands/index.html', context)


@login_required()
def add_brand(request):
    context = {
        'title': 'Marka Ekle',
    }
    if request.POST:
        record_id = request.POST.get('record_id')
        if record_id:
            record = get_object_or_404(Brands, id=record_id)
            record.name = request.POST.get('name')
            record.description = request.POST.get('description')
            record.modified_by = request.user
            record.modified_on = timezone.now()

        else:
            record = Brands()
            record.name = request.POST.get('name')
            record.description = request.POST.get('description')
            record.created_by = request.user
            record.created_on = timezone.now()
        try:
            record.save()
            write_log(request, 'Markalar', 'Marka Eklendi. ID= ' + str(record.id).upper())
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})
    return render(request, 'management/definitions/brands/index.html', context)


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

    queryset = Brands.objects.filter(is_deleted=False).values('id', 'name', 'description', 'is_active',
                                                              'created_by__username', 'created_on',
                                                              'modified_by__username', 'modified_on')

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
            records = Brands.objects.filter(id__in=ids)
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
            records = Brands.objects.filter(id__in=ids)
            for record in records:
                record.is_active = not record.is_active
                record.save()
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})
