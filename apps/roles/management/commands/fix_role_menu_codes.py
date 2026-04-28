"""
Mevcut mağaza rollerine eksik menü görünürlük kodlarını (ABC-pattern) ekler.

Bug: _resolve_groups_to_perm_ids fonksiyonu daha önce yalnızca işlevsel
izinleri (Tip A) role atıyordu; ABC menü kodları (Tip B) atlanıyordu.
Bu komut, mevcut rollerdeki eksik ABC kodlarını geriye dönük olarak ekler.

Kullanım:
    python manage.py fix_role_menu_codes              # Dry-run (varsayılan)
    python manage.py fix_role_menu_codes --execute    # Gerçek ekleme
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.roles.models import Permission, Roles, RoleDetail
from apps.stores.views import GROUP_MENU_CODES


class Command(BaseCommand):
    help = (
        "Mevcut mağaza rollerine eksik ABC menü görünürlük kodlarını ekler. "
        "Varsayılan olarak dry-run modunda çalışır."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            default=False,
            help='Gerçek ekleme işlemini yap. Bu flag olmadan sadece rapor üretilir.',
        )

    def handle(self, *args, **options):
        execute = options['execute']
        mode = "EXECUTE" if execute else "DRY-RUN"
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(f"  fix_role_menu_codes — {mode}")
        self.stdout.write(f"{'=' * 60}\n")

        # ABC kodlarının Permission ID'lerini topla
        all_abc_codes = set()
        for codes in GROUP_MENU_CODES.values():
            all_abc_codes.update(codes)

        abc_perms = {
            p.code: p
            for p in Permission.objects.filter(code__in=all_abc_codes)
        }

        if not abc_perms:
            self.stdout.write(self.style.WARNING(
                "Veritabanında hiç ABC menü kodu bulunamadı. İşlem yapılmadı."
            ))
            return

        self.stdout.write(f"Bulunan ABC kodları: {len(abc_perms)}")

        # Tüm grup → ABC kodu ters haritası
        # ABC kodu → hangi gruplara ait?
        abc_code_to_groups = {}
        for group_slug, codes in GROUP_MENU_CODES.items():
            for code in codes:
                abc_code_to_groups.setdefault(code, set()).add(group_slug)

        # Mağaza rollerini tara
        store_roles = Roles.objects.filter(
            category='STORE',
            store__isnull=False,
            is_deleted=False,
        ).prefetch_related('roledetail_set__permission')

        total_added = 0
        roles_fixed = 0

        for role in store_roles:
            # Rolün mevcut yetkileri
            existing_details = role.roledetail_set.filter(status=True)
            existing_perm_ids = {rd.permission_id for rd in existing_details}
            existing_codes = {rd.permission.code for rd in existing_details}

            # Rolde aktif olan grupları belirle
            existing_groups = set()
            for rd in existing_details:
                grp = rd.permission.group
                if grp:
                    existing_groups.add(grp)

            # Bu gruplara karşılık gelen eksik ABC kodlarını bul
            missing_details = []
            for group_slug in existing_groups:
                menu_codes = GROUP_MENU_CODES.get(group_slug, [])
                for abc_code in menu_codes:
                    if abc_code in existing_codes:
                        continue
                    perm = abc_perms.get(abc_code)
                    if perm and perm.id not in existing_perm_ids:
                        missing_details.append(
                            RoleDetail(role=role, permission=perm, status=True)
                        )
                        existing_perm_ids.add(perm.id)
                        existing_codes.add(abc_code)

            if missing_details:
                roles_fixed += 1
                total_added += len(missing_details)
                codes_str = ', '.join(d.permission.code for d in missing_details)
                self.stdout.write(
                    f"  Rol: {role.name} (store={role.store_id}) "
                    f"→ +{len(missing_details)} ABC kodu: {codes_str}"
                )

                if execute:
                    RoleDetail.objects.bulk_create(
                        missing_details,
                        ignore_conflicts=True,
                    )

        self.stdout.write(f"\n{'─' * 60}")
        self.stdout.write(f"Toplam: {roles_fixed} rol, {total_added} eksik ABC kodu")

        if not execute and total_added > 0:
            self.stdout.write(self.style.WARNING(
                "\nBu bir DRY-RUN'dır. Değişiklikleri uygulamak için:\n"
                "  python manage.py fix_role_menu_codes --execute"
            ))
        elif execute and total_added > 0:
            self.stdout.write(self.style.SUCCESS(
                f"\n{total_added} ABC kodu başarıyla eklendi."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nEksik ABC kodu bulunamadı. Tüm roller güncel."
            ))
