# ============================================================================
# DOSYA: apps/banking/management/commands/populate_expense_categories.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v1 — FAZ 61 (Hızlı Gider Modülü)
#
# AMAÇ:
#   Aktif her mağazaya 12 sistem preset gider kategorisini idempotent
#   şekilde tanıtır (Yemek, Kargo, Atölye, Kırtasiye vb.). .cursorrules
#   RULE 12 uyumu için migration içinde INSERT yapılmaz; bu komut elle
#   tetiklenir.
#
# KULLANIM:
#   python manage.py populate_expense_categories
#   python manage.py populate_expense_categories --dry-run
#   python manage.py populate_expense_categories --store-id=42
#
# GÜVENLİK:
#   - İdempotent: Tekrar çalıştırılırsa duplikasyon oluşmaz (get_or_create).
#   - Mağaza-izole: Her mağaza için ayrı kayıt.
#   - is_system_preset=True işaretler; UI'dan silinemez (sadece deaktif).
#   - --dry-run: Hiçbir kayıt yazmadan etkileyeceği işlem sayısını yazar.
# ============================================================================

import logging
from django.core.management.base import BaseCommand

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Sistem Preset Kategorileri — sırayla en sık kullanılanlar üstte
# ────────────────────────────────────────────────────────────────────────────
SYSTEM_PRESETS = [
    {'name': 'Yemek',      'short_code': 'YMK', 'icon': 'bi-cup-hot',         'color_css': '#f59e0b', 'display_order': 10},
    {'name': 'Atölye',     'short_code': 'ATL', 'icon': 'bi-tools',           'color_css': '#0d47a1', 'display_order': 20},
    {'name': 'Kargo',      'short_code': 'KRG', 'icon': 'bi-truck',           'color_css': '#10b981', 'display_order': 30},
    {'name': 'Kırtasiye',  'short_code': 'KRT', 'icon': 'bi-pencil',          'color_css': '#6366f1', 'display_order': 40},
    {'name': 'Personel',   'short_code': 'PRS', 'icon': 'bi-person-badge',    'color_css': '#ec4899', 'display_order': 50},
    {'name': 'Kira',       'short_code': 'KRA', 'icon': 'bi-house',           'color_css': '#8b5cf6', 'display_order': 60},
    {'name': 'Fatura',     'short_code': 'FTR', 'icon': 'bi-receipt',         'color_css': '#ef4444', 'display_order': 70},
    {'name': 'Vergi',      'short_code': 'VRG', 'icon': 'bi-bank',            'color_css': '#374151', 'display_order': 80},
    {'name': 'Bakım',      'short_code': 'BKM', 'icon': 'bi-wrench',          'color_css': '#0ea5e9', 'display_order': 90},
    {'name': 'Temizlik',   'short_code': 'TMZ', 'icon': 'bi-droplet',         'color_css': '#14b8a6', 'display_order': 100},
    {'name': 'Yakıt',      'short_code': 'YKT', 'icon': 'bi-fuel-pump',       'color_css': '#f97316', 'display_order': 110},
    {'name': 'Diğer',      'short_code': 'DGR', 'icon': 'bi-three-dots',      'color_css': '#6b7280', 'display_order': 999},
]


class Command(BaseCommand):
    help = 'Aktif her mağazaya sistem preset gider kategorilerini ekler (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Hiçbir kayıt yazma; sadece etkileyeceği işlem sayısını yazdır.',
        )
        parser.add_argument(
            '--store-id',
            type=int,
            default=None,
            help='Sadece belirli bir mağaza için çalıştır (varsayılan: tüm aktif mağazalar).',
        )

    def handle(self, *args, **options):
        from apps.stores.models import Stores
        from apps.banking.models import ExpenseCategory

        dry_run = options['dry_run']
        store_id = options['store_id']

        qs = Stores.objects.filter(is_active=True, is_deleted=False)
        if store_id:
            qs = qs.filter(id=store_id)

        total_stores = qs.count()
        if total_stores == 0:
            self.stdout.write(self.style.WARNING(
                'Eşleşen aktif mağaza bulunamadı.'
            ))
            return

        prefix = '[DRY-RUN] ' if dry_run else ''
        self.stdout.write(self.style.NOTICE(
            f'{prefix}{total_stores} mağaza × {len(SYSTEM_PRESETS)} preset = '
            f'{total_stores * len(SYSTEM_PRESETS)} potansiyel kayıt.'
        ))

        created_count = 0
        skipped_count = 0

        for store in qs.iterator():
            for preset in SYSTEM_PRESETS:
                # İdempotent: aynı (store, name) için yeniden eklemez.
                exists = ExpenseCategory.objects.filter(
                    store=store, name=preset['name']
                ).exists()

                if exists:
                    skipped_count += 1
                    continue

                if dry_run:
                    self.stdout.write(
                        f'  [+] {store.title or store.id} → "{preset["name"]}" ({preset["short_code"]})'
                    )
                    created_count += 1
                    continue

                ExpenseCategory.objects.create(
                    store=store,
                    name=preset['name'],
                    short_code=preset['short_code'],
                    icon=preset['icon'],
                    color_css=preset['color_css'],
                    display_order=preset['display_order'],
                    is_active=True,
                    is_system_preset=True,
                    created_by=None,
                )
                created_count += 1

        msg = (
            f'{prefix}Tamamlandı: {created_count} yeni kategori, '
            f'{skipped_count} zaten mevcut (atlandı).'
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(msg))
        else:
            self.stdout.write(self.style.SUCCESS(msg))
            log.info(
                "FAZ61 populate_expense_categories: created=%s skipped=%s store_filter=%s",
                created_count, skipped_count, store_id,
            )
