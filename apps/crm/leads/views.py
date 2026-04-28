import random
import re
import secrets
import string
from datetime import timedelta, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import *
from apps.activity_logs.views import write_log
from apps.crm.leads.models import *
from apps.crm.packages.models import SaaSModule
from apps.crm.proposals.models import Proposals, ProposalItems, ProposalLogs
from apps.process.models import Process, Payment
from apps.roles.decorators import role_required
from apps.whatsapp.services import wa_preflight, send_whatsapp_template_guarded
from apps.definitions.locations.models import *
from django.db.models import OuterRef, Subquery
from django.db.models.functions import Coalesce
from apps.roles.models import RoleDetail

import csv, io, unicodedata
from django.http import HttpResponseBadRequest


def check_can_assign(user):
    if user.is_superuser:
        return True

    if not hasattr(user, 'role') or not user.role:
        return False

    return RoleDetail.objects.filter(
        role=user.role,
        permission__code='LEAD_ASSIGN_STAFF',
        status=True
    ).exists()


def fmt_ts(dt):
    if not dt:
        return ''
    try:
        if getattr(settings, 'USE_TZ', False):
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            dt = timezone.localtime(dt)
        return dt.strftime('%d.%m.%Y %H:%M')
    except Exception:
        return dt.strftime('%d.%m.%Y %H:%M') if dt else ''


@login_required()
def leads_view(request):
    users = Users.objects.filter(
        store=request.user.store,
        is_active=True
    ).exclude(is_superuser=True).order_by('first_name', 'last_name')

    can_assign = check_can_assign(request.user)

    context = {
        'title': 'Müşteri Adayları',
        'users': users,
        'category_choices': Lead.CATEGORY_CHOICES,
        'status_choices': Lead.STATUSES,
        'can_assign': can_assign,
    }
    write_log(request, 'Leads', 'Leads Liste Görüntülendi.')
    return render(request, 'management/crm/leads/index.html', context)


@login_required()
def add_lead(request):
    store = request.user.store

    if request.POST:
        def _none_if_blank(v):
            v = (v or "").strip()
            return v or None

        full_name = _none_if_blank(request.POST.get('full_name'))
        email = _none_if_blank(request.POST.get('email'))

        if not full_name:
            return JsonResponse(
                {'result': False, 'error': True, 'error_msg': 'Ad Soyad zorunludur.'},
                status=400
            )

        # Şehir ve İlçe İşlemleri
        req_city = _none_if_blank(request.POST.get('city'))
        req_district = _none_if_blank(request.POST.get('district'))
        city_val = req_city
        district_val = req_district

        if req_city:
            try:
                c_obj = City.objects.get(id=req_city)
                city_val = c_obj.name
            except (City.DoesNotExist, ValueError):
                pass

        if req_district:
            try:
                d_obj = District.objects.get(id=req_district)
                district_val = d_obj.name
            except (District.DoesNotExist, ValueError):
                pass

        record_id = request.POST.get('record_id')

        # Temel Alanlar
        payload = {
            'full_name': full_name,
            'business_name': _none_if_blank(request.POST.get('business_name')),
            'phone': _none_if_blank(request.POST.get('phone')),
            'email': email,
            'channel': _none_if_blank(request.POST.get('channel')) or 'instagram',
            'channel_handle': _none_if_blank(request.POST.get('channel_handle')),
            'status': _none_if_blank(request.POST.get('status')) or 'new',
            'category': _none_if_blank(request.POST.get('category')),
            'city': city_val,
            'district': district_val,
            'score': int(request.POST.get('score') or 0),
        }

        # Multi-select'ten gelen kullanıcı ID listesi
        assigned_ids = request.POST.getlist('assigned_users[]')

        user_can_assign = check_can_assign(request.user)

        stage_payload = None

        try:
            if record_id:
                # --- GÜNCELLEME ---
                qs = Lead.objects.filter(id=record_id, store=store, is_deleted=False)

                # Yetki Kontrolü: Superuser değilse sadece kendi oluşturduğu veya atandığı lead'i düzenleyebilir
                if not request.user.is_superuser:
                    qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

                record = get_object_or_404(qs)
                prev_status = record.status

                # Alanları güncelle
                for k, v in payload.items():
                    setattr(record, k, v)

                record.last_activity_at = timezone.now()
                record.save()

                if user_can_assign:
                    if assigned_ids:
                        users_to_assign = Users.objects.filter(id__in=assigned_ids, store=store)
                        record.assigned_users.set(users_to_assign)
                    else:
                        record.assigned_users.clear()
                else:
                    record.assigned_users.add(request.user)

                # Durum değişikliği takibi
                if prev_status != record.status:
                    status_map = dict(Lead.STATUSES)
                    h = LeadStageHistory.objects.create(
                        lead=record,
                        from_status=prev_status,
                        to_status=record.status,
                        changed_by=request.user,
                        note="Ayarlar'dan güncellendi"
                    )
                    stage_payload = {
                        'from_status': prev_status,
                        'to_status': record.status,
                        'from_status_label': status_map.get(prev_status, prev_status),
                        'to_status_label': status_map.get(record.status, record.status),
                        'note': h.note,
                        'changed_by': str(request.user),
                        'changed_on': fmt_ts(h.changed_on),
                    }

            else:
                # --- YENİ KAYIT ---
                record = Lead(
                    store=store,
                    created_by=request.user,
                    last_activity_at=timezone.now(),
                    **payload
                )
                record.save()  # Önce kaydet, sonra M2M ekle

                if user_can_assign:
                    if assigned_ids:
                        users_to_assign = Users.objects.filter(id__in=assigned_ids, store=store)
                        record.assigned_users.set(users_to_assign)
                else:
                    record.assigned_users.add(request.user)

            write_log(request, 'Leads', f'Lead Kaydedildi/Güncellendi. ID={record.id}')
            return JsonResponse({'result': True, 'lead_id': str(record.id), 'stage': stage_payload})

        except IntegrityError:
            return JsonResponse({'result': False, 'error': True, 'error_msg': 'Telefon veya E-posta sistemde kayıtlı.'},
                                status=400)
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=400)

    # GET isteği
    users = Users.objects.filter(store=store, is_active=True).order_by('first_name', 'last_name')
    context = {'title': 'Müşteri Adayı Ekle', 'users': users}
    return render(request, 'management/crm/leads/index.html', context)


