# apps/testimonials/views.py
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.db.models import Q

# Önceki adımda oluşturduğumuz model
from apps.testimonials.models import Testimonial
# Rol dekoratörünüz (Eğer henüz bu roller tanımlı değilse bu satırı yorum satırı yapın)
from apps.roles.decorators import role_required


@login_required(login_url='login')
# @role_required('TESTIMONIALS_INDEX') # Rol tanımlarını veritabanına eklediyseniz açın
def index(request):
    return render(request, 'management/testimonials/index.html', {
        'title': 'Referans Yönetimi'
    })


@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
# @role_required('TESTIMONIALS_ADD')
def add(request):
    """
    Referans Ekleme ve Güncelleme İşlemi
    """
    testimonial_id = (request.POST.get('testimonial_id') or '').strip()

    full_name = (request.POST.get('full_name') or "").strip()
    company_name = (request.POST.get('company_name') or "").strip()
    message = (request.POST.get('message') or "").strip()
    order_val = request.POST.get('order') or "0"

    logo_file = request.FILES.get('logo')

    if testimonial_id:
        # --- GÜNCELLEME ---
        try:
            row = Testimonial.objects.get(id=testimonial_id)
        except Testimonial.DoesNotExist:
            return JsonResponse({'error': True, 'error_msg': 'Kayıt bulunamadı!'}, status=404)

        row.full_name = full_name
        row.company_name = company_name
        row.message = message
        row.order = int(order_val)

        if logo_file:
            row.logo = logo_file

        # Eğer resmi silmek isterlerse (Frontend'de avatar_remove inputu gelirse)
        if request.POST.get('avatar_remove') == '1':
            row.logo = None

        row.save()
        return JsonResponse({'result': True})

    # --- YENİ KAYIT ---
    Testimonial.objects.create(
        full_name=full_name,
        company_name=company_name,
        message=message,
        logo=logo_file,
        order=int(order_val),
        is_active=True
    )

    return JsonResponse({'result': True})


@login_required(login_url='login')
# @role_required('TESTIMONIALS_GET_ALL')
def get_all(request):
    try:
        draw = int(request.GET.get('draw', 1))
        length = int(request.GET.get('length', 10))
        start = int(request.GET.get('start', 0))
        search_value = (request.GET.get('search[value]', '') or '').strip()
        order_column_index = request.GET.get('order[0][column]', '0')
        order_dir = request.GET.get('order[0][dir]', 'asc')

        # Temel Sorgu
        qs = Testimonial.objects.all()

        # Arama
        if search_value:
            qs = qs.filter(
                Q(full_name__icontains=search_value) |
                Q(company_name__icontains=search_value) |
                Q(message__icontains=search_value)
            )

        total_records = Testimonial.objects.count()
        filtered_records = qs.count()

        # Sıralama Haritası (Frontend'deki kolon sırasına göre)
        # 0: Checkbox, 1: Logo, 2: Ad Soyad, 3: Firma, 4: Sıralama, 5: Durum, 6: İşlem
        columns_map = {
            '2': 'full_name',
            '3': 'company_name',
            '4': 'order',
            '5': 'is_active',
        }

        order_field = columns_map.get(str(order_column_index), '-created_at')
        if order_dir == 'desc':
            if not order_field.startswith('-'):
                order_field = '-' + order_field

        qs = qs.order_by(order_field)

        if length != -1:
            qs = qs[start:start + length]

        data = []
        for r in qs:
            # Logo URL kontrolü
            img_url = None
            if r.logo and hasattr(r.logo, 'url'):
                img_url = r.logo.url

            data.append({
                'id': str(r.id),
                'full_name': r.full_name,
                'company_name': r.company_name,
                'message': r.message,
                'order': r.order,
                'is_active': r.is_active,
                'image_url': img_url,
                'created_at': r.created_at.strftime('%d.%m.%Y %H:%M')
            })

        return JsonResponse({
            "draw": draw,
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data
        })
    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)}, status=500)


@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
# @role_required('TESTIMONIALS_DELETE')
def delete(request):
    ids = request.POST.getlist('ids[]') or []
    try:
        Testimonial.objects.filter(id__in=ids).delete()
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
# @role_required('TESTIMONIALS_CHANGE_STATUS')
def change_status(request):
    ids = request.POST.getlist('ids[]') or []
    try:
        rows = Testimonial.objects.filter(id__in=ids)
        for r in rows:
            r.is_active = not r.is_active
            r.save(update_fields=['is_active'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)
