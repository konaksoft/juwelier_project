"""
Django Management Command: fix_global_product_protection
========================================================

Standart paylaşılan ürün koruması (2026-05-07).

Sorun:
    Standart sarrafiye/döviz/altın ürünleri (Eski Tam, 22 Ayar Gram, USDTRY,
    Has Altın, vb.) tüm mağazalar tarafından `Q(store__isnull=True)` filtresiyle
    paylaşılır. Bu kayıtların `is_protected=True` olması gerekir aksi halde
    `change_status` / `delete` / `product_add` UPDATE view'leri yalnızca
    `is_protected` kontrolü yaptığı için herhangi bir mağaza kullanıcısı tüm
    mağazalardan ürün gizleyebilir / silebilir.

    Tespit edilen incident: "Eski Tam" ve "22 Ayar Gram" `is_active=False`
    durumuna geçti ve tüm kuyumcular için kayboldu. Sebep: bir mağaza
    kullanıcısı Ürün Yönetimi listesinde gördüğü global ürünü "Pasif Yap" ile
    toggle etti — `is_protected=False` olduğu için `change_status` engellemedi.

Hedef kayıtlar:
    - store IS NULL                 → global paylaşılan ürünler
    - is_protected = False          → koruma flag'i eksik
    - created_by IS NULL            → kullanıcı tarafından oluşturulmamış
                                      (tasks.py / fixture / management cmd)
    - is_deleted = False            → soft-delete'liler hariç

Onarım (iki adım):
    1. is_protected = True           → koruma flag'i set
    2. is_active = True              → yanlışlıkla deaktive edilmişse geri aç

Kullanım:
    # 1) Sadece raporla (DEFAULT — güvenli)
    python manage.py fix_global_product_protection

    # 2) Detaylı liste
    python manage.py fix_global_product_protection --verbose

    # 3) Onarımı uygula
    python manage.py fix_global_product_protection --apply

Append-only / Veri Bütünlüğü:
    - `Products.objects.filter(...).update(...)` kullanır → save() override'ı
      tetiklenmez, full_clean() bypass edilir, sadece iki flag güncellenir.
    - StockSnapshot, StockLedger, GoldPurchases hiç etkilenmez (bu komut
      yalnızca Products.is_protected ve Products.is_active alanlarını yazar).
    - Soft-delete'li (`is_deleted=True`) ürünler bilinçli olarak hariç tutuldu;
      bunlar başka bir nedenle silinmiş olabilir.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.products.models import Products


class Command(BaseCommand):
    help = (
        "Global standart ürünleri (store=None, created_by=None) koruma altına alır: "
        "is_protected=True ve is_active=True yapar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Onarımı uygula (default: dry-run sadece raporlar).',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Etkilenecek her ürünü tek tek listele.',
        )

    def handle(self, *args, **options):
        apply_fix = options['apply']
        verbose = options['verbose']

        # Hedef: global standart ürünler — koruma flag'i eksik.
        unprotected_qs = Products.objects.filter(
            store__isnull=True,
            created_by__isnull=True,
            is_protected=False,
            is_deleted=False,
        ).select_related('category')

        # Yanlışlıkla deaktive edilmiş standart ürünler — re-activate adayları.
        # Bu set, unprotected_qs ile çakışabilir veya `is_protected=True` olup
        # yine de `is_active=False` kalmış kayıtları yakalar (zincir tamamlamak).
        deactivated_qs = Products.objects.filter(
            store__isnull=True,
            created_by__isnull=True,
            is_active=False,
            is_deleted=False,
        ).select_related('category')

        unprotected_count = unprotected_qs.count()
        deactivated_count = deactivated_qs.count()

        if unprotected_count == 0 and deactivated_count == 0:
            self.stdout.write(self.style.SUCCESS(
                'Tüm global standart ürünler korunuyor ve aktif. Sistem temiz.'
            ))
            return

        if unprotected_count:
            self.stdout.write(self.style.WARNING(
                f'\n[KORUMASIZ] {unprotected_count} global ürün is_protected=False:'
            ))
            rows = list(unprotected_qs.values('id', 'name', 'category__name', 'is_active'))
            preview = rows if verbose else rows[:15]
            for r in preview:
                status = '✓ aktif' if r['is_active'] else '✗ PASİF'
                self.stdout.write(
                    f"  - {r['name']:<35} | {r['category__name'] or '-':<20} | {status}"
                )
            if not verbose and len(rows) > 15:
                self.stdout.write(
                    f"  ... ({len(rows) - 15} ürün daha — tümünü görmek için --verbose)"
                )

        if deactivated_count:
            self.stdout.write(self.style.WARNING(
                f'\n[YANLIŞ DEAKTİF] {deactivated_count} global ürün is_active=False:'
            ))
            rows = list(deactivated_qs.values('id', 'name', 'category__name', 'is_protected'))
            preview = rows if verbose else rows[:15]
            for r in preview:
                prot = '🔒 korunmuş' if r['is_protected'] else '⚠ korumasız'
                self.stdout.write(
                    f"  - {r['name']:<35} | {r['category__name'] or '-':<20} | {prot}"
                )
            if not verbose and len(rows) > 15:
                self.stdout.write(
                    f"  ... ({len(rows) - 15} ürün daha — tümünü görmek için --verbose)"
                )

        if not apply_fix:
            self.stdout.write(self.style.WARNING(
                '\n[DRY-RUN] Hiçbir değişiklik yapılmadı.\n'
                'Onarımı uygulamak için: --apply ekleyin.\n'
                'ÖNCE production DB yedeği almanız önerilir.'
            ))
            return

        # Apply — atomic.
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n[APPLY] {unprotected_count} koruma + {deactivated_count} re-activation...'
        ))

        with transaction.atomic():
            updated_protect = unprotected_qs.update(is_protected=True)
            updated_active = deactivated_qs.update(is_active=True)

        self.stdout.write(self.style.SUCCESS(
            f'\n✅ {updated_protect} ürün is_protected=True yapıldı.\n'
            f'✅ {updated_active} ürün is_active=True yapıldı.\n'
            f'   Bundan sonra change_status / delete view\'leri bu ürünleri reddedecek.'
        ))
