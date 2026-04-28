import json
from decimal import Decimal
from functools import wraps

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.db.models import Q, F
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

from apps.accounts.models import Users
from apps.activity_logs.views import write_log
from apps.chambers.models import Chambers, ChamberProductPrice
from apps.definitions.locations.models import City, District, TaxOffice
from apps.products.models import Products
from apps.roles.decorators import role_required
from apps.roles.models import Roles
from apps.stores.models import Company, Stores


# =====================================================================
# DERNEK BAŞKANI YETKİ DECORATÖRLERİ
# =====================================================================

def chamber_president_required(view_func):
    """
    Sadece CHAMBER kategorili role sahip ve bir derneğe president_user olarak
    bağlı kullanıcıların erişebileceği view'lar için dekoratör.
    """
    @wraps(view_func)
    @login_required(login_url='login')
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if user.is_superuser:
            return view_func(request, *args, **kwargs)

        role = getattr(user, 'role', None)
        if not role or role.category != 'CHAMBER':
            return HttpResponseForbidden("Bu sayfaya erişim yetkiniz bulunmamaktadır.")

        chamber = Chambers.objects.filter(
            president_user=user, is_active=True, is_deleted=False
        ).first()
        if not chamber:
            return HttpResponseForbidden("Hesabınıza bağlı aktif bir dernek bulunamadı.")

        request.president_chamber = chamber
        return view_func(request, *args, **kwargs)

    return _wrapped


def _get_user_chamber(user):
    """Kullanıcının başkanı olduğu derneği döndürür (veya None)."""
    if user.is_superuser:
        return Chambers.objects.filter(is_active=True, is_deleted=False).first()
    return Chambers.objects.filter(
        president_user=user, is_active=True, is_deleted=False
    ).first()


# =====================================================================
# DERNEK BAŞKANI PANELİ (DASHBOARD)
# =====================================================================

@chamber_president_required
def chamber_dashboard_view(request):
    """Dernek başkanının ana yönetim ekranı."""
    chamber = request.president_chamber

    company_ids = chamber.companies.values_list('id', flat=True)
    member_stores = Stores.objects.filter(
        company_id__in=company_ids, is_deleted=False
    )

    today = timezone.now().date()
    today_price_count = ChamberProductPrice.objects.filter(
        chamber=chamber, updated_at__date=today
    ).count()

    last_store = member_stores.select_related('company').order_by('-id').first()

    context = {
        'title': f'{chamber.name} — Yönetim Paneli',
        'chamber': chamber,
        'member_company_count': chamber.companies.filter(is_deleted=False).count(),
        'member_store_count': member_stores.filter(is_active=True).count(),
        'today_price_count': today_price_count,
        'last_store_name': (last_store.title or last_store.company.title) if last_store else '-',
    }
    write_log(request, 'Dernekler', f'Dernek Paneli Görüntülendi. ID={chamber.id}')
    return render(request, 'management/chambers/dashboard.html', context)


@chamber_president_required
def chamber_dashboard_stores(request):
    """Derneğe bağlı mağazaları DataTables formatında döndürür."""
    chamber = request.president_chamber

    draw = int(request.GET.get('draw', '1'))
    length = int(request.GET.get('length', '25'))
    start = int(request.GET.get('start', '0'))
    search_value = (request.GET.get('search[value]') or '').strip()

    company_ids = chamber.companies.filter(is_deleted=False).values_list('id', flat=True)
    queryset = Stores.objects.filter(
        company_id__in=company_ids, is_deleted=False
    ).select_related('company').values(
        'id', 'title', 'phone', 'email', 'city', 'district',
        'is_active', 'company__title'
    )

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(
            Q(title__icontains=search_value) |
            Q(company__title__icontains=search_value) |
            Q(city__icontains=search_value)
        )

    count = queryset.count()

    if str(length) == '-1':
        page_qs = queryset.order_by('company__title', 'title')
    else:
        page_qs = queryset.order_by('company__title', 'title')[start:start + length]

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": list(page_qs)
    })


