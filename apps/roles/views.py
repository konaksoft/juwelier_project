# apps/roles/views.py
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from django.db.models import Q
from django.views.decorators.http import require_http_methods

from apps.roles.models import Roles, Permission, RoleDetail
from apps.activity_logs.views import write_log
from apps.roles.decorators import role_required
from apps.accounts.models import Users
from apps.stores.services import get_store_effective_permission_ids


@login_required
@role_required('ROLE_MANAGE_VIEW')
def roles_view(request):
    """
    Faz 12.4 — Rol listeleme view'ı.

    Görünürlük kuralları:
      - Superuser / is_staff: Tüm roller (SYSTEM, STORE, CHAMBER)
      - Mağaza kullanıcısı: Global STORE roller (store=NULL, category='STORE')
        + Kendi mağazasına ait izole roller (store=<kendi_store>)
    """
    user = request.user
    if user.is_superuser or user.is_staff:
        roles = Roles.objects.filter(is_deleted=False)
    else:
        user_store_id = getattr(user, 'store_id', None)
        if user_store_id:
            # Global STORE roller + kendi mağazasına ait izole roller
            roles = Roles.objects.filter(
                is_deleted=False
            ).filter(
                Q(category='STORE', store__isnull=True) |
                Q(store_id=user_store_id)
            )
        else:
            # Mağazası olmayan kullanıcı — sadece global STORE roller
            roles = Roles.objects.filter(is_deleted=False, category='STORE', store__isnull=True)

    write_log(request, 'Güvenlik Rolleri', 'Görüntülendi.')
    return render(request, 'management/roles/index.html', {
        'roles': roles,
        'title': 'Güvenlik Rolleri'
    })


@login_required
@role_required('ROLE_MANAGE_ADD')
@require_http_methods(['GET', 'POST'])
def add_role(request):
    """
    Faz 12.4 — Rol ekleme/güncelleme view'ı.

    store_id parametresi:
      - Superuser / is_staff: İsteğe bağlı; boş bırakılırsa global rol oluşturulur.
      - Mağaza kullanıcısı: Otomatik olarak kendi store_id'si atanır.

    Kural:
      - category='SYSTEM' veya 'CHAMBER' ise store her zaman NULL olmalı.
      - category='STORE' ise store opsiyonel; dolu ise izole rol.
    """
    if request.method == 'POST':
        record_id = request.POST.get('record_id', '').strip()
        name = (request.POST.get('name') or '').strip()
        description = (request.POST.get('description') or '').strip()
        category = (request.POST.get('category') or 'STORE').strip()

        # Faz 12.4: store_id parametresini al
        store_id = (request.POST.get('store_id') or '').strip() or None

        # SYSTEM ve CHAMBER kategorileri her zaman global
        if category in ('SYSTEM', 'CHAMBER'):
            store_id = None

        # Mağaza kullanıcısı sadece kendi mağazası için izole rol oluşturabilir
        user = request.user
        if not (user.is_superuser or user.is_staff):
            user_store_id = getattr(user, 'store_id', None)
            if user_store_id:
                store_id = str(user_store_id)
                category = 'STORE'  # Mağaza kullanıcısı sadece STORE rolü oluşturabilir
            else:
                store_id = None

        if record_id:
            record = get_object_or_404(Roles, id=record_id, is_deleted=False)
            record.name = name
            record.description = description
            record.category = category
            record.store_id = store_id
        else:
            record = Roles(
                name=name,
                description=description,
                category=category,
                store_id=store_id,
            )

        try:
            record.save()
            return JsonResponse({'result': True, 'record_id': str(record.id)})
        except Exception as e:
            return JsonResponse({'error': True, 'error_msg': str(e)})

    return render(request, 'management/roles/add.html')


