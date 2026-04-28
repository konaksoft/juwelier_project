"""
FAZ 4-5 Ek 8 — Hybrid-Gate: Middleware Katmanı

Aynı 3 katmanlı mantık decorator ile paralel çalışır:
  Katman 1: ABC kodu → RoleDetail kontrolü
  Katman 2: Ana sayfa index kodu → ABC'ye çevir → RoleDetail kontrolü
  Katman 3: Alt işlem kodu → staff: RoleDetail / mağaza: efektif havuz
"""
import re

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import resolve

from apps.roles.models import RoleDetail, Permission
from apps.roles.management.commands._permission_utils import EXCLUDED_APPS
from apps.roles.decorators import MAIN_PAGE_TO_ABC_MAP

_ABC_PATTERN = re.compile(r'^[A-Z0-9]{2,5}\d{3,5}[A-Z]?$')


def _resolve_app_name_from_view(view_func):
    """
    View fonksiyonunun ait olduğu app_name'i tespit eder.
    """
    module = getattr(view_func, '__module__', None)
    if not module:
        return None

    parts = module.split('.')
    if len(parts) < 3 or parts[0] != 'apps':
        return None

    return parts[-2]


def _get_effective_perm_codes(user):
    """
    Kullanıcının mağazası üzerinden efektif yetki CODE setini döndürür.
    Sonuçlar request bazlı cache'lenir (_mw_eff_codes).
    """
    if hasattr(user, '_mw_eff_codes'):
        return user._mw_eff_codes

    codes = set()

    store = getattr(user, 'store', None)
    if store:
        from apps.stores.services import get_store_effective_permission_ids
        perm_ids = get_store_effective_permission_ids(store)
        if perm_ids:
            codes = set(
                Permission.objects.filter(id__in=perm_ids)
                .values_list('code', flat=True)
            )

    user._mw_eff_codes = codes
    return codes


class RolePermissionMiddleware:
    """
    Middleware seviyesinde SaaS yetki kontrolü (Hybrid-Gate / 3 Katmanlı).

    Katman 1: ABC kodu → Personelin RoleDetail'inde var mı?
    Katman 2: Ana sayfa index kodu → MAIN_PAGE_TO_ABC_MAP ile ABC'ye çevir
              → RoleDetail kontrolü (URL Guessing koruması)
    Katman 3: Alt işlem kodu → staff: RoleDetail / mağaza: efektif havuz

    Bypass Kuralları:
      - is_authenticated=False → pass
      - is_superuser=True → pass
      - EXCLUDED_APPS view'ları → pass
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated or request.user.is_superuser:
            return self.get_response(request)

        if not (request.path.startswith("/dashboard/")
                or request.path.startswith("/management/")):
            return self.get_response(request)

        try:
            resolved = resolve(request.path_info)
            view_func = resolved.func

            # ── İstisna app bypass ──
            app_name = _resolve_app_name_from_view(view_func)
            if app_name and app_name in EXCLUDED_APPS:
                return self.get_response(request)

            perm_code = getattr(view_func, 'permission_code', None)
            if not perm_code:
                return self.get_response(request)

            u = request.user

            # ─────────────────────────────────────────────
            # KATMAN 1: ABC Menü Kodu → RoleDetail kontrolü
            # ─────────────────────────────────────────────
            if _ABC_PATTERN.match(perm_code):
                has_perm = (
                    RoleDetail.objects.filter(
                        role_id=u.role_id,
                        permission__code=perm_code,
                        status=True,
                    ).exists()
                    if u.role_id
                    else False
                )
                if not has_perm:
                    messages.error(
                        request,
                        "Bu menüye erişim yetkiniz bulunmamaktadır."
                    )
                    return redirect("dashboard:index")
                return self.get_response(request)

            # ─────────────────────────────────────────────
            # KATMAN 2: Ana Sayfa Index Kodu → ABC'ye çevir
            #           → RoleDetail kontrolü
            # (URL Guessing Koruması)
            # ─────────────────────────────────────────────
            mapped_abc = MAIN_PAGE_TO_ABC_MAP.get(perm_code)
            if mapped_abc:
                has_perm = (
                    RoleDetail.objects.filter(
                        role_id=u.role_id,
                        permission__code=mapped_abc,
                        status=True,
                    ).exists()
                    if u.role_id
                    else False
                )
                if not has_perm:
                    messages.error(
                        request,
                        "Bu sayfaya erişim yetkiniz bulunmamaktadır."
                    )
                    return redirect("dashboard:index")
                return self.get_response(request)

            # ─────────────────────────────────────────────
            # KATMAN 3a: Alt İşlem — Konasoft personeli → rol kontrolü
            # ─────────────────────────────────────────────
            if getattr(u, 'is_staff', False):
                has_perm = (
                    RoleDetail.objects.filter(
                        role_id=u.role_id,
                        permission__code=perm_code,
                        status=True,
                    ).exists()
                    if u.role_id
                    else False
                )
                if not has_perm:
                    messages.error(
                        request,
                        "Bu alana erişim yetkiniz bulunmamaktadır."
                    )
                    return redirect("dashboard:index")
                return self.get_response(request)

            # ─────────────────────────────────────────────
            # KATMAN 3b: Alt İşlem — Mağaza personeli → efektif havuz
            # ─────────────────────────────────────────────
            effective_codes = _get_effective_perm_codes(u)

            if not effective_codes:
                messages.error(
                    request,
                    "Hesabınıza bağlı aktif bir paket veya modül bulunamadı."
                )
                return redirect("dashboard:index")

            if perm_code not in effective_codes:
                messages.error(
                    request,
                    "Bu özellik mevcut paketinizin kapsamında değil."
                )
                return redirect("dashboard:index")

        except Exception:
            pass

        return self.get_response(request)