@chamber_president_required
def chamber_dashboard_products(request):
    """Başkan panelindeki ürün/fiyat DataTables endpoint'i."""
    chamber = request.president_chamber

    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    category_name = request.GET.get('category', '')

    queryset = Products.objects.filter(is_active=True, is_deleted=False)

    if category_name:
        queryset = queryset.filter(category__name=category_name)

    if search_value:
        queryset = queryset.filter(name__icontains=search_value)

    total = queryset.count()

    if length != -1:
        queryset = queryset.order_by('order', 'name')[start:start + length]
    else:
        queryset = queryset.order_by('order', 'name')

    product_ids = [p.id for p in queryset]
    chamber_prices = ChamberProductPrice.objects.filter(chamber=chamber, product_id__in=product_ids)
    price_map = {cp.product_id: cp for cp in chamber_prices}

    data = []
    for p in queryset:
        cp = price_map.get(p.id)
        data.append({
            'id': str(p.id),
            'image': p.image.name if p.image else None,
            'name': p.name,
            'category_name': p.category.name if p.category else '-',
            'global_buy_hs': float(p.buy_price_hs or 0),
            'global_sale_hs': float(p.sale_price_hs or 0),
            'global_labor': float(p.fixed_labor_amount or 0),
            'chamber_buy_hs': float(cp.buy_price_hs) if cp and cp.buy_price_hs is not None else "",
            'chamber_sale_hs': float(cp.sale_price_hs) if cp and cp.sale_price_hs is not None else "",
            'chamber_labor': float(cp.fixed_labor_amount) if cp and cp.fixed_labor_amount is not None else "",
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data
    })


@chamber_president_required
@require_POST
def chamber_dashboard_save_prices(request):
    """Başkan panelinden fiyat güncelleme."""
    chamber = request.president_chamber

    try:
        items = json.loads(request.POST.get('items', '[]'))
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Geçersiz JSON.'}, status=400)

    updated = 0
    with transaction.atomic():
        for item in items:
            p_id = item.get('product_id')
            data = item.get('updated_data', {})

            def parse_decimal(val):
                if val == "" or val is None:
                    return None
                return Decimal(str(val).replace(',', '.'))

            buy_hs = parse_decimal(data.get('chamber_buy_hs'))
            sale_hs = parse_decimal(data.get('chamber_sale_hs'))
            labor = parse_decimal(data.get('chamber_labor'))

            if buy_hs is None and sale_hs is None and labor is None:
                ChamberProductPrice.objects.filter(chamber=chamber, product_id=p_id).delete()
            else:
                ChamberProductPrice.objects.update_or_create(
                    chamber=chamber,
                    product_id=p_id,
                    defaults={
                        'buy_price_hs': buy_hs,
                        'sale_price_hs': sale_hs,
                        'fixed_labor_amount': labor,
                    }
                )
            updated += 1

    write_log(request, "Dernekler", f"Başkan panelinden fiyat güncellendi. Dernek={chamber.name}")
    return JsonResponse({'status': 'success', 'updated': updated})


# =====================================================================
# ADMİN DERNEK YÖNETİMİ (Mevcut — değişmedi)
# =====================================================================

@login_required(login_url='login')
@role_required('CHAMBERS_INDEX_VIEW')
def index_view(request):
    context = {
        'title': 'Kuyumcu Dernek ve Odaları',
    }
    write_log(request, 'Dernekler', 'Dernekler Görüntülendi.')
    return render(request, 'management/chambers/index.html', context)


