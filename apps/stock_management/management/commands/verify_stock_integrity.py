"""
Django Management Command: verify_stock_integrity
===================================================

StockSnapshot ile StockLedger SUM tutarliligini dogrulamak icin
komut satiri araci.

Kullanim:
    python manage.py verify_stock_integrity
    python manage.py verify_stock_integrity --store <store_id>
    python manage.py verify_stock_integrity --fix
    python manage.py verify_stock_integrity --verbose
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Case, DecimalField, F, Sum, When


class Command(BaseCommand):
    help = (
        'StockSnapshot ile StockLedger SUM tutarliligini dogrular. '
        'Fark varsa raporlar, --fix ile snapshot duzeltmesi yapar.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--store',
            type=str,
            default=None,
            help='Belirli bir magaza ID si (UUID). Bos ise tum magazalar kontrol edilir.',
        )
        parser.add_argument(
            '--fix',
            action='store_true',
            default=False,
            help=(
                'Uyumsuzluk tespit edilirse snapshot i ledger SUM una '
                'gore otomatik duzelt. DIKKAT: Canli sistemde dikkatli kullanin.'
            ),
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            default=False,
            help='Her kontrol edilen kaydi ayrintili goster.',
        )
        parser.add_argument(
            '--tolerance',
            type=str,
            default='0.001',
            help='Gram cinsinden tolerans esigi (varsayilan: 0.001 = 1mg)',
        )

    def handle(self, *args, **options):
        from apps.stock_management.models import StockLedger, StockSnapshot

        store_id = options['store']
        fix_mode = options['fix']
        verbose = options['verbose']
        tolerance = Decimal(options['tolerance'])

        self.stdout.write(self.style.MIGRATE_HEADING(
            '\n=== STOK TUTARLILIK DOGRULAMA ==='
        ))
        self.stdout.write(f'Tolerans: {tolerance}g')
        self.stdout.write(f'Duzeltme modu: {"ACIK" if fix_mode else "KAPALI"}')
        self.stdout.write('')

        # Snapshot queryset
        queryset = StockSnapshot.objects.select_related('product', 'store')
        if store_id:
            queryset = queryset.filter(store_id=store_id)

        checked = 0
        ok_count = 0
        mismatch_count = 0
        fixed_count = 0

        for snap in queryset.iterator(chunk_size=500):
            # Ledger'dan hesaplanmis gercek stok
            agg = StockLedger.objects.filter(
                product=snap.product,
                store=snap.store,
            ).aggregate(
                net_gram=Sum(
                    Case(
                        When(direction='IN', then=F('quantity_gram')),
                        When(direction='OUT', then=-F('quantity_gram')),
                        default=Decimal('0'),
                        output_field=DecimalField(),
                    )
                ),
                total_in=Sum(
                    Case(
                        When(direction='IN', then=F('quantity_gram')),
                        default=Decimal('0'),
                        output_field=DecimalField(),
                    )
                ),
                total_out=Sum(
                    Case(
                        When(direction='OUT', then=F('quantity_gram')),
                        default=Decimal('0'),
                        output_field=DecimalField(),
                    )
                ),
            )

            ledger_gram = agg['net_gram'] or Decimal('0')
            total_in = agg['total_in'] or Decimal('0')
            total_out = agg['total_out'] or Decimal('0')
            snapshot_gram = snap.stock_gram or Decimal('0')
            diff = snapshot_gram - ledger_gram

            checked += 1

            if abs(diff) <= tolerance:
                ok_count += 1
                if verbose:
                    self.stdout.write(
                        f'  OK  | {snap.product.name[:30]:30} | '
                        f'Magaza: {snap.store} | '
                        f'Snapshot: {snapshot_gram}g | '
                        f'Ledger: {ledger_gram}g'
                    )
            else:
                mismatch_count += 1
                self.stdout.write(self.style.ERROR(
                    f'  UYUMSUZ | {snap.product.name[:30]:30} | '
                    f'Magaza: {snap.store} | '
                    f'Snapshot: {snapshot_gram}g | '
                    f'Ledger: {ledger_gram}g | '
                    f'FARK: {diff}g | '
                    f'Giris: {total_in}g | Cikis: {total_out}g'
                ))

                if fix_mode:
                    snap.stock_gram = max(Decimal('0'), ledger_gram)
                    snap.save(update_fields=['stock_gram', 'updated_on'])
                    fixed_count += 1
                    self.stdout.write(self.style.WARNING(
                        f'         -> DUZELTILDI: {snapshot_gram}g -> {snap.stock_gram}g'
                    ))

        # Sonuc ozeti
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('=== SONUC ==='))
        self.stdout.write(f'Kontrol edilen: {checked}')
        self.stdout.write(self.style.SUCCESS(f'Basarili (OK)  : {ok_count}'))

        if mismatch_count > 0:
            self.stdout.write(self.style.ERROR(f'Uyumsuz        : {mismatch_count}'))
            if fix_mode:
                self.stdout.write(self.style.WARNING(f'Duzeltilen      : {fixed_count}'))
            else:
                self.stdout.write(self.style.WARNING(
                    'Duzeltme icin --fix parametresi ile calistirin.'
                ))
        else:
            self.stdout.write(self.style.SUCCESS(
                '\nTum stok kayitlari tutarli. Sorun tespit edilmedi.'
            ))

        if mismatch_count > 0 and not fix_mode:
            raise CommandError(
                f'{mismatch_count} stok tutarsizligi tespit edildi!'
            )