@login_required(login_url='login')
def get_leads(request):
    store = request.user.store
    qs = Lead.objects.filter(is_deleted=False, is_active=True, store=store)

    # Güvenlik: Sadece yetkili olduklarını gör
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

    # Basit liste dönüşü
    data = list(qs.values('id', 'full_name', 'phone', 'email', 'status', 'channel'))
    return JsonResponse(data, safe=False)


@login_required(login_url='login')
def get_all(request):
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 10))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    order_column = request.GET.get('columns[' + request.GET.get('order[0][column]', '0') + '][data]')
    order_dir = request.GET.get('order[0][dir]', 'asc')

    filter_tab = request.GET.get('filter_tab', 'all')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    filter_category = request.GET.get('filter_category')
    filter_city = request.GET.get('filter_city')
    filter_user = request.GET.get('filter_user')

    if not order_column:
        order_column = "last_activity_at"
        order_dir = "desc"

    prefix = '-' if order_dir == 'desc' else ''

    if order_column == 'created_by_name':
        order_column = 'created_by__first_name'
    elif order_column == 'assigned_users_names':
        order_column = 'created_on'

    user_store = request.user.store
    qs = Lead.objects.filter(is_deleted=False, store=user_store)

    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

    if filter_tab == 'new':
        qs = qs.filter(status__in=['new', 'giriş'])
    elif filter_tab == 'follow_up':
        qs = qs.exclude(status__in=['new', 'giriş', 'won', 'lost', 'spam', 'dnc'])
    elif filter_tab == 'completed':
        qs = qs.filter(status__in=['won', 'lost', 'spam', 'dnc'])

    if start_date and end_date:
        try:
            sd = datetime.strptime(start_date, '%Y-%m-%d')
            ed = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            qs = qs.filter(created_on__range=[sd, ed])
        except ValueError:
            pass

    if filter_category: qs = qs.filter(category=filter_category)
    if filter_city: qs = qs.filter(Q(city__icontains=filter_city) | Q(district__icontains=filter_city))
    if filter_user:
        qs = qs.filter(assigned_users__id=filter_user)

    if search_value:
        qs = qs.filter(
            Q(full_name__icontains=search_value) |
            Q(business_name__icontains=search_value) |
            Q(phone__icontains=search_value) |
            Q(email__icontains=search_value) |
            Q(city__icontains=search_value) |
            Q(district__icontains=search_value) |
            Q(channel__icontains=search_value)
        ).distinct()

    qs = qs.annotate(
        sort_date=Coalesce('last_activity_at', 'created_on')
    )

    if order_column == 'last_activity_at':
        qs = qs.order_by(f'{prefix}sort_date')
    else:
        if order_dir == 'desc':
            qs = qs.order_by(f'-{order_column}')
        else:
            qs = qs.order_by(order_column)

    total = qs.count()

    if str(length) != '-1':
        qs = qs[start:start + length]

    latest_activity = LeadActivity.objects.filter(lead=OuterRef('pk')).order_by('-activity_date')
    qs = qs.annotate(
        last_activity_outcome=Subquery(latest_activity.values('outcome')[:1]),
        last_activity_summary=Subquery(latest_activity.values('summary')[:1])
    ).prefetch_related('assigned_users')

    data = []
    for lead in qs:
        assigned_users_list = lead.assigned_users.all()
        assigned_names = ", ".join([u.get_full_name() or u.username for u in assigned_users_list])
        assigned_ids = [str(u.id) for u in assigned_users_list]

        display_date = lead.last_activity_at if lead.last_activity_at else lead.created_on
        created_by_name = "-"
        if lead.created_by:
            created_by_name = lead.created_by.get_full_name() or lead.created_by.username

        data.append({
            'id': lead.id,
            'full_name': lead.full_name,
            'business_name': lead.business_name,
            'phone': lead.phone,
            'email': lead.email,
            'status': lead.status,
            'channel': lead.channel,
            'channel_handle': lead.channel_handle,
            'category': lead.category,
            'city': lead.city,
            'district': lead.district,
            'score': lead.score,
            'assigned_users_names': assigned_names,
            'assigned_users_ids': assigned_ids,
            'is_active': lead.is_active,
            'created_on': lead.created_on,
            'created_by_name': created_by_name,
            'last_activity_at': display_date,
            'last_activity_outcome': lead.last_activity_outcome,
            'last_activity_summary': lead.last_activity_summary
        })

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": total,
        "recordsTotal": total,
        "data": data
    })


