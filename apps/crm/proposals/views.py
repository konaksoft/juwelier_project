from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_http_methods

from apps.crm.proposals.models import *
from apps.helpers.numbers import parse_decimal_locale
from apps.orders.models import *
from apps.crm.packages.models import *
from apps.roles.decorators import role_required


# ----------------- yardımcılar -----------------

def d_quantize(val: Decimal, places: int) -> Decimal:
    if val is None:
        val = Decimal('0')
    q = Decimal('1').scaleb(-places) if places > 0 else Decimal('1')
    return Decimal(val).quantize(q, rounding=ROUND_HALF_UP)


def create_proposal_log(proposal, user, action, description=""):
    """Teklif işlemleri için log oluşturur"""
    try:
        ProposalLogs.objects.create(
            proposal=proposal,
            user=user,
            action=action,
            description=description
        )
    except Exception as e:
        print(f"Log oluşturma hatası: {e}")


@login_required(login_url='login')
def index(request):
    """Teklifler listesi sayfası"""
    return render(request, 'management/crm/proposals/index.html', {
        'title': 'Teklif Yönetimi',
        'status_choices': Proposals.STATUS_CHOICES
    })


# Mevcut get_all fonksiyonunu bu şekilde güncelleyin:

@login_required(login_url='login')
@require_http_methods(["GET"])
def get_all(request):
    """DataTable için JSON listesi döndürür (Sekmeli Yapı Destekli)"""
    try:
        draw = int(request.GET.get('draw', 1))
        length = int(request.GET.get('length', 10))
        start = int(request.GET.get('start', 0))
        search_value = (request.GET.get('search[value]', '') or '').strip()
        order_column_index = request.GET.get('order[0][column]', '0')
        order_dir = request.GET.get('order[0][dir]', 'desc')

        # --- YENİ EKLENEN KISIM: Tab Filtresi ---
        scope = request.GET.get('scope', 'draft')  # Varsayılan: draft

        qs = Proposals.objects.filter(is_deleted=False).select_related('lead', 'company', 'created_by')

        # Sekme Mantığı
        if scope == 'draft':
            qs = qs.filter(status='draft')
        elif scope == 'accepted':
            qs = qs.filter(status='accepted')
        elif scope == 'others':
            # Taslak ve Onaylanan dışındaki her şey (Gönderildi, Reddedildi, Revize)
            qs = qs.exclude(status__in=['draft', 'accepted'])

        # ----------------------------------------

        # Arama (Mevcut kod)
        if search_value:
            qs = qs.filter(
                Q(proposal_no__icontains=search_value) |
                Q(title__icontains=search_value) |
                Q(lead__full_name__icontains=search_value) |
                Q(company__title__icontains=search_value)
            )

        total_records = qs.count()
        filtered_records = qs.count()

        # Sıralama (Mevcut kod)
        columns_map = {
            '0': 'proposal_no',
            '1': 'title',
            '2': 'lead__full_name',
            '3': 'date',
            '4': 'status',
            '5': 'grand_total',
            '6': 'created_at',
        }

        order_field = columns_map.get(str(order_column_index), 'created_at')

        if order_field not in ['grand_total']:
            if order_dir == 'desc':
                order_field = '-' + order_field
            qs = qs.order_by(order_field)
        else:
            qs = qs.order_by('-created_at')

        if length != -1:
            qs = qs[start:start + length]

        data = []
        for p in qs:
            client_name = "-"
            if p.lead:
                client_name = f"{p.lead.full_name} (Lead)"
            elif p.company:
                client_name = p.company.title

            data.append({
                'id': str(p.id),
                'proposal_no': p.proposal_no,
                'title': p.title or "-",
                'client': client_name,
                'date': p.date.strftime('%d.%m.%Y') if p.date else "-",
                'status': p.get_status_display(),
                'status_code': p.status,
                'grand_total': f"{p.grand_total:,.2f} {p.currency}",
                'created_at': p.created_at.strftime('%d.%m.%Y %H:%M'),
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
@transaction.atomic
def add(request):
    """Teklif Ekleme ve Güncelleme İşlemi"""
    if request.method == 'POST':
        try:
            proposal_id = request.POST.get('proposal_id')

            # Form verilerini al
            lead_id = request.POST.get('lead_id')
            company_id = request.POST.get('company_id')
            title = request.POST.get('title')
            date = request.POST.get('date')
            valid_until = request.POST.get('valid_until')
            status = request.POST.get('status', 'draft')
            currency = request.POST.get('currency', 'USD')
            notes = request.POST.get('notes')

            discount_amount = parse_decimal_locale(request.POST.get('discount_amount'), default="0")
            tax_rate = parse_decimal_locale(request.POST.get('tax_rate'), default="20")

            defaults = {
                'lead_id': lead_id if lead_id else None,
                'company_id': company_id if company_id else None,
                'title': title,
                'date': date if date else None,
                'valid_until': valid_until if valid_until else None,
                'status': status,
                'currency': currency,
                'notes': notes,
                'discount_amount': discount_amount,
                'tax_rate': tax_rate,
            }

            if proposal_id:
                # GÜNCELLEME
                proposal = get_object_or_404(Proposals, id=proposal_id)
                for k, v in defaults.items():
                    setattr(proposal, k, v)
                proposal.save()

                create_proposal_log(proposal, request.user, "Güncelleme", "Teklif bilgileri güncellendi.")
            else:
                # YENİ KAYIT
                defaults['created_by'] = request.user
                proposal = Proposals.objects.create(**defaults)
                create_proposal_log(proposal, request.user, "Oluşturma", "Yeni teklif oluşturuldu.")

            # --- KALEMLER (ITEMS) ---
            if proposal_id:
                proposal.items.all().delete()

            # Formdan gelen listeler
            store_names = request.POST.getlist('item_store_name[]')
            descriptions = request.POST.getlist('item_description[]')
            quantities = request.POST.getlist('item_quantity[]')
            prices = request.POST.getlist('item_price[]')
            packages = request.POST.getlist('item_package[]')
            devices = request.POST.getlist('item_device[]')
            modules = request.POST.getlist('item_module[]')

            for i, desc in enumerate(descriptions):
                if not desc.strip(): continue

                qty = parse_decimal_locale(quantities[i]) if i < len(quantities) else 1
                price = parse_decimal_locale(prices[i]) if i < len(prices) else 0
                pkg_id = packages[i] if i < len(packages) and packages[i] else None
                dev_id = devices[i] if i < len(devices) and devices[i] else None
                mod_id = modules[i] if i < len(modules) and modules[i] else None

                # Mağaza adını al (liste uzunluğunu kontrol ederek)
                s_name = store_names[i] if i < len(store_names) else None

                is_maint = False

                ProposalItems.objects.create(
                    proposal=proposal,
                    package_id=pkg_id,
                    module_id=mod_id,
                    device_id=dev_id,
                    store_name=s_name,
                    description=desc,
                    quantity=int(qty),
                    unit_price=price,
                    maintenance_included=is_maint
                )

            return JsonResponse({'result': True, 'id': proposal.id})

        except Exception as e:
            return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)

    # GET İsteği
    lead_id = request.GET.get('lead_id')
    initial_lead = None
    if lead_id:
        initial_lead = Lead.objects.filter(id=lead_id).first()

    proposal_id = request.GET.get('id')
    proposal = None
    if proposal_id:
        proposal = get_object_or_404(Proposals, id=proposal_id)

    leads = Lead.objects.filter(is_active=True)
    companies = Company.objects.filter(is_active=True)
    packages = Packages.objects.filter(is_active=True)
    devices = Device.objects.filter(is_active=True)
    all_modules = SaaSModule.objects.filter(is_active=True).order_by('order', 'name')

    return render(request, 'management/crm/proposals/forms.html', {
        'title': 'Teklif Düzenle' if proposal else 'Yeni Teklif',
        'proposal': proposal,
        'leads': leads,
        'companies': companies,
        'packages': packages,
        'devices': devices,
        'all_modules': all_modules,
        'initial_lead': initial_lead,
        'status_choices': Proposals.STATUS_CHOICES,
        'currency_choices': Proposals.CURRENCY_CHOICES,
    })


@login_required(login_url='login')
@require_http_methods(["POST"])
@transaction.atomic
def delete(request):
    """
    Sadece 'Taslak' (draft) durumundaki teklifleri siler.
    Onaylanmış veya gönderilmiş teklifler silinemez.
    """
    ids = request.POST.getlist('ids[]') or []
    try:
        # Önce silinmek istenenlerde taslak olmayan var mı kontrol et
        non_draft_count = Proposals.objects.filter(id__in=ids).exclude(status='draft').count()

        if non_draft_count > 0:
            return JsonResponse({
                'result': False,
                'error': True,
                'error_msg': 'Sadece TASLAK durumundaki teklifler silinebilir. Onaylanmış veya Gönderilmiş teklifleri silemezsiniz.'
            })

        # Sadece draft olanları sil (soft delete)
        Proposals.objects.filter(id__in=ids, status='draft').update(is_deleted=True, is_active=False)
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


@login_required(login_url='login')
@require_http_methods(["POST"])
def update_status(request):
    """
    Index sayfasından durum güncellemek için.

    Faz 12.7 (Faz D): Teklif 'accepted' yapıldığında otomatik olarak
    pasif bir mağaza oluşturulur ve teklifin modül kalemleri atanır.
    Mağaza oluşturulduktan sonra sipariş (Order) de oluşturulur.
    """
    try:
        proposal_id = request.POST.get('id')
        new_status = request.POST.get('status')

        proposal = get_object_or_404(Proposals, id=proposal_id)
        old_status_display = proposal.get_status_display()

        store_created = False
        store_info = None

        # Eğer durum 'accepted' (onaylandı) yapılıyorsa
        if new_status == 'accepted' and proposal.status != 'accepted':

            # ── 1. Otomatik Mağaza Oluşturma (Faz D) ──
            try:
                from apps.stores.services import auto_create_store_from_proposal
                store = auto_create_store_from_proposal(proposal)
                if store:
                    store_created = True
                    store_info = {
                        'id': str(store.id),
                        'title': store.title,
                        'is_active': store.is_active,
                    }
            except Exception as e:
                # Mağaza oluşturma hatası teklif onayını engellemesin
                print(f"[Faz D] Otomatik mağaza oluşturma hatası: {e}")

            # ── 2. Sipariş Oluşturma ──
            try:
                create_order_from_proposal(proposal, requester=request.user)
            except ValueError as ve:
                # Sipariş oluşturulamazsa devam et — firma/mağaza eksik olabilir
                print(f"[update_status] Sipariş oluşturulamadı: {ve}")
            except Exception as e:
                print(f"[update_status] Sipariş hatası: {e}")

        proposal.status = new_status
        proposal.save(update_fields=['status'])
        new_status_display = proposal.get_status_display()
        description = f"Durum '{old_status_display}' -> '{new_status_display}' olarak değiştirildi."
        if store_created:
            description += f" Otomatik mağaza oluşturuldu: {store_info['title']} (Pasif)"
        create_proposal_log(proposal, request.user, "Durum Değişikliği", description)

        response_data = {'result': True}
        if store_created:
            response_data['store_created'] = True
            response_data['store'] = store_info
        return JsonResponse(response_data)
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)})


