# apps/definitions/packages/views.py
from __future__ import annotations

import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods

from apps.activity_logs.views import write_log
from apps.crm.packages.forms import SaaSModuleForm
from apps.crm.packages.models import Packages, PackagePermissionMatrix, SaaSModule, PackageModule
from apps.crm.packages.services import sync_package_permissions_from_modules
from apps.roles.models import Permission


# -------------------- Yardımcılar --------------------
def _to_decimal(val) -> Decimal | None:
    if val is None:
        return None
    s = str(val).strip().replace(' ', '').replace('\xa0', '')
    if s == '':
        return None
    if ',' in s and '.' in s:
        last_comma, last_dot = s.rfind(','), s.rfind('.')
        if last_comma > last_dot:
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _to_int(val, default=None) -> int | None:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def _bool_from_post(val) -> bool:
    return str(val).lower() in ('1', 'true', 'on', 'yes')


def _id_set(post, base_name: str) -> set[str]:
    return set(post.getlist(f"{base_name}[]") or post.getlist(base_name) or [])


# -------------------- Liste (Index) --------------------
@login_required()
def packages_index(request):
    write_log(request, "Paketler", "Paketler sayfası görüntülendi.")
    return render(request, "management/crm/packages/index.html", {"title": "Paketler"})


package_view = packages_index


# -------------------- DataTables Kaynağı --------------------
@login_required()
def get_all(request):
    draw = _to_int(request.GET.get("draw"), 1)
    length = _to_int(request.GET.get("length"), 10)
    start = _to_int(request.GET.get("start"), 0)
    search_value = (request.GET.get("search[value]", "") or "").strip()
    order_idx = request.GET.get("order[0][column]", "0")
    order_dir = request.GET.get("order[0][dir]", "asc")

    columns = {
        "0": "id",
        "1": "name",
        "2": "currency",
        "3": "price_license",
        "4": "maintenance_percent",
        "5": "maintenance_percent",
        "6": "order",
        "7": "is_recommended",
        "8": "is_active",
    }

    order_field = columns.get(order_idx, "order")
    if order_dir == "desc":
        order_field = f"-{order_field}"

    qs = Packages.objects.all()
    total = qs.count()

    if search_value:
        qs = qs.filter(Q(code__icontains=search_value) | Q(name__icontains=search_value))

    count = qs.count()
    page = qs.order_by(order_field)
    if str(length) != "-1":
        page = page[start:start + length]

    data = []
    for p in page:
        m_amount = p.maintenance_amount

        data.append({
            "id": str(p.id),
            "code": p.code,
            "name": p.name,
            "currency": p.currency,
            "price_license": f"{p.price_license:,.2f}",
            "maintenance_percent": f"%{p.maintenance_percent:,.2f}",
            "maintenance_amount": f"{m_amount:,.2f} {p.currency_symbol}",
            "order": p.order,
            "is_recommended": p.is_recommended,
            "badge_text": p.badge_text or "",
            "is_active": p.is_active,
        })

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": count,
        "recordsTotal": total,
        "data": data
    })


# -------------------- Ekle (Sadece İsim) --------------------
@login_required()
@require_http_methods(["POST"])
def add_package(request):
    """
    Sadece Paket Adı ile hızlı kayıt oluşturur.
    Diğer detaylar 'package_detail' sayfasında girilir.
    """
    data = request.POST
    name = (data.get("name") or "").strip()

    if not name:
        return JsonResponse({"error": True, "error_msg": "Paket Adı zorunludur."})

    # Code (slug) üretimi
    code = slugify(name)
    if not code:
        code = f"pkg-{uuid.uuid4().hex[:8]}"

    # Unique Code kontrolü (Basit)
    original_code = code
    counter = 1
    while Packages.objects.filter(code=code).exists():
        code = f"{original_code}-{counter}"
        counter += 1

    try:
        # Varsayılan değerlerle oluştur
        pkg = Packages.objects.create(
            name=name,
            code=code,
            currency="USD",
            price_license=Decimal('0.00'),
            maintenance_percent=Decimal('15.00')
        )
        write_log(request, "Paketler", f"Paket Eklendi (Hızlı). ID={pkg.id}")

        # Başarılı olunca Detay sayfasına yönlendirme URL'i dönüyoruz
        return JsonResponse({
            "result": True,
            "redirect_url": reverse("packages:detail", args=[pkg.id])
        })
    except Exception as e:
        return JsonResponse({"error": True, "error_msg": str(e)})