@login_required()
def quick_field_update(request):
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek'})

    lead_id = request.POST.get('id')
    field = request.POST.get('field')
    value = request.POST.get('value')

    store = request.user.store

    # GÜVENLİK
    qs = Lead.objects.filter(id=lead_id, store=store, is_deleted=False)
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

    lead = get_object_or_404(qs)

    if field == 'category':
        valid_choices = [c[0] for c in Lead.CATEGORY_CHOICES] + ['']
        if value not in valid_choices:
            return JsonResponse({'result': False, 'error_msg': 'Geçersiz kategori'})
        lead.category = value or None

    elif field == 'city':
        val = value.strip() if value else None
        if val:
            try:
                c_obj = City.objects.get(id=val)
                val = c_obj.name
            except (City.DoesNotExist, ValueError):
                pass
        lead.city = val

    elif field == 'business_name':
        lead.business_name = value.strip() if value else None

    else:
        return JsonResponse({'result': False, 'error_msg': 'Bu alan güncellenemez'})

    lead.last_activity_at = timezone.now()
    lead.save(update_fields=[field, 'last_activity_at'])

    return JsonResponse({'result': True})


@login_required(login_url='login')
def delete(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            qs = Lead.objects.filter(id__in=ids, store=request.user.store)

            # GÜVENLİK
            if not request.user.is_superuser:
                qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

            for record in qs:
                record.is_deleted = True
                record.save(update_fields=['is_deleted'])
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required(login_url='login')
def change_status(request):
    if request.method == 'POST':
        ids = request.POST.getlist('ids[]')
        try:
            qs = Lead.objects.filter(id__in=ids, store=request.user.store)

            # GÜVENLİK
            if not request.user.is_superuser:
                qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

            for record in qs:
                record.is_active = not record.is_active
                record.save(update_fields=['is_active'])
            return JsonResponse({'result': True})
        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})
    return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz istek.'})