@login_required(login_url='login')
@transaction.atomic
def change_status(request):
    """Aktif/Pasif durumunu değiştir (Liste butonu için)"""
    ids = request.POST.getlist('ids[]') or []
    try:
        rows = Proposals.objects.filter(id__in=ids)
        for r in rows:
            r.is_active = not r.is_active
            r.save(update_fields=['is_active'])
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'result': False, 'error': True, 'error_msg': str(e)}, status=500)


@login_required(login_url='login')
def detail(request, pk):
    """Teklif Önizleme / Detay Sayfası"""
    proposal = get_object_or_404(Proposals, pk=pk)
    return render(request, 'management/crm/proposals/preview.html', {
        'proposal': proposal
    })


@login_required(login_url='login')
def get_package_info(request):
    """AJAX: Paket fiyatını getir"""
    pkg_id = request.GET.get('id')
    try:
        pkg = Packages.objects.get(id=pkg_id)
        return JsonResponse({
            'result': True,
            'price': str(pkg.price_license),
            'name': pkg.name,
            'maintenance': str(pkg.maintenance_percent)
        })
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})


@login_required(login_url='login')
def get_device_info(request):
    """AJAX: Cihaz fiyatını getir"""
    dev_id = request.GET.get('id')
    try:
        dev = Device.objects.get(id=dev_id)
        return JsonResponse({
            'result': True,
            'price': str(dev.price),
            'name': dev.name,
            'currency': dev.currency
        })
    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)})


@login_required(login_url='login')
def package_details(request, pk):
    """
    Belirli bir teklif için Modül Bazlı Paket Karşılaştırma ve Detay Tablosu (Sözleşme Eki).
    Her SaaS modülü başlık satırı olarak gösterilir, altında o modülün
    Permission.group='Dashboard' yetkilerini listeler.
    (Faz 12.6 — Modül Bazlı Kapsam Tablosu)
    """
    proposal = get_object_or_404(Proposals, pk=pk)

    from apps.crm.packages.services import build_module_scope_table
    packages, module_rows = build_module_scope_table()

    return render(request, 'management/crm/proposals/package_details.html', {
        'proposal': proposal,
        'packages': packages,
        'module_rows': module_rows,
    })


@login_required(login_url='login')
def history(request, pk):
    """Teklifin tarihçesini gösteren sayfa"""
    proposal = get_object_or_404(Proposals, pk=pk)
    logs = proposal.logs.all().select_related('user').order_by('-created_at')

    return render(request, 'management/crm/proposals/history.html', {
        'proposal': proposal,
        'logs': logs
    })