# -------------------- Sil / Durum Değiştir --------------------
@login_required()
@require_http_methods(["POST"])
def delete(request):
    ids = request.POST.getlist("ids[]")
    try:
        Packages.objects.filter(id__in=ids).delete()
        return JsonResponse({"result": True})
    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})


@login_required()
@require_http_methods(["POST"])
def change_status(request):
    ids = request.POST.getlist("ids[]")
    try:
        for pkg in Packages.objects.filter(id__in=ids):
            pkg.is_active = not pkg.is_active
            pkg.save(update_fields=["is_active"])
        return JsonResponse({"result": True})
    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})


# -------------------- Detay (Form + Permission + Modül) --------------------
@transaction.atomic
@login_required()
def package_detail(request, pk=None):
    # Bu view hem düzenleme hem de detay görüntüleme işini yapar.
    # pk zorunludur çünkü create işlemi index'teki modalda yapılır.
    pkg = get_object_or_404(Packages, pk=pk)

    if request.method == "POST":
        pkg.name = request.POST.get("name") or pkg.name
        pkg.code = request.POST.get("code") or pkg.code
        pkg.currency = request.POST.get("currency") or "TRY"

        # Sayısal Alanlar
        pkg.price_license = _to_decimal(request.POST.get("price_license")) or Decimal('0.00')
        pkg.maintenance_percent = _to_decimal(request.POST.get("maintenance_percent")) or Decimal('15.00')

        pkg.order = _to_int(request.POST.get("order"), 100)
        pkg.is_active = _bool_from_post(request.POST.get("is_active"))
        pkg.is_recommended = _bool_from_post(request.POST.get("is_recommended"))
        pkg.badge_text = request.POST.get("badge_text") or ""

        try:
            pkg.save()
        except Exception as e:
            messages.error(request, f"Hata: {str(e)}")
            return redirect("packages:detail", pk=pkg.pk)

        # ──────── Modül Seçimi (Faz 12.2) ────────
        wanted_module_ids = _id_set(request.POST, "module_ids")

        # Mevcut modülleri çek
        existing_module_id_strs = {
            str(mid) for mid in
            PackageModule.objects.filter(package=pkg)
            .values_list('module_id', flat=True)
        }

        # Yeni eklenecek modüller
        to_add = wanted_module_ids - existing_module_id_strs
        for mid in to_add:
            try:
                PackageModule.objects.create(package=pkg, module_id=mid)
            except Exception:
                pass  # Geçersiz UUID veya duplicate — sessizce atla

        # Kaldırılacak modüller (çekirdek modüller hariç)
        to_remove = existing_module_id_strs - wanted_module_ids
        if to_remove:
            PackageModule.objects.filter(
                package=pkg,
                module_id__in=to_remove,
                module__is_core=False,
            ).delete()

        # Modül değişikliğinden sonra permission matrix'i senkronize et
        sync_package_permissions_from_modules(pkg)

        # ──────── Elle Permission Seçimi (Mevcut) ────────
        wanted_perm_ids = _id_set(request.POST, "permission_ids")
        existing = {str(x.permission_id): x for x in PackagePermissionMatrix.objects.filter(package=pkg)}

        # Ekle / Aktif Et (sadece elle atanan)
        for pid in wanted_perm_ids:
            if pid in existing:
                row = existing[pid]
                if not row.available:
                    row.available = True
                    row.save(update_fields=['available'])
                # Elle seçildiyse source'u manual'e çek
                if row.source != 'manual':
                    row.source = 'manual'
                    row.save(update_fields=['source'])
            else:
                PackagePermissionMatrix.objects.create(
                    package=pkg, permission_id=pid,
                    available=True, source='manual'
                )

        # Pasif Et (elle atanmış olup artık seçili olmayanlar)
        # NOT: Modül kaynaklı yetkilere dokunma
        to_disable = set(existing.keys()) - wanted_perm_ids
        if to_disable:
            PackagePermissionMatrix.objects.filter(
                package=pkg,
                permission_id__in=to_disable,
                source='manual',
            ).update(available=False)

        messages.success(request, "Paket başarıyla güncellendi.")
        return redirect("packages:detail", pk=pkg.pk)

    # ──────── GET Request ────────
    all_perms = Permission.objects.all().order_by("group", "name", "code")
    checked_perm_ids = set(
        PackagePermissionMatrix.objects.filter(package=pkg, available=True)
        .values_list("permission_id", flat=True)
    )

    # Modül kaynaklı permission id'leri (template'de farklı göstermek için)
    module_sourced_perm_ids = set(
        PackagePermissionMatrix.objects.filter(package=pkg, available=True, source='module')
        .values_list("permission_id", flat=True)
    )

    # Tüm aktif modüller (seçim için)
    all_modules = SaaSModule.objects.filter(is_active=True).order_by('order', 'name')

    # Bu pakete atanmış modül id'leri
    checked_module_ids = set(
        PackageModule.objects.filter(package=pkg).values_list('module_id', flat=True)
    )

    # Çekirdek modül id'leri (her zaman seçili ve disabled)
    core_module_ids = set(
        SaaSModule.objects.filter(is_core=True, is_active=True).values_list('id', flat=True)
    )

    return render(request, "management/crm/packages/detail.html", {
        "pkg": pkg,
        "all_perms": all_perms,
        "checked_perm_ids": checked_perm_ids,
        "module_sourced_perm_ids": module_sourced_perm_ids,
        "all_modules": all_modules,
        "checked_module_ids": checked_module_ids,
        "core_module_ids": core_module_ids,
    })