@login_required()
def lead_detail_view(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, is_deleted=False, store=request.user.store)

    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

    lead = get_object_or_404(qs)

    activities = (LeadActivity.objects
                  .filter(lead=lead)
                  .select_related('created_by')
                  .order_by('-activity_date'))

    tags = (LeadTagMap.objects
            .filter(lead=lead)
            .select_related('tag')
            .order_by('added_on'))

    notes = (LeadNote.objects
             .filter(lead=lead)
             .select_related('author')
             .order_by('-created_on'))

    stage_history = (LeadStageHistory.objects
                     .filter(lead=lead)
                     .select_related('changed_by')
                     .order_by('-changed_on'))

    state_history = (LeadStateHistory.objects
                     .filter(lead=lead)
                     .select_related('changed_by')
                     .order_by('-changed_on'))

    channel_choices = Lead.CHANNELS
    status_choices = Lead.STATUSES
    category_choices = Lead.CATEGORY_CHOICES
    state_choices = LeadStateHistory.STATES

    users = Users.objects.filter(store=request.user.store, is_active=True).order_by('first_name', 'last_name')

    context = {
        'title': f'Lead Detay • {lead.full_name or lead.phone or lead.email or str(lead.id)}',
        'lead': lead,
        'tags': tags,
        'notes': notes,
        'stage_history': stage_history,
        'state_history': state_history,
        'activities': activities,
        'channel_choices': channel_choices,
        'status_choices': status_choices,
        'category_choices': category_choices,
        'state_choices': state_choices,
        'users': users,
    }
    write_log(request, 'Leads', f'Lead Detay Görüntülendi. ID= {lead.id}')
    return render(request, 'management/crm/leads/detail.html', context)


@login_required()
def add_note(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, is_deleted=False, store=request.user.store)
    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()
    lead = get_object_or_404(qs)

    body = (request.POST.get('body') or '').strip()
    is_private = bool(request.POST.get('is_private'))
    if not body:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Not boş olamaz.'})

    note = LeadNote.objects.create(
        lead=lead,
        author=request.user,
        body=body,
        is_private=is_private,
    )
    lead.last_activity_at = timezone.now()
    lead.save(update_fields=['last_activity_at'])

    return JsonResponse({
        'result': True,
        'data': {
            'id': str(note.id),
            'author': str(note.author) if note.author else '-',
            'body': note.body,
            'is_private': note.is_private,
            'created_on': fmt_ts(note.created_on),
        }
    })


@login_required()
def add_tag(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, is_deleted=False, store=request.user.store)
    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()
    lead = get_object_or_404(qs)

    name = (request.POST.get('name') or '').strip()
    if not name:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Etiket adı boş olamaz.'})

    try:
        tag, _ = LeadTag.objects.get_or_create(store=request.user.store, name=name)
        map_obj, created = LeadTagMap.objects.get_or_create(lead=lead, tag=tag, defaults={'added_by': request.user})
        if not created:
            return JsonResponse({'result': False, 'error': True, 'error_msg': 'Etiket zaten ekli.'})
        lead.last_activity_at = timezone.now()
        lead.save(update_fields=['last_activity_at'])
        return JsonResponse(
            {'result': True, 'data': {'map_id': str(map_obj.id), 'tag_id': str(tag.id), 'name': tag.name}})
    except IntegrityError:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Etiket zaten mevcut.'})


@login_required()
def remove_tag(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, is_deleted=False, store=request.user.store)
    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()
    lead = get_object_or_404(qs)

    map_id = request.POST.get('map_id')
    try:
        m = LeadTagMap.objects.get(id=map_id, lead=lead)
        m.delete()
        lead.last_activity_at = timezone.now()
        lead.save(update_fields=['last_activity_at'])
        return JsonResponse({'result': True})
    except LeadTagMap.DoesNotExist:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Etiket bulunamadı.'})


