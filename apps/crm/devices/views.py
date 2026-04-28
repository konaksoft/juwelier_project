# apps/devices/views.py
from decimal import Decimal
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.db.models import Q

from apps.roles.decorators import role_required  # Sizin role decorator'ınız
from apps.crm.devices.models import *


@login_required(login_url='login')
@role_required('DEVICES_INDEX')
def index(request):
    """Cihazlar ana sayfası"""
    return render(request, 'management/crm/devices/index.html', {
        'title': 'Cihaz Yönetimi'
    })


@login_required(login_url='login')
@require_http_methods(["GET"])
@role_required('DEVICES_GET_ALL')
def get_all(request):
    """DataTable listesi"""
    try:
        draw = int(request.GET.get('draw', 1))
        length = int(request.GET.get('length', 10))
        start = int(request.GET.get('start', 0))
        search_value = (request.GET.get('search[value]', '') or '').strip()

        # Filtreleme
        qs = Device.objects.all()
        if search_value:
            qs = qs.filter(
                Q(name__icontains=search_value) |
                Q(code__icontains=search_value) |
                Q(description__icontains=search_value)
            )

        total_records = qs.count()

        # Sıralama (Basit versiyon)
        qs = qs.order_by('-created_at')

        if length != -1:
            qs = qs[start:start + length]

        data = []
        for d in qs:
            data.append({
                'id': str(d.id),
                'image': d.image.url if d.image else None,
                'code': d.code,
                'name': d.name,
                'price_formatted': f"{d.price} {d.currency_symbol}",
                'price': str(d.price),
                'currency': d.currency,
                'description': d.description or "",
                'is_active': d.is_active,
            })

        return JsonResponse({
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": total_records,  # Filtreli sayısı ile aynı basitleştirildi
            "data": data
        })
    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('DEVICES_ADD')
def add(request):
    """Ekleme ve Güncelleme"""
    try:
        device_id = request.POST.get('device_id')
        name = request.POST.get('name')
        code = request.POST.get('code')
        price = request.POST.get('price')
        currency = request.POST.get('currency')
        description = request.POST.get('description')
        image = request.FILES.get('image')

        if device_id:
            device = get_object_or_404(Device, id=device_id)
            device.name = name
            device.code = code
            device.price = Decimal(price) if price else Decimal('0')
            device.currency = currency
            device.description = description
            if image:
                device.image = image
            device.save()
        else:
            Device.objects.create(
                name=name,
                code=code,
                price=Decimal(price) if price else Decimal('0'),
                currency=currency,
                description=description,
                image=image,
                created_by=request.user
            )

        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@require_http_methods(["POST"])
@role_required('DEVICES_DELETE')
def delete(request):
    """Silme"""
    ids = request.POST.getlist('ids[]')
    try:
        Device.objects.filter(id__in=ids).delete()
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})


@login_required(login_url='login')
@require_http_methods(["GET"])
def get_device_info(request):
    """Teklif formunda seçilince fiyat getirmek için AJAX"""
    device_id = request.GET.get('id')
    try:
        d = Device.objects.get(id=device_id)
        return JsonResponse({
            'result': True,
            'price': str(d.price),
            'currency': d.currency,
            'name': d.name
        })
    except Device.DoesNotExist:
        return JsonResponse({'result': False, 'error_msg': 'Cihaz bulunamadı'})