@login_required
@require_http_methods(['GET', 'POST'])
@role_required('ROLES_DETAIL_ROLE')
def detail_role(request, record_id):
    """
    Faz 12.4 — Rol detay / yetki atama view'ı.

    Yetki filtreleme kuralları:
      - Superuser / is_staff → Tüm Permission kayıtları gösterilir.
      - Mağaza kullanıcısı → Sadece group='Dashboard' (mağaza operasyon
        yetkileri) VE mağazanın efektif yetki havuzundaki permission'lar
        gösterilir. is_system_only=True yetkiler gizlenir.

    İzole rol kontrolü:
      - Rol bir mağazaya aitse (store != NULL) ve kullanıcı farklı
        mağazadaysa → Erişim engellenir (404).
    """
    role = get_object_or_404(Roles, id=record_id, is_deleted=False)
    user = request.user

    # ── Erişim kontrolü: İzole rol sadece sahibi mağaza veya superadmin görebilir ──
    if role.store_id and not (user.is_superuser or user.is_staff):
        user_store_id = getattr(user, 'store_id', None)
        if str(role.store_id) != str(user_store_id):
            from django.http import Http404
            raise Http404("Bu rol mağazanıza ait değil.")

    # ── Yetki listesi (store-scoped filtreleme) ──
    if user.is_superuser or user.is_staff:
        # Superadmin / Konasoft personeli: Tüm yetkiler
        all_perms = list(Permission.objects.all().order_by('group', 'order', 'code'))
    else:
        # Mağaza kullanıcısı: Sadece Dashboard grubundaki yetkiler
        # + mağazanın efektif yetki havuzundaki yetkiler
        user_store = getattr(user, 'store', None)
        if user_store:
            effective_perm_ids = get_store_effective_permission_ids(user_store)
            all_perms = list(
                Permission.objects.filter(
                    group='Dashboard',
                    is_system_only=False,
                    id__in=effective_perm_ids,
                ).order_by('group', 'order', 'code')
            )
        else:
            # Mağazası olmayan kullanıcı — sadece Dashboard grubunu göster
            all_perms = list(
                Permission.objects.filter(
                    group='Dashboard',
                    is_system_only=False,
                ).order_by('group', 'order', 'code')
            )

    # Seçili izin id'leri (UUID set)
    checked_perm_ids = set(
        RoleDetail.objects.filter(role=role, status=True)
        .values_list('permission_id', flat=True)
    )

    # Rolün kullanıcıları (store filtreli)
    req_store_id = getattr(request.user, 'store_id', None)
    if req_store_id:
        users = Users.objects.filter(role=role, store_id=req_store_id, is_active=True)
    else:
        users = Users.objects.filter(role=role, is_active=True)

    if request.method == 'POST':
        role.name = (request.POST.get('name') or '').strip()
        role.description = (request.POST.get('description') or '').strip()

        # Hem permission_ids hem permission_ids[] desteği
        raw_ids = request.POST.getlist('permission_ids[]') or request.POST.getlist('permission_ids')
        selected_ids = list(dict.fromkeys(raw_ids))  # benzersiz sıralı

        # Sadece var olan Permission id'lerini kabul et
        valid_ids = set(
            Permission.objects.filter(id__in=selected_ids).values_list('id', flat=True)
        )

        # Mağaza kullanıcısı: Sadece efektif havuzdaki yetkileri atayabilir
        if not (user.is_superuser or user.is_staff):
            user_store = getattr(user, 'store', None)
            if user_store:
                effective_perm_ids = get_store_effective_permission_ids(user_store)
                valid_ids = valid_ids & effective_perm_ids

        try:
            with transaction.atomic():
                role.save()
                RoleDetail.objects.filter(role=role).delete()
                RoleDetail.objects.bulk_create(
                    [RoleDetail(role=role, permission_id=pid, status=True) for pid in valid_ids],
                    ignore_conflicts=True
                )

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'result': True})

            messages.success(request, 'Rol başarıyla güncellendi.')
            return redirect('roles:detail', record_id=str(role.id))

        except Exception as e:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'error': True, 'error_msg': str(e)})
            messages.error(request, f'Hata: {e}')

        # POST başarısızsa ekranda işaretlerin kalması için güncelle
        checked_perm_ids = valid_ids

    return render(request, 'management/roles/detail.html', {
        'record': role,
        'all_perms': all_perms,
        'checked_perm_ids': checked_perm_ids,
        'users': users,
        'title': f'Rol Detay - {role.name}'
    })