@login_required()
def change_stage(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, is_deleted=False, store=request.user.store)
    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()
    lead = get_object_or_404(qs)

    to_status = (request.POST.get('to_status') or '').strip()
    note = (request.POST.get('note') or '').strip()

    status_map = dict(Lead.STATUSES)

    if to_status not in status_map.keys():
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz aşama.'})

    from_status = lead.status
    if from_status == to_status:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Zaten bu aşamada.'})

    LeadStageHistory.objects.create(
        lead=lead,
        from_status=from_status,
        to_status=to_status,
        changed_by=request.user,
        note=note or None,
    )
    lead.status = to_status
    lead.last_activity_at = timezone.now()
    lead.save(update_fields=['status', 'last_activity_at'])

    return JsonResponse({
        'result': True,
        'data': {
            'from_status': from_status,
            'to_status': to_status,
            'from_status_label': status_map.get(from_status, from_status),
            'to_status_label': status_map.get(to_status, to_status),
            'note': note or '',
            'changed_by': str(request.user),
            'changed_on': fmt_ts(timezone.now()),
        }
    })


@login_required()
def change_state(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, is_deleted=False, store=request.user.store)
    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()
    lead = get_object_or_404(qs)

    state = (request.POST.get('state') or '').strip()
    note = (request.POST.get('note') or '').strip()

    valid_states = dict(LeadStateHistory.STATES)
    if state not in valid_states.keys():
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'Geçersiz durum.'})

    LeadStateHistory.objects.create(
        lead=lead,
        state=state,
        note=note or None,
        changed_by=request.user
    )
    lead.current_state = state
    lead.last_activity_at = timezone.now()
    lead.save(update_fields=['current_state', 'last_activity_at'])

    return JsonResponse({
        'result': True,
        'data': {
            'state': state,
            'state_label': valid_states[state],
            'note': note or '',
            'changed_by': str(request.user),
            'changed_on': fmt_ts(timezone.now()),
        }
    })