@login_required(login_url='login')
@role_required('CHAMBERS_ADD_CHAMBER')
@require_POST
def add_chamber(request):
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    record_id = request.POST.get("record_id")
    name = (request.POST.get("name") or "").strip()
    email = (request.POST.get("email") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    website = (request.POST.get("website") or "").strip()
    address = (request.POST.get("address") or "").strip()
    tax_number = (request.POST.get("tax_number") or "").strip()
    registry_number = (request.POST.get("registry_number") or "").strip()
    president_name = (request.POST.get("president_name") or "").strip()
    description = (request.POST.get("description") or "").strip()

    city_id = request.POST.get("city")
    district_id = request.POST.get("district")
    tax_office_id = request.POST.get("tax_office")

    city_obj = City.objects.filter(id=city_id).first() if city_id else None
    district_obj = District.objects.filter(id=district_id).first() if district_id else None
    tax_office_obj = TaxOffice.objects.filter(id=tax_office_id).first() if tax_office_id else None

    if record_id:
        try:
            chamber = Chambers.objects.get(id=record_id, is_deleted=False)
            chamber.name = name or chamber.name
            chamber.email = email or None
            chamber.phone = phone or None
            chamber.website = website or None
            chamber.address = address or None
            chamber.tax_number = tax_number or None
            chamber.registry_number = registry_number or None
            chamber.president_name = president_name or None
            chamber.description = description or None

            chamber.city = city_obj
            chamber.district = district_obj
            chamber.tax_office = tax_office_obj

            chamber.save()
            write_log(request, "Dernekler", f"DERNEK GÜNCELLENDİ. ID={chamber.id}")
            return JsonResponse({"result": True})
        except Chambers.DoesNotExist:
            return JsonResponse({"error": True, "error_msg": "Dernek bulunamadı."})

    if Chambers.objects.filter(name=name, is_deleted=False).exists():
        return JsonResponse({"error": True, "error_msg": "Bu isimde bir dernek/oda zaten kayıtlı."})

    chamber = Chambers.objects.create(
        name=name,
        email=email or None,
        phone=phone or None,
        website=website or None,
        address=address or None,
        tax_number=tax_number or None,
        registry_number=registry_number or None,
        president_name=president_name or None,
        description=description or None,
        city=city_obj,
        district=district_obj,
        tax_office=tax_office_obj
    )
    write_log(request, "Dernekler", f"DERNEK EKLENDİ. ID={chamber.id}")
    return JsonResponse({"result": True})


@login_required(login_url='login')
@role_required('CHAMBERS_DELETE_CHAMBER')
@require_POST
def delete_chamber(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    ids = request.POST.getlist('ids[]')
    try:
        records = Chambers.objects.filter(id__in=ids, is_deleted=False)
        for record in records:
            record.is_deleted = True
            record.save(update_fields=['is_deleted'])
            write_log(request, "Dernekler", f"DERNEK SİLİNDİ. ID={record.id}")
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('CHAMBERS_CHANGE_STATUS')
@require_POST
def change_status(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    ids = request.POST.getlist('ids[]')
    try:
        for record in Chambers.objects.filter(id__in=ids, is_deleted=False):
            record.is_active = not record.is_active
            record.save(update_fields=['is_active'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('CHAMBERS_GET_ALL')
def get_all(request):
    draw = int(request.GET.get('draw', '1'))
    length = int(request.GET.get('length', '25'))
    start = int(request.GET.get('start', '0'))
    search_value = (request.GET.get('search[value]') or '').strip()
    order_column = request.GET.get('columns[' + request.GET.get('order[0][column]', '0') + '][data]')
    order = request.GET.get('order[0][dir]', 'desc')

    if not order_column:
        order_column = "name"
    if order == 'desc':
        order_column = '-' + order_column

    queryset = Chambers.objects.filter(is_deleted=False).annotate(
        city_name=F('city__name'),
        district_name=F('district__name'),
        tax_office_name=F('tax_office__name')
    ).values(
        'id', 'name', 'email', 'phone', 'website', 'address', 'tax_number',
        'registry_number', 'president_name', 'description', 'is_active',
        'city_id', 'city_name', 'district_id', 'district_name',
        'tax_office_id', 'tax_office_name'
    )

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(
            Q(name__icontains=search_value) |
            Q(email__icontains=search_value) |
            Q(president_name__icontains=search_value) |
            Q(city__name__icontains=search_value)
        )

    count = queryset.count()
    if str(length) == '-1':
        page_qs = queryset.order_by(order_column)
    else:
        page_qs = queryset.order_by(order_column)[start:start + length]

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": list(page_qs)
    })


@login_required(login_url='login')
@role_required('CHAMBERS_DETAIL_VIEW')
def detail_view(request, record_id):
    record = get_object_or_404(Chambers, id=record_id, is_deleted=False)

    context = {
        'title': f'{record.name} - Dernek Detayı',
        'record': record,
    }

    write_log(request, 'Dernekler', f'Dernek Detayı Görüntülendi. ID={record.id}')
    return render(request, 'management/chambers/detail.html', context)


@login_required(login_url='login')
@role_required('CHAMBERS_DETAIL_VIEW')
def get_companies(request, record_id):
    chamber = get_object_or_404(Chambers, id=record_id, is_deleted=False)

    draw = int(request.GET.get('draw', '1'))
    length = int(request.GET.get('length', '25'))
    start = int(request.GET.get('start', '0'))
    search_value = (request.GET.get('search[value]') or '').strip()

    order_column = request.GET.get('columns[' + request.GET.get('order[0][column]', '0') + '][data]')
    order = request.GET.get('order[0][dir]', 'desc')

    if not order_column:
        order_column = "title"
    if order == 'desc':
        order_column = '-' + order_column

    queryset = chamber.companies.filter(is_deleted=False).values(
        'id', 'title', 'phone', 'city', 'district', 'is_active'
    )

    total = queryset.count()

    if search_value:
        queryset = queryset.filter(
            Q(title__icontains=search_value) |
            Q(phone__icontains=search_value) |
            Q(city__icontains=search_value) |
            Q(district__icontains=search_value)
        )

    count = queryset.count()

    if str(length) == '-1':
        page_qs = queryset.order_by(order_column)
    else:
        page_qs = queryset.order_by(order_column)[start:start + length]

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": list(page_qs)
    })


@login_required(login_url='login')
@role_required('CHAMBERS_DETAIL_VIEW')
def get_chamber_products(request, record_id):
    """Dernek detay sayfasındaki ürünler ve fiyatlar sekmesini doldurur."""
    chamber = get_object_or_404(Chambers, id=record_id, is_deleted=False)

    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    category_name = request.GET.get('category', '')

    queryset = Products.objects.filter(is_active=True, is_deleted=False)

    if category_name:
        queryset = queryset.filter(category__name=category_name)

    if search_value:
        queryset = queryset.filter(name__icontains=search_value)

    total = queryset.count()

    if length != -1:
        queryset = queryset.order_by('order', 'name')[start:start + length]
    else:
        queryset = queryset.order_by('order', 'name')

    product_ids = [p.id for p in queryset]
    chamber_prices = ChamberProductPrice.objects.filter(chamber=chamber, product_id__in=product_ids)
    price_map = {cp.product_id: cp for cp in chamber_prices}

    data = []
    for p in queryset:
        cp = price_map.get(p.id)
        data.append({
            'id': str(p.id),
            'image': p.image.name if p.image else None,
            'name': p.name,
            'category_name': p.category.name if p.category else '-',
            'global_buy_hs': float(p.buy_price_hs or 0),
            'global_sale_hs': float(p.sale_price_hs or 0),
            'global_labor': float(p.fixed_labor_amount or 0),
            'chamber_buy_hs': float(cp.buy_price_hs) if cp and cp.buy_price_hs is not None else "",
            'chamber_sale_hs': float(cp.sale_price_hs) if cp and cp.sale_price_hs is not None else "",
            'chamber_labor': float(cp.fixed_labor_amount) if cp and cp.fixed_labor_amount is not None else "",
        })

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data
    })


