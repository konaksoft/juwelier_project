"""Cari/Emanet Refactor — Permission seed.

Onay zinciri için permission kodlarını sisteme tanıtır ve "Admin"
adındaki rollere otomatik atar. Idempotent: defalarca çalıştırılabilir.

FAZ 30 (Hızlı Onay Mimarisi) eklemeleri:
  - Yeni izin: `can_approve_ledger_adjustment` (cari fark onayı şemsiyesi)
  - Admin rolüne otomatik atama mantığı (rol adı kontrolüne bağımlılık
    kaldırıldı — onay yetkisi artık doğrudan permission üzerinden okunur).

Kullanım:
    python manage.py seed_cari_permissions
    python manage.py seed_cari_permissions --dry-run
    python manage.py seed_cari_permissions --skip-role-assign
"""

from django.core.management.base import BaseCommand

from apps.roles.models import Permission, Roles


PERMISSIONS = [
    {
        'code': 'customer_adjust_minor',
        'name': 'Cari Küçük Fark/İskonto',
        'group': 'Dashboard',
        'order': 510,
        'is_system_only': False,
    },
    {
        'code': 'customer_adjust_major',
        'name': 'Cari Büyük Fark/İskonto (Yönetici)',
        'group': 'Dashboard',
        'order': 511,
        'is_system_only': False,
    },
    {
        'code': 'customer_writeoff',
        'name': 'Şüpheli Alacak Silme (Üst Yönetici)',
        'group': 'Dashboard',
        'order': 512,
        'is_system_only': False,
    },
    # FAZ 30 — şemsiye onay yetkisi: hızlı onay modalında ve cari hesap
    # merkezindeki "Onayla" butonunda kullanılır. customer_adjust_major
    # ile birlikte verilir; ayrı bir kod tutuluyor çünkü ileride farklı
    # adjustment tipleri için ayrı yetki ayarlanmak istenebilir.
    {
        'code': 'can_approve_ledger_adjustment',
        'name': 'Cari Fark Onayı (Hızlı Onay Modalı)',
        'group': 'Dashboard',
        'order': 513,
        'is_system_only': False,
    },
]


# Rol adı → atanacak permission kodları haritası.
# Karşılaştırma case-insensitive yapılır (lower()).
ROLE_ASSIGNMENTS = {
    # Mağaza Admin / Patron rolü: tüm cari onay yetkileri
    'admin': [
        'customer_adjust_minor',
        'customer_adjust_major',
        'customer_writeoff',
        'can_approve_ledger_adjustment',
    ],
    'mağaza admin': [
        'customer_adjust_minor',
        'customer_adjust_major',
        'customer_writeoff',
        'can_approve_ledger_adjustment',
    ],
    'magaza admin': [
        'customer_adjust_minor',
        'customer_adjust_major',
        'customer_writeoff',
        'can_approve_ledger_adjustment',
    ],
    'store admin': [
        'customer_adjust_minor',
        'customer_adjust_major',
        'customer_writeoff',
        'can_approve_ledger_adjustment',
    ],
    # Veznedar / Kasiyer / Personel: küçük fark onayı (≤ %10)
    'veznedar': ['customer_adjust_minor'],
    'kasiyer': ['customer_adjust_minor'],
    'personel': ['customer_adjust_minor'],
    'satış': ['customer_adjust_minor'],
    'satis': ['customer_adjust_minor'],
}