@login_required(login_url='login')
@role_required('ROLES_DELETE')
@require_http_methods(['POST'])
def delete(request):
    ids = request.POST.getlist('ids[]')
    try:
        Roles.objects.filter(id__in=ids).update(is_deleted=True)
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@role_required('ROLES_CHANGE_STATUS')
@require_http_methods(['POST'])
def change_status(request):
    ids = request.POST.getlist('ids[]')
    try:
        records = Roles.objects.filter(id__in=ids)
        for r in records:
            r.is_active = not r.is_active
            r.save(update_fields=['is_active'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required
@role_required('ROLE_MANAGE_VIEW')
def get_all(request):
    """
    Faz 12.4 — DataTable endpoint (server-side).

    Filtreleme kuralları:
      - Superuser / is_staff: Tüm roller
      - Mağaza kullanıcısı: Global STORE roller (store=NULL, category='STORE')
        + kendi mağazasına ait izole roller

    Response'a store_name alanı eklendi.
    """
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    order_col_index = request.GET.get('order[0][column]', '0')
    order_column = request.GET.get(f'columns[{order_col_index}][data]', 'name')
    order_dir = request.GET.get('order[0][dir]', 'asc')

    if order_dir == 'desc':
        order_column = '-' + order_column

    # Faz 12.4: Kullanıcı türüne göre base queryset
    user = request.user
    if user.is_superuser or user.is_staff:
        base_qs = Roles.objects.filter(is_deleted=False).select_related('store')
    else:
        user_store_id = getattr(user, 'store_id', None)
        if user_store_id:
            base_qs = Roles.objects.filter(is_deleted=False).filter(
                Q(category='STORE', store__isnull=True) |
                Q(store_id=user_store_id)
            ).select_related('store')
        else:
            base_qs = Roles.objects.filter(
                is_deleted=False, category='STORE', store__isnull=True
            ).select_related('store')

    total_count = base_qs.count()

    qs = base_qs
    if search_value:
        qs = qs.filter(Q(name__icontains=search_value) | Q(description__icontains=search_value))

    filtered_count = qs.count()

    if length != -1:
        qs = qs.order_by(order_column)[start:start + length]
    else:
        qs = qs.order_by(order_column)

    data = []
    for r in qs:
        # Faz 12.4: store bilgisi eklendi
        store_name = ''
        if r.store:
            store_name = r.store.title or r.store.store_id or str(r.store.id)

        data.append({
            'id': str(r.id),
            'name': r.name,
            'description': r.description,
            'is_active': r.is_active,
            'category': r.category,
            'store_name': store_name,  # Faz 12.4: Mağaza adı
            'is_global': r.store_id is None,  # Faz 12.4: Global mi?
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total_count,
        'recordsFiltered': filtered_count,
        'data': data
    })

@login_required
@role_required('ROLE_MANAGE_VIEW')
def get_users_role(request, record_id):
    users = Users.objects.filter(role_id=record_id, is_active=True)
    data = [{
        'id': str(u.id),
        'first_name': u.first_name,
        'username': u.username,
        'email': u.email,
        'mobile_phone': u.mobile_phone,
        'is_active': u.is_active,
        'is_superuser': u.is_superuser,
        'is_staff': u.is_staff,
        'date_joined': u.date_joined,
        'avatar': str(getattr(u, 'avatar', '') or '')
    } for u in users]
    return JsonResponse({'data': data})