"""
Permission tablosu temizleme komutu.

URL-routed olmayan çöp permission kayıtlarını (helper fonksiyonlar,
private fonksiyonlar vb.) tespit edip siler ve meşru kayıtların
group/is_system_only alanlarını düzeltir.

Ek olarak, EXCLUDED_APPS listesindeki uygulamalara ait mevcut
permission kayıtları agresif olarak silinir — bu uygulamalar
SaaS rol/permission sisteminin dışında tutulur.

Kullanım:
    python manage.py cleanup_permissions                  # Dry-run (varsayılan)
    python manage.py cleanup_permissions --execute        # Gerçek silme + düzeltme
    python manage.py cleanup_permissions --fix-groups-only  # Sadece group düzelt
"""

import uuid

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.roles.models import Permission, RoleDetail
from apps.crm.packages.models import PackagePermissionMatrix, SaaSModule
from apps.roles.management.commands._permission_utils import (
    build_valid_code_set,
    discover_url_routed_views,
    build_excluded_app_prefixes,
    ABC_PATTERN,
    EXCLUDED_APPS,
)


class Command(BaseCommand):
    help = (
        "Çöp permission kayıtlarını tespit edip siler, "
        "meşru kayıtların group alanlarını düzeltir. "
        "EXCLUDED_APPS listesindeki app'lere ait permission'ları "
        "agresif olarak siler."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            default=False,
            help=(
                'Gerçek silme ve güncelleme işlemini yap. '
                'Bu flag olmadan sadece rapor üretilir (dry-run).'
            ),
        )
        parser.add_argument(
            '--fix-groups-only',
            action='store_true',
            default=False,
            help=(
                'Sadece meşru permission kayıtlarının group ve '
                'is_system_only alanlarını düzelt. Çöp silme yapma.'
            ),
        )

    def handle(self, *args, **options):
        execute = options['execute']
        fix_groups_only = options['fix_groups_only']

        mode_label = "EXECUTE" if execute else "DRY-RUN"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Permission Temizleme — Mod: {mode_label}"
        ))
        self.stdout.write("")

        # İstisna uygulama bilgisi
        if EXCLUDED_APPS:
            self.stdout.write(self.style.WARNING(
                f"İstisna uygulamalar (agresif silinecek): "
                f"{', '.join(sorted(EXCLUDED_APPS))}"
            ))
            self.stdout.write("")

        # ─────────────────────────────────────────────────
        #  1) Geçerli permission kodlarını topla
        # ─────────────────────────────────────────────────
        routed_views = discover_url_routed_views()
        valid_codes = {v['code'] for v in routed_views}

        # code → view_info lookup (group düzeltme için)
        code_to_info = {v['code']: v for v in routed_views}

        # İstisna app prefix'leri (agresif silme için)
        excluded_prefixes = build_excluded_app_prefixes()

        self.stdout.write(
            f"  URL-routed geçerli permission sayısı: {len(valid_codes)}"
        )

        # ─────────────────────────────────────────────────
        #  2) Veritabanındaki tüm permission'ları tara
        # ─────────────────────────────────────────────────
        all_perms = list(Permission.objects.all().order_by('code'))
        self.stdout.write(
            f"  Veritabanındaki toplam permission sayısı: {len(all_perms)}"
        )

        garbage_perms = []
        excluded_perms = []
        valid_perms = []
        abc_perms = []

        for perm in all_perms:
            # Önce istisna app kontrolü — prefix eşleşmesi
            is_from_excluded = any(
                perm.code.startswith(prefix)
                for prefix in excluded_prefixes
            )
            # Ayrıca group alanı üzerinden de kontrol et
            if not is_from_excluded and perm.group in EXCLUDED_APPS:
                is_from_excluded = True

            if is_from_excluded:
                excluded_perms.append(perm)
            elif perm.code in valid_codes:
                valid_perms.append(perm)
            elif ABC_PATTERN.match(perm.code):
                abc_perms.append(perm)
            else:
                garbage_perms.append(perm)

        self.stdout.write(
            f"  Geçerli (URL-routed): {len(valid_perms)}"
        )
        self.stdout.write(
            f"  ABC-pattern (menü yetkileri, korunacak): {len(abc_perms)}"
        )
        self.stdout.write(self.style.WARNING(
            f"  İstisna app permission'ları (agresif silinecek): "
            f"{len(excluded_perms)}"
        ))
        self.stdout.write(
            f"  Çöp (silinecek): {len(garbage_perms)}"
        )
        self.stdout.write("")

        # ─────────────────────────────────────────────────
        #  3) İstisna app permission'larını agresif sil
        # ─────────────────────────────────────────────────
        if not fix_groups_only:
            self._handle_excluded(excluded_perms, execute)

        # ─────────────────────────────────────────────────
        #  4) Çöp analizi ve silme
        # ─────────────────────────────────────────────────
        if not fix_groups_only:
            self._handle_garbage(garbage_perms, execute)

        # ─────────────────────────────────────────────────
        #  5) Group düzeltme
        # ─────────────────────────────────────────────────
        self._fix_groups(valid_perms, code_to_info, execute)

        # ─────────────────────────────────────────────────
        #  6) Özet
        # ─────────────────────────────────────────────────
        self.stdout.write("")
        if not execute:
            self.stdout.write(self.style.WARNING(
                "DRY-RUN modu — hiçbir değişiklik yapılmadı. "
                "Gerçek işlem için --execute flag'ini kullanın."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "İşlem tamamlandı."
            ))

    def _handle_excluded(self, excluded_perms, execute):
        """İstisna app'lere ait permission'ları agresif olarak sil."""
        if not excluded_perms:
            self.stdout.write(self.style.SUCCESS(
                "İstisna app'lere ait permission bulunamadı — temiz."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"İstisna App Permission Detayı ({len(excluded_perms)} adet):"
        ))

        total_role_details = 0
        total_matrix_entries = 0
        total_module_links = 0

        for perm in excluded_perms:
            rd_count = RoleDetail.objects.filter(permission=perm).count()
            pm_count = PackagePermissionMatrix.objects.filter(
                permission=perm
            ).count()
            mod_count = perm.saas_modules.count()

            total_role_details += rd_count
            total_matrix_entries += pm_count
            total_module_links += mod_count

            cascade_parts = []
            if rd_count:
                cascade_parts.append(f"RoleDetail={rd_count}")
            if pm_count:
                cascade_parts.append(f"PackageMatrix={pm_count}")
            if mod_count:
                cascade_parts.append(f"SaaSModule={mod_count}")

            cascade_str = (
                f" [cascade: {', '.join(cascade_parts)}]"
                if cascade_parts
                else ""
            )

            self.stdout.write(self.style.WARNING(
                f"  ✗ {perm.code:50s} "
                f"(group={perm.group!r}) [EXCLUDED]{cascade_str}"
            ))

        self.stdout.write("")
        self.stdout.write(
            f"  Toplam cascade etki: "
            f"RoleDetail={total_role_details}, "
            f"PackagePermissionMatrix={total_matrix_entries}, "
            f"SaaSModule M2M={total_module_links}"
        )

        if execute:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "İstisna app permission'ları siliniyor..."
            ))

            excluded_ids = [p.id for p in excluded_perms]

            with transaction.atomic():
                for perm in excluded_perms:
                    perm.saas_modules.clear()

                deleted_count = Permission.objects.filter(
                    id__in=excluded_ids
                ).delete()[0]

            self.stdout.write(self.style.SUCCESS(
                f"  {deleted_count} istisna app permission ve ilişkili "
                f"kayıtlar silindi."
            ))

    def _handle_garbage(self, garbage_perms, execute):
        """Çöp permission'ları analiz et ve (execute ise) sil."""
        if not garbage_perms:
            self.stdout.write(self.style.SUCCESS(
                "Çöp permission bulunamadı — tablo temiz."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Çöp Permission Detayı ({len(garbage_perms)} adet):"
        ))

        total_role_details = 0
        total_matrix_entries = 0
        total_module_links = 0

        for perm in garbage_perms:
            # Cascade etki analizi
            rd_count = RoleDetail.objects.filter(permission=perm).count()
            pm_count = PackagePermissionMatrix.objects.filter(
                permission=perm
            ).count()
            mod_count = perm.saas_modules.count()

            total_role_details += rd_count
            total_matrix_entries += pm_count
            total_module_links += mod_count

            cascade_parts = []
            if rd_count:
                cascade_parts.append(f"RoleDetail={rd_count}")
            if pm_count:
                cascade_parts.append(f"PackageMatrix={pm_count}")
            if mod_count:
                cascade_parts.append(f"SaaSModule={mod_count}")

            cascade_str = (
                f" [cascade: {', '.join(cascade_parts)}]"
                if cascade_parts
                else ""
            )

            self.stdout.write(
                f"  ✗ {perm.code:50s} (group={perm.group!r}){cascade_str}"
            )

        self.stdout.write("")
        self.stdout.write(
            f"  Toplam cascade etki: "
            f"RoleDetail={total_role_details}, "
            f"PackagePermissionMatrix={total_matrix_entries}, "
            f"SaaSModule M2M={total_module_links}"
        )

        if execute:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Silme işlemi başlıyor..."))

            garbage_ids = [p.id for p in garbage_perms]

            with transaction.atomic():
                # 1) SaaSModule M2M bağlantılarını kaldır
                for perm in garbage_perms:
                    perm.saas_modules.clear()

                # 2) Permission silme (RoleDetail ve PackagePermissionMatrix
                #    CASCADE ile otomatik silinir)
                deleted_count = Permission.objects.filter(
                    id__in=garbage_ids
                ).delete()[0]

            self.stdout.write(self.style.SUCCESS(
                f"  {deleted_count} çöp permission ve ilişkili "
                f"kayıtlar silindi."
            ))

    def _fix_groups(self, valid_perms, code_to_info, execute):
        """Meşru permission'ların group ve is_system_only alanlarını düzelt."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Group Düzeltme:"))

        fix_count = 0

        for perm in valid_perms:
            info = code_to_info.get(perm.code)
            if not info:
                continue

            new_group = info['group']
            new_system = info['is_system_only']

            changes = []
            if perm.group != new_group:
                changes.append(f"group: {perm.group!r} → {new_group!r}")
            if perm.is_system_only != new_system:
                changes.append(
                    f"is_system_only: {perm.is_system_only} → {new_system}"
                )

            if not changes:
                continue

            fix_count += 1
            self.stdout.write(
                f"  ~ {perm.code:50s} — {', '.join(changes)}"
            )

            if execute:
                perm.group = new_group
                perm.is_system_only = new_system
                perm.save(update_fields=['group', 'is_system_only'])

        if fix_count == 0:
            self.stdout.write(self.style.SUCCESS(
                "  Tüm group değerleri zaten doğru."
            ))
        else:
            self.stdout.write("")
            self.stdout.write(
                f"  Düzeltilecek kayıt sayısı: {fix_count}"
            )