@login_required(login_url='login')
@require_POST
def update_chamber_prices(request):
    try:
        chamber_id = request.POST.get('chamber_id')
        items = json.loads(request.POST.get('items', '[]'))

        chamber = get_object_or_404(Chambers, id=chamber_id, is_deleted=False)

        updated = 0
        with transaction.atomic():
            for item in items:
                p_id = item.get('product_id')
                data = item.get('updated_data', {})

                def parse_decimal(val):
                    if val == "" or val is None:
                        return None
                    return Decimal(str(val).replace(',', '.'))

                buy_hs = parse_decimal(data.get('chamber_buy_hs'))
                sale_hs = parse_decimal(data.get('chamber_sale_hs'))
                labor = parse_decimal(data.get('chamber_labor'))

                if buy_hs is None and sale_hs is None and labor is None:
                    ChamberProductPrice.objects.filter(chamber=chamber, product_id=p_id).delete()
                else:
                    ChamberProductPrice.objects.update_or_create(
                        chamber=chamber,
                        product_id=p_id,
                        defaults={
                            'buy_price_hs': buy_hs,
                            'sale_price_hs': sale_hs,
                            'fixed_labor_amount': labor
                        }
                    )
                updated += 1

        write_log(request, "Dernekler", f"Fiyatlar Güncellendi. Dernek ID={chamber_id}")
        return JsonResponse({'status': 'success', 'updated': updated})

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required(login_url='login')
@require_GET
def get_available_companies(request, record_id):
    """Derneğe henüz üye OLMAYAN firmaları Select2 için getirir."""
    chamber = get_object_or_404(Chambers, id=record_id, is_deleted=False)
    search = request.GET.get('q', '')

    companies = Company.objects.filter(is_deleted=False, is_active=True).exclude(chambers=chamber)

    if search:
        companies = companies.filter(title__icontains=search)

    data = [{'id': str(c.id), 'text': c.title} for c in companies[:50]]
    return JsonResponse({'results': data})