@login_required()
def import_csv(request):
    """
    CSV İçe Aktarma:
    - assigned_to sütunundaki kullanıcıları bulur.
    - Bulduğu kullanıcıyı lead.assigned_users (M2M) alanına ekler.
    """
    if request.method != 'POST' or 'file' not in request.FILES:
        return HttpResponseBadRequest('CSV dosyası gerekli')

    store = getattr(request.user, 'store', None)
    up = request.FILES['file']

    try:
        sample = up.read(4096).decode('utf-8', 'ignore')
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=[',', ';', '\t', '|'])
            delimiter = dialect.delimiter
        except Exception:
            delimiter = ';' if (';' in sample and ',' not in sample) else ','
    finally:
        up.seek(0)

    text = io.TextIOWrapper(up.file, encoding='utf-8-sig')
    reader = csv.DictReader(text, delimiter=delimiter)

    if not reader.fieldnames:
        return JsonResponse({'result': False, 'error': True, 'error_msg': 'CSV başlıkları okunamadı.'}, status=400)

    field_map = {_norm(h): h for h in reader.fieldnames}

    NAME_KEYS = ['fullname', 'adsoyad', 'ad', 'isim', 'isimsoyisim', 'name', 'musteriadi']
    BUSINESS_KEYS = ['business_name', 'businessname', 'magazaadi', 'magaza', 'firma', 'firmaadi', 'company']
    PHONE_KEYS = ['phone', 'telefon', 'tel', 'gsm', 'cep', 'mobile', 'mobil']
    EMAIL_KEYS = ['email', 'e-mail', 'eposta', 'e-posta', 'mail']
    CHANNEL_KEYS = ['channel', 'kaynak', 'kanal']
    HANDLE_KEYS = ['channelhandle', 'handle', 'kullaniciadi', 'username', 'profil', 'hesap']
    STATUS_KEYS = ['status', 'durum', 'asama', 'stage']
    ASSIGN_KEYS = ['assigned_to', 'assignedto', 'atanan', 'atananemail', 'assignedtoemail', 'sorumlu', 'assigned']
    CATEGORY_KEYS = ['category', 'kategori']
    CITY_KEYS = ['city', 'sehir', 'il']
    DISTRICT_KEYS = ['district', 'ilce']
    SCORE_KEYS = ['score', 'puan']
    CREATED_KEYS = ['olusturuldu', 'olusturma', 'olusturulmatarihi', 'olusturmatarihi',
                    'created', 'createdon', 'created_at', 'createdtime', 'kayit', 'kayittarihi', 'tarih']

    def getv(row, keys):
        for k in keys:
            orig = field_map.get(_norm(k))
            if orig:
                v = (row.get(orig) or '').strip()
                if v:
                    return v
        for nk, orig in field_map.items():
            if nk in keys:
                v = (row.get(orig) or '').strip()
                if v:
                    return v
        return ''

    created = updated = skipped = 0
    no_key = 0

    for row in reader:
        full_name = getv(row, NAME_KEYS) or None
        business_name = getv(row, BUSINESS_KEYS) or None
        phone = getv(row, PHONE_KEYS) or None
        email = getv(row, EMAIL_KEYS) or None
        channel = (getv(row, CHANNEL_KEYS) or 'instagram').lower()
        channel_handle = getv(row, HANDLE_KEYS) or None
        status = (getv(row, STATUS_KEYS) or 'new').lower()
        category = getv(row, CATEGORY_KEYS) or None
        city = getv(row, CITY_KEYS) or None
        district = getv(row, DISTRICT_KEYS) or None
        score_str = getv(row, SCORE_KEYS)
        try:
            score = int(score_str) if score_str else 0
        except Exception:
            score = 0

        assigned_hint = getv(row, ASSIGN_KEYS)
        created_str = getv(row, CREATED_KEYS)
        created_dt = _parse_dt(created_str)

        # CSV'den gelen atanan kullanıcıyı bul
        target_user = None
        if assigned_hint:
            try:
                target_user = Users.objects.filter(id=assigned_hint, store=store, is_active=True).first()
            except Exception:
                pass
            target_user = (target_user or
                           Users.objects.filter(email__iexact=assigned_hint, store=store, is_active=True).first() or
                           Users.objects.filter(username__iexact=assigned_hint, store=store, is_active=True).first() or
                           Users.objects.filter(full_name__iexact=assigned_hint, store=store, is_active=True).first())

        if phone:
            lookup = {'store': store, 'phone': phone}
        elif email:
            lookup = {'store': store, 'email': email}
        else:
            skipped += 1
            no_key += 1
            continue

        try:
            obj = Lead.objects.filter(**lookup).first()

            if obj:
                changed = False

                def set_if_empty(field, new_val, empty_values=(None, '')):
                    nonlocal changed
                    cur = getattr(obj, field)
                    if (cur in empty_values) and (new_val not in empty_values):
                        setattr(obj, field, new_val)
                        changed = True

                set_if_empty('full_name', full_name)
                set_if_empty('business_name', business_name)
                set_if_empty('email', email)
                set_if_empty('phone', phone)
                set_if_empty('channel', channel)
                set_if_empty('channel_handle', channel_handle)
                set_if_empty('category', category)
                set_if_empty('city', city)
                set_if_empty('district', district)

                if (obj.score in (None, 0)) and (score not in (None, 0)):
                    obj.score = score
                    changed = True

                # M2M Güncelleme: Eğer CSV'de atanan varsa ve lead'e henüz ekli değilse ekle
                if target_user:
                    if not obj.assigned_users.filter(id=target_user.id).exists():
                        obj.assigned_users.add(target_user)
                        changed = True

                if obj.status == 'new' and status and status != 'new':
                    obj.status = status
                    changed = True

                if changed:
                    obj.last_activity_at = timezone.now()
                    obj.save()
                updated += 1

            else:
                # Yeni Oluşturma
                obj = Lead.objects.create(
                    store=store,
                    created_by=request.user,
                    # assigned_users burada verilmez, save sonrası eklenir
                    full_name=full_name,
                    business_name=business_name,
                    email=email if email else None,
                    phone=phone if phone else None,
                    channel=channel,
                    channel_handle=channel_handle,
                    status=status,
                    category=category,
                    city=city,
                    district=district,
                    score=score,
                    last_activity_at=timezone.now()
                )

                # M2M Ekleme (Yeni Kayıt)
                if target_user:
                    obj.assigned_users.add(target_user)
                # İsterseniz import eden kişiyi de ekleyebilirsiniz:
                # obj.assigned_users.add(request.user)

                created += 1

                if created_dt:
                    try:
                        obj.created_on = created_dt
                        obj.save(update_fields=['created_on'])
                    except Exception:
                        pass

        except Exception:
            skipped += 1

    return JsonResponse({
        'result': True,
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'skipped_no_key': no_key,
        'delimiter': delimiter,
        'columns_seen': list(field_map.values()),
    })


