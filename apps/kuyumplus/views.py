# apps/website/views_pricing.py
import json
from decimal import Decimal, InvalidOperation
from typing import List, Set, Tuple

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.testimonials.models import *
from apps.crm.packages.models import Packages, PackagePermissionMatrix, SaaSModule
from apps.crm.leads.models import Lead, PackageApplication
from apps.roles.models import Permission


def _to_decimal_safe(val):
    if val is None:
        return None
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None

def _fmt_money(val, symbol: str) -> str:
    """None → '—', sayı → '₺123.4' (gereksiz .00 yok)"""
    q = _to_decimal_safe(val)
    if q is None:
        return "—"
    q = q.quantize(Decimal("0.01"))
    s = f"{q:.2f}".rstrip("0").rstrip(".")
    return f"{symbol} {s}"

def pricing_view(request):
    # Aktif paketler + sadece dashboard izinleri
    packages: List[Packages] = list(
        Packages.objects.filter(is_active=True).order_by("order", "name")
    )
    perms: List[Permission] = list(
        Permission.objects.filter(group__iexact="dashboard", order__isnull=False).order_by('order')
    )

    # (perm_id, pkg_id) lookup
    allowed: Set[Tuple[str, str]] = set(
        PackagePermissionMatrix.objects
        .filter(available=True, package__in=packages, permission__in=perms)
        .values_list("permission_id", "package_id")
    )

    # Paketlere ham ve gösterim alanlarını ekle
    for p in packages:
        p.month_raw =1
        p.year_raw = 12        # model metodunuz
        p.month_display = _fmt_money(p.month_raw, p.currency_symbol)
        p.year_display  = _fmt_money(p.year_raw,  p.currency_symbol)

    # Satırlar
    feature_rows = []
    for perm in perms:
        feature_rows.append({
            "feature": perm,
            "cells": [{"package": pkg, "available": (perm.id, pkg.id) in allowed} for pkg in packages],
        })

    # Baz plan
    base_code = next((p.code for p in packages if p.is_recommended),
                     (packages[-1].code if packages else ""))

    return render(request, "theme/pricing.html", {
        "packages": packages,
        "feature_rows": feature_rows,
        "base_code": base_code,
        "min_term_global": min((1 for p in packages), default=3),
    })


def index_view(request):
    testimonials = Testimonial.objects.filter(is_active=True).order_by('order')
    context = {
        'title': 'Kuyum Plus',
        'testimonials': testimonials,  # Veriyi şablona gönderiyoruz
    }
    return render(request, 'theme/index.html', context)


def inventory_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/inventory_detail.html', context)


def cost_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/cost_detail.html', context)


def retail_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/retail_detail.html', context)


def barcode_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/barcode_detail.html', context)


def wholesale_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/wholesale_detail.html', context)


def current_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/current_detail.html', context)


def contact_page(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/contact.html', context)


def reference_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/reference.html', context)


def about_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/about.html', context)


def report_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/report.html', context)


def education_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/education.html', context)


def order_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/order.html', context)


def support_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/support.html', context)


def stock_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/stock.html', context)


def kvkk_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/kvkk.html', context)


def kullanimkosullari_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/kullanimkosullari.html', context)


def referanslar_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/reference.html', context)

def efatura_view(request):
    context = {
        'title': 'Kuyum Plus',
    }
    return render(request, 'theme/efatura_entegrasyonu.html', context)

def devices_view(request):
    context = {
        'title': 'Kuyum Plus | Uyumlu Cihazlar',
    }
    return render(request, 'theme/devices.html', context)


def basvuru_view(request):
    modules = SaaSModule.objects.filter(is_active=True).prefetch_related('dependencies').order_by('order', 'name')

    dependency_map = {}
    modules_json = []
    for m in modules:
        dep_ids = [str(d.id) for d in m.dependencies.all()]
        dependency_map[str(m.id)] = dep_ids
        modules_json.append({
            'id': str(m.id),
            'name': m.name,
            'slug': m.slug,
            'description': m.description,
            'icon': m.icon,
            'is_core': m.is_core,
            'dependencies': dep_ids,
        })

    utm = request.GET.get('utm_source', '')

    return render(request, 'theme/basvuru.html', {
        'title': 'Özel Paket Başvurusu | Kuyum Plus',
        'modules': modules,
        'modules_json': json.dumps(modules_json, ensure_ascii=False),
        'dependency_map_json': json.dumps(dependency_map, ensure_ascii=False),
        'utm_source': utm,
    })


@require_http_methods(["POST"])
def basvuru_submit(request):
    try:
        first_name = (request.POST.get('first_name') or '').strip()
        last_name = (request.POST.get('last_name') or '').strip()
        phone = (request.POST.get('phone') or '').strip()
        email = (request.POST.get('email') or '').strip()
        business_name = (request.POST.get('business_name') or '').strip()
        city = (request.POST.get('city') or '').strip()
        utm_source = (request.POST.get('utm_source') or '').strip()
        notes = (request.POST.get('notes') or '').strip()
        module_ids = request.POST.getlist('modules[]')

        if not first_name or not last_name or not phone or not business_name:
            return JsonResponse({
                'result': False,
                'error_msg': 'Ad, Soyad, Telefon ve Firma Adı zorunludur.'
            }, status=400)

        if not module_ids:
            return JsonResponse({
                'result': False,
                'error_msg': 'En az bir modül seçmelisiniz.'
            }, status=400)

        selected_modules = SaaSModule.objects.filter(id__in=module_ids, is_active=True)
        if not selected_modules.exists():
            return JsonResponse({
                'result': False,
                'error_msg': 'Geçersiz modül seçimi.'
            }, status=400)

        monthly_total = sum(m.price_monthly for m in selected_modules)
        yearly_total = sum(m.price_yearly for m in selected_modules)

        with transaction.atomic():
            lead = Lead.objects.filter(
                phone=phone, store__isnull=True, is_deleted=False
            ).first()

            if not lead:
                lead = Lead.objects.create(
                    full_name=f"{first_name} {last_name}",
                    business_name=business_name,
                    phone=phone,
                    email=email if email else None,
                    channel='website',
                    status='new',
                    city=city if city else None,
                    category='store',
                )
            else:
                lead.full_name = f"{first_name} {last_name}"
                lead.business_name = business_name
                if email:
                    lead.email = email
                if city:
                    lead.city = city
                lead.save(update_fields=['full_name', 'business_name', 'email', 'city', 'updated_on'])

            application = PackageApplication.objects.create(
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                email=email if email else None,
                business_name=business_name,
                city=city if city else None,
                monthly_total=monthly_total,
                yearly_total=yearly_total,
                utm_source=utm_source if utm_source else None,
                notes=notes if notes else None,
                lead=lead,
            )
            application.selected_modules.set(selected_modules)

        return JsonResponse({
            'result': True,
            'application_no': application.application_no,
            'message': 'Başvurunuz başarıyla alınmıştır! En kısa sürede sizinle iletişime geçeceğiz.'
        })

    except Exception as e:
        return JsonResponse({
            'result': False,
            'error_msg': f'Bir hata oluştu: {str(e)}'
        }, status=500)