@login_required(login_url='login')
@require_POST
def add_company_to_chamber(request):
    """Seçilen firmayı derneğe ekler."""
    chamber_id = request.POST.get('chamber_id')
    company_id = request.POST.get('company_id')

    chamber = get_object_or_404(Chambers, id=chamber_id, is_deleted=False)
    company = get_object_or_404(Company, id=company_id, is_deleted=False)

    chamber.companies.add(company)
    write_log(request, "Dernekler", f"{company.title} firması {chamber.name} derneğine eklendi.")

    return JsonResponse({'result': True})


@login_required(login_url='login')
@require_POST
def remove_company_from_chamber(request):
    chamber_id = request.POST.get('chamber_id')
    company_id = request.POST.get('company_id')

    chamber = get_object_or_404(Chambers, id=chamber_id, is_deleted=False)
    company = get_object_or_404(Company, id=company_id, is_deleted=False)

    chamber.companies.remove(company)
    write_log(request, "Dernekler", f"{company.title} firması {chamber.name} derneğinden çıkarıldı.")

    return JsonResponse({'result': True})


# =====================================================================
# BAŞKAN ATAMA ENDPOINTLERİ (Admin tarafı)
# =====================================================================

@login_required(login_url='login')
@require_GET
def get_available_president_users(request, record_id):
    """
    Belirtilen derneğe atanabilecek kullanıcıları döndürür.
    CHAMBER kategorili role sahip ve henüz başka bir derneğe atanmamış kullanıcılar.
    (veya zaten bu derneğin başkanı olan kullanıcı dahil)
    """
    chamber = get_object_or_404(Chambers, id=record_id, is_deleted=False)
    search = request.GET.get('q', '').strip()

    chamber_roles = Roles.objects.filter(category='CHAMBER', is_deleted=False).values_list('id', flat=True)

    assigned_user_ids = Chambers.objects.filter(
        is_deleted=False, president_user__isnull=False
    ).exclude(id=chamber.id).values_list('president_user_id', flat=True)

    queryset = Users.objects.filter(
        role_id__in=chamber_roles,
        is_active=True,
        is_deleted=False,
    ).exclude(id__in=assigned_user_ids)

    if search:
        queryset = queryset.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(username__icontains=search) |
            Q(email__icontains=search)
        )

    data = [{
        'id': u.id,
        'text': f"{u.get_full_name()} ({u.username})" if u.get_full_name().strip() else u.username,
    } for u in queryset[:50]]

    return JsonResponse({'results': data})


