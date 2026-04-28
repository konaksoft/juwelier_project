# apps/roles/templatetags/role_tags.py
"""
FAZ 4-5 Ek 6 — Radikal Sadeleştirme: Template Tag Katmanı

menu_allowed filtresi dual-gate mantığına uyumlu:
  ABC kodu → RoleDetail kontrolü (menü görünürlüğü)
  İşlevsel kod (staff) → RoleDetail kontrolü
  İşlevsel kod (mağaza personeli) → Sadece efektif havuz (rol yok sayılır)
"""
from __future__ import annotations

import re
from typing import Optional, Set

from django import template
from apps.crm.packages.models import *
from apps.roles.models import *

register = template.Library()

# ABC-pattern menü görünürlük kodları regex'i
# Örnekler: ABC1001D, ABC1706D, ABCD005D
_ABC_PATTERN = re.compile(r'^[A-Z0-9]{2,5}\d{3,5}[A-Z]?$')


@register.filter
def is_checked(perm_id, role_details):
    try:
        return role_details.filter(permission_id=perm_id, status=True).exists()
    except Exception:
        try:
            return any(
                getattr(rd, "permission_id", None) == perm_id and getattr(rd, "status", False) for rd in role_details)
        except Exception:
            return False


def _package_perm_codes(user) -> Set[str]:
    """
    Kullanıcının mağazası üzerinden efektif yetki CODE setini döndürür.

    Hesaplama (ADDİTİVE UNION):
        Çekirdek Modül Yetkileri ∪ Paket Yetkileri ∪ StoreModule Yetkileri

    Paketsiz mağazalarda sadece StoreModule + Çekirdek modül yetkileri geçerlidir.
    Paketli mağazalarda PackagePermissionMatrix de eklenir.
    """
    if hasattr(user, "_pkg_codes"):
        return user._pkg_codes  # type: ignore[attr-defined]

    codes: Set[str] = set()

    # 1. Doğrudan kullanıcıya bağlı paket (nadiren; eski yapı uyumluluğu)
    direct_package_id = getattr(user, "package_id", None) or \
                        getattr(getattr(user, "package", None), "id", None)
    if direct_package_id:
        codes |= set(
            PackagePermissionMatrix.objects.filter(
                package_id=str(direct_package_id),
                available=True,
            ).values_list("permission__code", flat=True)
        )

    # 2. Mağaza üzerinden efektif yetkiler (Çekirdek + Paket + StoreModule)
    store = getattr(user, "store", None)
    if store:
        from apps.stores.services import get_store_effective_permission_ids
        perm_ids = get_store_effective_permission_ids(store)
        if perm_ids:
            codes |= set(
                Permission.objects.filter(id__in=perm_ids)
                .values_list("code", flat=True)
            )

    user._pkg_codes = codes  # type: ignore[attr-defined]
    return codes


def _role_perm_codes(user) -> Set[str]:
    """
    Kullanıcının tekil rolüne göre RoleDetail(status=True) üzerinden
    Permission.code setini döndürür.
    Sadeleştirme sonrası bu set yalnızca ABC menü kodlarını içerir.
    """
    if hasattr(user, "_role_codes"):
        return user._role_codes  # type: ignore[attr-defined]

    role_id = getattr(user, "role_id", None)
    if not role_id:
        codes: Set[str] = set()
    else:
        codes = set(
            RoleDetail.objects.filter(
                role_id=role_id, status=True
            ).values_list("permission__code", flat=True).distinct()
        )

    user._role_codes = codes  # type: ignore[attr-defined]
    return codes


@register.filter
def menu_allowed(user, perm_code: str) -> bool:
    """
    Menü / yetki görünürlük filtresi (Sadeleştirilmiş Dual-Gate).

    Mantık:
      1. Giriş yapmamış → False
      2. Superuser → True (her şeyi görür)
      3. ABC menü kodu → Personelin RoleDetail'inde var mı?
         (Menü görünürlüğü personelin rolüne bağlı)
      4. Konasoft personeli (is_staff) → Klasik rol kontrolü
         (Paket/modül kavramı yok)
      5. Mağaza personeli + işlevsel kod → Sadece mağaza efektif havuzu
         (Personelin bireysel rolü yok sayılır; mağazanın paketinde
          bu yetki varsa menü öğesi gösterilir)
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False

    # 1. Superuser → her şeyi görür
    if getattr(user, "is_superuser", False):
        return True

    # 2. ABC menü kodu → RoleDetail kontrolü
    if _ABC_PATTERN.match(perm_code):
        role_perms = _role_perm_codes(user)
        return perm_code in role_perms

    # 3. Konasoft personeli → klasik rol kontrolü
    if getattr(user, "is_staff", False):
        role_perms = _role_perm_codes(user)
        return perm_code in role_perms

    # 4. Mağaza personeli + işlevsel kod → sadece efektif havuz
    store_perms = _package_perm_codes(user)
    return perm_code in store_perms