# ============================================================
#  SaaS Modül Yönetimi (CRUD)
# ============================================================

@login_required()
def saas_module_index(request):
    write_log(request, "SaaS Modüller", "SaaS Modüller sayfası görüntülendi.")
    return render(request, "management/crm/packages/modules_index.html", {
        "title": "SaaS Modüller",
    })


@login_required()
def saas_module_get_all(request):
    draw = _to_int(request.GET.get("draw"), 1)
    length = _to_int(request.GET.get("length"), 25)
    start = _to_int(request.GET.get("start"), 0)
    search_value = (request.GET.get("search[value]", "") or "").strip()
    order_idx = request.GET.get("order[0][column]", "0")
    order_dir = request.GET.get("order[0][dir]", "asc")

    columns = {
        "0": "id",
        "1": "name",
        "2": "price_monthly",
        "3": "price_yearly",
        "4": "is_core",
        "5": "order",
        "6": "is_active",
    }

    order_field = columns.get(order_idx, "order")
    if order_dir == "desc":
        order_field = f"-{order_field}"

    qs = SaaSModule.objects.annotate(
        dep_count=Count('dependencies'),
        perm_count=Count('permissions'),
    )
    total = qs.count()

    if search_value:
        qs = qs.filter(Q(name__icontains=search_value) | Q(slug__icontains=search_value))

    filtered = qs.count()
    page = qs.order_by(order_field)
    if str(length) != "-1":
        page = page[start:start + length]

    data = []
    cur_map = {'TRY': '₺', 'USD': '$', 'EUR': '€'}
    for m in page:
        sym = cur_map.get(m.currency, m.currency)
        data.append({
            "id": str(m.id),
            "name": m.name,
            "slug": m.slug,
            "icon": m.icon or "fa-solid fa-cube",
            "description": (m.description or "")[:80],
            "license_price": f"{sym}{m.license_price:,.2f}",
            "currency": m.currency,
            "price_monthly": f"₺{m.price_monthly:,.2f}",
            "price_yearly": f"₺{m.price_yearly:,.2f}",
            "is_core": m.is_core,
            "is_active": m.is_active,
            "order": m.order,
            "dep_count": m.dep_count,
            "perm_count": m.perm_count,
        })

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": filtered,
        "recordsTotal": total,
        "data": data,
    })