@login_required()
def add_activity(request, lead_id):
    qs = Lead.objects.filter(id=lead_id, store=request.user.store)
    # GÜVENLİK
    if not request.user.is_superuser:
        qs = qs.filter(Q(created_by=request.user) | Q(assigned_users=request.user)).distinct()

    lead = get_object_or_404(qs)

    if request.method == 'POST':
        activity_type = request.POST.get('activity_type')
        outcome = request.POST.get('outcome')
        summary = request.POST.get('summary')

        LeadActivity.objects.create(
            lead=lead,
            created_by=request.user,
            activity_type=activity_type,
            outcome=outcome,
            summary=summary,
            activity_date=timezone.now()
        )

        old_status = lead.status
        new_status = None

        if outcome == 'offer_requested' and lead.status != 'proposal':
            new_status = 'proposal'

        elif outcome == 'offer_decision_pending':
            new_status = 'negotiation'

        elif outcome == 'positive' and lead.status == 'new':
            new_status = 'contacted'
        elif outcome == 'sale_closed':
            new_status = 'won'

        if new_status:
            LeadStageHistory.objects.create(
                lead=lead,
                from_status=old_status,
                to_status=new_status,
                changed_by=request.user,
                note=f"Otomatik geçiş: {summary}"
            )

            lead.status = new_status

        lead.last_activity_at = timezone.now()
        lead.save()

        return JsonResponse({'result': True})


@login_required(login_url='login')
def applications_view(request):
    return render(request, 'management/crm/leads/applications.html', {
        'title': 'Paket Başvuruları',
        'status_choices': PackageApplication.APPLICATION_STATUSES,
    })


@login_required(login_url='login')
def applications_get_all(request):
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 25))
    search_val = request.GET.get('search[value]', '').strip()
    status_filter = request.GET.get('status', '')

    qs = PackageApplication.objects.select_related('lead', 'proposal').prefetch_related('selected_modules')

    if status_filter:
        qs = qs.filter(status=status_filter)

    if search_val:
        qs = qs.filter(
            Q(first_name__icontains=search_val) |
            Q(last_name__icontains=search_val) |
            Q(phone__icontains=search_val) |
            Q(business_name__icontains=search_val) |
            Q(application_no__icontains=search_val)
        )

    total = PackageApplication.objects.count()
    filtered = qs.count()

    order_col = int(request.GET.get('order[0][column]', 0))
    order_dir = request.GET.get('order[0][dir]', 'desc')
    order_map = {
        1: 'application_no',
        2: 'first_name',
        3: 'business_name',
        4: 'phone',
        5: 'monthly_total',
        6: 'created_on',
        7: 'status',
    }
    order_field = order_map.get(order_col, '-created_on')
    if order_dir == 'desc' and not order_field.startswith('-'):
        order_field = '-' + order_field

    records = qs.order_by(order_field)[start:start + length]

    status_badges = {
        'pending': ('Beklemede', 'badge-light-warning'),
        'contacted': ('İletişime Geçildi', 'badge-light-info'),
        'proposal_created': ('Teklif Oluşturuldu', 'badge-light-success'),
        'rejected': ('Reddedildi', 'badge-light-danger'),
    }

    data = []
    for r in records:
        s_label, s_class = status_badges.get(r.status, ('—', 'badge-light'))
        modules = ', '.join(m.name for m in r.selected_modules.all()[:5])
        if r.selected_modules.count() > 5:
            modules += f' +{r.selected_modules.count() - 5}'

        data.append({
            'id': str(r.id),
            'application_no': r.application_no or '—',
            'full_name': f"{r.first_name} {r.last_name}",
            'business_name': r.business_name or '—',
            'phone': r.phone or '—',
            'monthly_total': f"₺{r.monthly_total:,.2f}",
            'yearly_total': f"₺{r.yearly_total:,.2f}",
            'modules_summary': modules,
            'module_count': r.selected_modules.count(),
            'created_on': r.created_on.strftime('%d.%m.%Y %H:%M') if r.created_on else '—',
            'status': s_label,
            'status_code': r.status,
            'status_class': s_class,
            'has_proposal': r.proposal_id is not None,
            'proposal_id': str(r.proposal_id) if r.proposal_id else None,
            'utm_source': r.utm_source or '—',
        })

    return JsonResponse({
        'draw': draw,
        'recordsTotal': total,
        'recordsFiltered': filtered,
        'data': data,
    })