@login_required(login_url='login')
@require_POST
def assign_president_user(request):
    """Bir derneğe başkan kullanıcısı atar veya kaldırır."""
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    chamber_id = request.POST.get('chamber_id')
    user_id = request.POST.get('user_id')

    chamber = get_object_or_404(Chambers, id=chamber_id, is_deleted=False)

    with transaction.atomic():
        if user_id:
            user = get_object_or_404(Users, id=user_id, is_active=True, is_deleted=False)
            Chambers.objects.filter(president_user=user).update(president_user=None)
            chamber.president_user = user
            chamber.save(update_fields=['president_user', 'updated_at'])
            write_log(request, "Dernekler",
                      f"Başkan atandı: {user.username} → {chamber.name}")
        else:
            chamber.president_user = None
            chamber.save(update_fields=['president_user', 'updated_at'])
            write_log(request, "Dernekler", f"Başkan kaldırıldı: {chamber.name}")

    return JsonResponse({'result': True})


@login_required(login_url='login')
@require_POST
def quick_create_president(request):
    """
    Hızlı başkan kullanıcısı oluşturur ve doğrudan ilgili derneğe atar.
    Kullanıcı CHAMBER kategorili ilk aktif role atanır.
    """
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden("Yetkiniz bulunmamaktadır.")

    chamber_id = request.POST.get('chamber_id')
    first_name = (request.POST.get('first_name') or '').strip()
    last_name = (request.POST.get('last_name') or '').strip()
    email = (request.POST.get('email') or '').strip()
    username = (request.POST.get('username') or '').strip()
    password = (request.POST.get('password') or '').strip()

    if not all([first_name, last_name, email, username, password]):
        return JsonResponse({'error': True, 'error_msg': 'Tüm alanlar zorunludur.'})

    chamber = get_object_or_404(Chambers, id=chamber_id, is_deleted=False)

    if Users.objects.filter(username=username).exists():
        return JsonResponse({'error': True, 'error_msg': 'Bu kullanıcı adı zaten kullanılıyor.'})
    if Users.objects.filter(email=email).exists():
        return JsonResponse({'error': True, 'error_msg': 'Bu e-posta adresi zaten kullanılıyor.'})

    chamber_role = Roles.objects.filter(category='CHAMBER', is_deleted=False, is_active=True).first()
    if not chamber_role:
        return JsonResponse({'error': True, 'error_msg': 'CHAMBER kategorisinde aktif bir rol bulunamadı. Önce rol oluşturun.'})

    with transaction.atomic():
        user = Users(
            first_name=first_name,
            last_name=last_name,
            email=email,
            username=username,
            is_active=True,
            is_staff=True,
            is_superuser=False,
            role=chamber_role,
        )
        user.set_password(password)
        user.save()

        Chambers.objects.filter(president_user=user).update(president_user=None)
        chamber.president_user = user
        chamber.save(update_fields=['president_user', 'updated_at'])

    write_log(request, "Dernekler",
              f"Hızlı başkan oluşturuldu: {user.username} → {chamber.name}")

    return JsonResponse({
        'result': True,
        'user_id': user.id,
        'user_display': f"{user.get_full_name()} ({user.username})",
    })
