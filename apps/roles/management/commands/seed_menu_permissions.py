"""
Menü görünürlük kodlarının (ABC-pattern) Permission tablosunda
karşılığının olmasını garanti eden seed komutu.

base.html'deki sol menü öğeleri {% if request.user|menu_allowed:'ABC1017D' %}
gibi kontroller kullanır. Bu ABC kodları sync_permissions tarafından
oluşturulmaz (sadece URL-routed view izinleri oluşturulur).
Bu komut eksik ABC kodlarını idempotent olarak ekler.

Kullanım:
    python manage.py seed_menu_permissions
"""

import uuid

from django.core.management.base import BaseCommand

from apps.roles.models import Permission
from apps.stores.views import GROUP_LABELS, GROUP_MENU_CODES


# ─────────────────────────────────────────────────────────────────
# ABC menü kodları tanım listesi
# Format: (code, name, is_system_only)
#
# name alanı, grup slug'ına karşılık gelen GROUP_LABELS değerinden
# otomatik türetilir. is_system_only=False çünkü bu kodlar
# mağaza yöneticisinin personele atayabileceği yetkilerdir.
# ─────────────────────────────────────────────────────────────────
def _build_menu_permission_list():
    """
    GROUP_MENU_CODES sözlüğünden menü izin listesi oluşturur.

    Returns:
        list[tuple]: (code, name, is_system_only) üçlüleri
    """
    result = []
    seen_codes = set()

    for group_slug, abc_codes in GROUP_MENU_CODES.items():
        label = GROUP_LABELS.get(group_slug, group_slug.replace('_', ' ').title())

        for code in abc_codes:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            result.append((code, f"Menü: {label}", False))

    return sorted(result, key=lambda x: x[0])


class Command(BaseCommand):
    help = (
        "ABC-pattern menü görünürlük kodlarını Permission tablosuna ekler. "
        "Mevcut kayıtlara dokunmaz (idempotent)."
    )

    def handle(self, *args, **options):
        menu_perms = _build_menu_permission_list()

        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(f"  seed_menu_permissions")
        self.stdout.write(f"{'=' * 60}\n")
        self.stdout.write(f"GROUP_MENU_CODES'dan {len(menu_perms)} ABC kodu tespit edildi.\n")

        created_count = 0
        existing_count = 0

        fixed_count = 0

        for code, name, is_system_only in menu_perms:
            perm, created = Permission.objects.get_or_create(
                code=code,
                defaults={
                    'id': uuid.uuid4(),
                    'name': name,
                    'group': None,
                    'order': 0,
                    'is_system_only': is_system_only,
                },
            )
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  + Eklendi: {code} ({name})"
                ))
            else:
                existing_count += 1
                # Mevcut ABC kodlarının is_system_only=False olduğunu garanti et
                if perm.is_system_only:
                    perm.is_system_only = False
                    perm.save(update_fields=['is_system_only'])
                    fixed_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"  ~ Düzeltildi: {code} (is_system_only → False)"
                    ))
                else:
                    self.stdout.write(f"  = Mevcut:  {code}")

        self.stdout.write(f"\n{'─' * 60}")
        self.stdout.write(self.style.SUCCESS(
            f"Tamamlandı: {created_count} yeni eklendi, "
            f"{existing_count} mevcut, {fixed_count} düzeltildi (is_system_only → False)."
        ))