@login_required(login_url='login')
def application_detail_view(request, pk):
    app = get_object_or_404(
        PackageApplication.objects.select_related('lead', 'proposal').prefetch_related('selected_modules'),
        id=pk
    )

    selected = list(app.selected_modules.all())

    license_total = sum(m.license_price for m in selected)
    modules_monthly_total = sum(m.price_monthly for m in selected)
    modules_yearly_total = sum(m.price_yearly for m in selected)
    maintenance_license = (license_total * Decimal('0.10')).quantize(Decimal('0.01'))
    maintenance_monthly = (modules_monthly_total * Decimal('0.10')).quantize(Decimal('0.01'))
    maintenance_yearly = (modules_yearly_total * Decimal('0.10')).quantize(Decimal('0.01'))

    return render(request, 'management/crm/leads/application_detail.html', {
        'title': f'Başvuru Detay — {app.application_no}',
        'app': app,
        'status_choices': PackageApplication.APPLICATION_STATUSES,
        'license_total': license_total,
        'modules_monthly_total': modules_monthly_total,
        'modules_yearly_total': modules_yearly_total,
        'maintenance_license': maintenance_license,
        'maintenance_monthly': maintenance_monthly,
        'maintenance_yearly': maintenance_yearly,
    })


@login_required(login_url='login')
@require_POST
def application_update_status(request):
    try:
        app_id = request.POST.get('id')
        new_status = request.POST.get('status')
        app = get_object_or_404(PackageApplication, id=app_id)
        app.status = new_status
        app.save(update_fields=['status'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


@login_required(login_url='login')
@require_POST
def application_convert_to_proposal(request):
    try:
        app_id = request.POST.get('application_id')
        app = get_object_or_404(
            PackageApplication.objects.prefetch_related('selected_modules'),
            id=app_id
        )

        if app.proposal_id:
            return JsonResponse({
                'result': False,
                'error_msg': 'Bu başvurudan zaten bir teklif oluşturulmuş.',
                'proposal_id': str(app.proposal_id),
            })

        modules = list(app.selected_modules.all())
        currencies_used = set(m.currency for m in modules if m.license_price > 0)
        proposal_currency = currencies_used.pop() if len(currencies_used) == 1 else 'TRY'

        with transaction.atomic():
            proposal = Proposals.objects.create(
                lead=app.lead,
                created_by=request.user,
                title=f"{app.business_name} — Özel Paket Teklifi",
                status='draft',
                currency=proposal_currency,
                notes=f"Başvuru No: {app.application_no}\n"
                      f"Kaynak: {app.utm_source or 'Website'}\n"
                      f"Müşteri Notu: {app.notes or '—'}",
            )

            license_total = Decimal('0.00')
            for module in modules:
                unit = module.license_price if module.license_price else module.price_monthly
                ProposalItems.objects.create(
                    proposal=proposal,
                    module=module,
                    description=f"{module.name} — Lisans Bedeli",
                    quantity=1,
                    unit_price=unit,
                )
                license_total += unit

            maintenance_fee = (license_total * Decimal('0.10')).quantize(Decimal('0.01'))
            if maintenance_fee > Decimal('0.00'):
                ProposalItems.objects.create(
                    proposal=proposal,
                    description="Yıllık Bakım ve Hizmet Bedeli (%10)",
                    quantity=1,
                    unit_price=maintenance_fee,
                    maintenance_included=True,
                )

            currency_sym = {'TRY': '₺', 'USD': '$', 'EUR': '€'}.get(proposal_currency, proposal_currency)
            ProposalLogs.objects.create(
                proposal=proposal,
                user=request.user,
                action='Oluşturma',
                description=f"Paket başvurusundan ({app.application_no}) otomatik oluşturuldu. "
                            f"Lisans toplamı: {currency_sym}{license_total}, "
                            f"Bakım bedeli (%10): {currency_sym}{maintenance_fee}"
            )

            app.proposal = proposal
            app.status = 'proposal_created'
            app.save(update_fields=['proposal', 'status'])

            if app.lead:
                app.lead.status = 'proposal'
                app.lead.save(update_fields=['status'])

        return JsonResponse({
            'result': True,
            'proposal_id': str(proposal.id),
            'proposal_no': proposal.proposal_no,
            'redirect_url': f"/proposals/add?id={proposal.id}",
        })

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)
