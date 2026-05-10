"""
Django Management Command: fix_orphan_product_stores
=====================================================

Cross-store leak fix (2026-05-07).

Sorun:
    `apps/products/views.py:product_add` eski versiyonunda yeni özel ürün
    eklenirken `record.store_id = request.POST.get('store_id')` deniyor;
    POST verisi eksik/boş gelirse `store_id=NULL` kaydediliyordu. `get_all`
    ve `get_product_details` view'lerinin `Q(store__isnull=True)` koşulu
    "global sistem ürünleri" (Has Altın, USDTRY, 22 Ayar Gram) için yazılmıştı;
    yan etki olarak NULL'a düşen kullanıcı ürünleri TÜM mağazalardan görünür
    hale geldi (ör. başka kuyumcunun "22 ayar gramm" ürünü Hızlı İşlem'de
    sızıyor).

    Backend artık `record.store = request.user.store` yazıyor (yeni leak
    önlendi). Bu komut, fix'ten ÖNCE yaratılmış sızıntılı kayıtları temizler.

Hedef kayıtlar (güvenli filtre seti):
    - is_protected = False        → sistem ürünleri ASLA dokunulmaz
    - store_id IS NULL            → sızıntılı (orphan) kayıt
    - is_deleted = False          → soft-delete'liler hariç
    - created_by IS NOT NULL      → tasks.py global ürünleri (created_by=None) hariç
    - created_by.store IS NOT NULL → atayacak hedef mağaza var

Onarım:
    Hedef = `created_by.store` (yaratıcı kullanıcının şu anki mağazası)
    Kullanıcı mağazası değişmişse en doğru tahmin budur; per-store ledger
    için historical track yok.

Kullanım:
    # 1) Sadece raporla (DEFAULT — güvenli)
    python manage.py fix_orphan_product_stores

    # 2) Detaylı liste (ürün-ürün)
    python manage.py fix_orphan_product_stores --verbose

    # 3) Onarımı uygula (apply)
    python manage.py fix_orphan_product_stores --apply

Append-only / Veri Bütünlüğü:
    - Products.save() override'ı `full_clean()` çağırır; bu komut atomic
      `Products.objects.filter(...).update(store_id=...)` kullanarak
      validation tetiklenmesini bypass eder (yan etkisiz, sadece FK günceller).
    - StockSnapshot, StockLedger, GoldPurchases gibi bağımlı tablolar zaten
      kendi `store` alanlarını barındırır; bu güncelleme onları etkilemez.
    - Reverse uyarısı: ürün başka mağaza tarafından da kullanılmışsa
      (örn. başka mağaza yanlışlıkla satış yapmış) update sonrası bu satışlar
      hâlâ historical kayıt olarak ledger'da durur — silinmez.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import OuterRef, Subquery

from apps.accounts.models import Users
from apps.products.models import Products


class Command(BaseCommand):
    help = (
        "Sızıntılı (store=NULL) kullanıcı ürünlerini yaratıcının mağazasına atar. "
        "is_protected=True sistem ürünleri korunur."
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

        base_qs = Products.objects.filter(
            is_protected=False,
            store__isnull=True,
            is_deleted=False,
            created_by__isnull=False,
            created_by__store__isnull=False,
        ).select_related('created_by', 'created_by__store', 'category')

        total = base_qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                'Sızıntılı (store=NULL) kullanıcı ürünü bulunamadı. Sistem temiz.'
            ))
            self._report_unfixable()
            return

        # Per-store breakdown
        breakdown = {}
        rows = []
        for p in base_qs:
            target_store = p.created_by.store
            store_label = f"{target_store.name} [{target_store.id}]"
            breakdown[store_label] = breakdown.get(store_label, 0) + 1
            rows.append({
                'id': str(p.id),
                'name': p.name or '(adsız)',
                'category': p.category.name if p.category else '-',
                'created_by': p.created_by.email or p.created_by.username or '?',
                'target_store': target_store.name,
            })

        self.stdout.write(self.style.WARNING(
            f'\n[ORPHAN] {total} sızıntılı ürün bulundu.\n'
        ))
        self.stdout.write('Mağaza bazında dağılım:')
        for store_label, n in sorted(breakdown.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  • {store_label} → {n} ürün')

        if verbose:
            self.stdout.write('\nDetay:')
            for r in rows:
                self.stdout.write(
                    f"  - {r['name']:<30} | {r['category']:<15} "
                    f"| {r['created_by']:<25} → {r['target_store']}"
                )
        else:
            self.stdout.write(f"\nÖrnekler (ilk 10):")
            for r in rows[:10]:
                self.stdout.write(
                    f"  - {r['name']:<30} | {r['category']:<15} → {r['target_store']}"
                )
            if len(rows) > 10:
                self.stdout.write(
                    f"  ... ({len(rows) - 10} ürün daha — tümünü görmek için --verbose)"
                )

        # Unfixable (created_by NULL veya created_by.store NULL) raporu
        self._report_unfixable()

        if not apply_fix:
            self.stdout.write(self.style.WARNING(
                '\n[DRY-RUN] Hiçbir değişiklik yapılmadı.\n'
                'Onarımı uygulamak için: --apply ekleyin.\n'
                'ÖNCE production DB yedeği almanız önerilir.'
            ))
            return

        # Apply
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n[APPLY] {total} ürün güncelleniyor...'
        ))

        # Subquery: her ürün için created_by.store_id'yi getir
        user_store_subq = (
            Users.objects
            .filter(pk=OuterRef('created_by_id'))
            .values('store_id')[:1]
        )

        # Atomic update — Products.save() override'ı tetiklenmez (filter().update()
        # full_clean bypass eder). Sadece store_id FK güncellenir, başka alan etkilenmez.
        with transaction.atomic():
            updated = base_qs.update(store_id=Subquery(user_store_subq))

        self.stdout.write(self.style.SUCCESS(
            f'\n✅ {updated} ürün başarıyla onarıldı.\n'
            f'   Etkilenen ürünler artık sadece kendi mağazalarında görünecek.'
        ))

    def _report_unfixable(self):
        """
        Manuel müdahale gerektiren sızıntılı kayıtları raporla.
        Bunlar otomatik onarılamaz çünkü hedef mağaza belirlenemiyor.
        """
        unfixable_no_creator = Products.objects.filter(
            is_protected=False,
            store__isnull=True,
            is_deleted=False,
            created_by__isnull=True,
        ).count()

        unfixable_creator_no_store = Products.objects.filter(
            is_protected=False,
            store__isnull=True,
            is_deleted=False,
            created_by__isnull=False,
            created_by__store__isnull=True,
        ).count()

        if unfixable_no_creator or unfixable_creator_no_store:
            self.stdout.write(self.style.WARNING(
                '\n[MANUEL MÜDAHALE GEREKLİ]'
            ))
            if unfixable_no_creator:
                self.stdout.write(
                    f'  • {unfixable_no_creator} ürün: created_by=NULL '
                    '(yaratıcı bilinmiyor — legacy/import veya tasks.py global)'
                )
            if unfixable_creator_no_store:
                self.stdout.write(
                    f'  • {unfixable_creator_no_store} ürün: yaratıcının mağazası yok '
                    '(silinmiş kullanıcı veya staff hesap)'
                )
            self.stdout.write(
                '  → Bu kayıtlar otomatik onarılamaz; DB üzerinden manuel '
                'incelenmesi gerekir.'
            )