class Command(BaseCommand):
    help = (
        'Cari/Emanet Refactor için onay zinciri permission kodlarını oluşturur '
        've Admin rollerine otomatik atar.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Yazma yapmadan sadece raporla.',
        )
        parser.add_argument(
            '--skip-role-assign',
            action='store_true',
            help='Permission oluştur ama rollere otomatik atama yapma.',
        )

    # ────────────────────────────────────────────────────────────────────
    # 1) Permission tanımlarını oluştur / güncelle
    # ────────────────────────────────────────────────────────────────────
    def _seed_permissions(self, dry_run: bool):
        created, updated, untouched = 0, 0, 0
        for spec in PERMISSIONS:
            code = spec['code']
            try:
                obj = Permission.objects.get(code=code)
                changed = False
                for k, v in spec.items():
                    if k == 'code':
                        continue
                    if getattr(obj, k, None) != v:
                        setattr(obj, k, v)
                        changed = True
                if changed and not dry_run:
                    obj.save()
                    updated += 1
                    self.stdout.write(self.style.WARNING(
                        f'GÜNCELLENDİ: {code}'
                    ))
                elif changed:
                    self.stdout.write(self.style.WARNING(
                        f'[dry-run] GÜNCELLENECEKTİ: {code}'
                    ))
                else:
                    untouched += 1
                    self.stdout.write(f'OK (değişmedi): {code}')
            except Permission.DoesNotExist:
                if not dry_run:
                    Permission.objects.create(**spec)
                    created += 1
                    self.stdout.write(self.style.SUCCESS(
                        f'OLUŞTURULDU: {code}'
                    ))
                else:
                    self.stdout.write(self.style.SUCCESS(
                        f'[dry-run] OLUŞTURULACAKTI: {code}'
                    ))
        return created, updated, untouched

    # ────────────────────────────────────────────────────────────────────
    # 2) Admin / Veznedar rollerine permission ataması
    # ────────────────────────────────────────────────────────────────────
    def _assign_to_roles(self, dry_run: bool):
        """Rol adı eşleşen rollere ilgili permission'ları ekler.

        Yöntem:
          - Roles tablosundaki tüm STORE kategori rollerini tarar.
          - Adı `ROLE_ASSIGNMENTS` haritasında geçen rolleri bulur.
          - Eksik olan permission'ları RoleDetail üzerinden ekler.

        RoleDetail / role_details ilişkisi mevcut olduğu için onu kullanır;
        alternatif "permissions" M2M ilişkisi de denenir (model çeşitlemesi
        için savunmacı kod).
        """
        try:
            from apps.roles.models import RoleDetail
        except ImportError:
            RoleDetail = None

        assigned, skipped = 0, 0

        roles_qs = Roles.objects.all()
        for role in roles_qs:
            name_key = (role.name or '').strip().lower()
            perm_codes = ROLE_ASSIGNMENTS.get(name_key)
            if not perm_codes:
                continue

            for code in perm_codes:
                try:
                    perm = Permission.objects.get(code=code)
                except Permission.DoesNotExist:
                    self.stdout.write(self.style.ERROR(
                        f'  ! Permission bulunamadı: {code}'
                    ))
                    continue

                already = False
                # RoleDetail üzerinden kontrol
                if RoleDetail is not None:
                    already = RoleDetail.objects.filter(
                        role=role, permission=perm,
                    ).exists()

                if already:
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f'  [dry-run] ATANACAK: {role.name} ← {code}'
                    ))
                    continue

                # Yazma: önce RoleDetail dene
                if RoleDetail is not None:
                    try:
                        RoleDetail.objects.create(role=role, permission=perm)
                        assigned += 1
                        self.stdout.write(self.style.SUCCESS(
                            f'  ATANDI: {role.name} ← {code}'
                        ))
                        continue
                    except Exception as exc:
                        self.stdout.write(self.style.WARNING(
                            f'  RoleDetail yazılamadı ({exc!s}); '
                            f'M2M denenecek.'
                        ))
                # M2M fallback
                try:
                    role.permissions.add(perm)
                    assigned += 1
                    self.stdout.write(self.style.SUCCESS(
                        f'  ATANDI (M2M): {role.name} ← {code}'
                    ))
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(
                        f'  ! Atanamadı: {role.name} ← {code} ({exc!s})'
                    ))

        return assigned, skipped

    # ────────────────────────────────────────────────────────────────────
    # ANA AKIŞ
    # ────────────────────────────────────────────────────────────────────
    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        skip_role_assign = options.get('skip_role_assign', False)

        self.stdout.write(self.style.MIGRATE_HEADING(
            '── 1) Permission tanımları ──'
        ))
        created, updated, untouched = self._seed_permissions(dry_run)

        assigned, skipped = 0, 0
        if not skip_role_assign:
            self.stdout.write(self.style.MIGRATE_HEADING(
                '\n── 2) Rol atamaları ──'
            ))
            assigned, skipped = self._assign_to_roles(dry_run)

        summary = (
            f'\nÖzet — Permissions: {created} yeni, {updated} güncellendi, '
            f'{untouched} değişmedi.'
        )
        if not skip_role_assign:
            summary += (
                f' Roller: {assigned} yeni atama, {skipped} zaten mevcut.'
            )
        if dry_run:
            summary += ' (dry-run — DB yazılmadı)'
        self.stdout.write(self.style.SUCCESS(summary))