def _grouped_permissions(checked_ids=None):
    """Permission kayıtlarını group alanına göre gruplayıp döndürür."""
    if checked_ids is None:
        checked_ids = set()
    all_perms = Permission.objects.all().order_by("group", "order", "code")
    groups = {}
    for p in all_perms:
        g = p.group or "Genel"
        groups.setdefault(g, []).append(p)
    return [
        {"name": g, "perms": perms}
        for g, perms in groups.items()
    ], checked_ids


@login_required()
def saas_module_create(request):
    if request.method == "POST":
        form = SaaSModuleForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                module = form.save()
                perm_ids = _id_set(request.POST, "permission_ids")
                if perm_ids:
                    module.permissions.set(Permission.objects.filter(id__in=perm_ids))
            write_log(request, "SaaS Modüller", f"Modül oluşturuldu: {module.name}")
            messages.success(request, f'"{module.name}" modülü başarıyla oluşturuldu.')
            return redirect("packages:module-update", pk=module.pk)
        else:
            messages.error(request, "Lütfen formdaki hataları düzeltin.")
    else:
        form = SaaSModuleForm()

    perm_groups, checked_perm_ids = _grouped_permissions()

    return render(request, "management/crm/packages/module_form.html", {
        "title": "Yeni SaaS Modül",
        "form": form,
        "is_edit": False,
        "perm_groups": perm_groups,
        "checked_perm_ids": checked_perm_ids,
    })


@login_required()
def saas_module_update(request, pk):
    module = get_object_or_404(SaaSModule, pk=pk)

    if request.method == "POST":
        form = SaaSModuleForm(request.POST, instance=module)
        if form.is_valid():
            with transaction.atomic():
                form.save()
                perm_ids = _id_set(request.POST, "permission_ids")
                module.permissions.set(Permission.objects.filter(id__in=perm_ids))

                # Modülün permission'ları değiştiyse, bu modülü içeren
                # tüm paketlerin permission matrix'ini senkronize et
                affected_packages = Packages.objects.filter(
                    package_modules__module=module
                ).distinct()
                for affected_pkg in affected_packages:
                    sync_package_permissions_from_modules(affected_pkg)

            write_log(request, "SaaS Modüller", f"Modül güncellendi: {module.name}")
            messages.success(request, "Modül başarıyla güncellendi.")
            return redirect("packages:module-update", pk=module.pk)
        else:
            messages.error(request, "Lütfen formdaki hataları düzeltin.")
    else:
        form = SaaSModuleForm(instance=module)

    checked_perm_ids = set(module.permissions.values_list("id", flat=True))
    perm_groups, checked_perm_ids = _grouped_permissions(checked_perm_ids)

    return render(request, "management/crm/packages/module_form.html", {
        "title": f"{module.name} — Düzenle",
        "form": form,
        "module": module,
        "is_edit": True,
        "perm_groups": perm_groups,
        "checked_perm_ids": checked_perm_ids,
    })


@login_required()
@require_http_methods(["POST"])
def saas_module_delete(request):
    ids = request.POST.getlist("ids[]")
    try:
        deleted = SaaSModule.objects.filter(id__in=ids).update(is_active=False)
        return JsonResponse({"result": True, "deactivated": deleted})
    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})


@login_required()
@require_http_methods(["POST"])
def saas_module_change_status(request):
    ids = request.POST.getlist("ids[]")
    try:
        for m in SaaSModule.objects.filter(id__in=ids):
            m.is_active = not m.is_active
            m.save(update_fields=["is_active"])
        return JsonResponse({"result": True})
    except Exception as e:
        return JsonResponse({"result": False, "error": True, "error_msg": str(e)})
