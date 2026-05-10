"""FAZ 27 — Hata 3B backfill yardımcısı.

Kullanım:
    python manage.py sync_customer_balance_cache
    python manage.py sync_customer_balance_cache --dry-run
    python manage.py sync_customer_balance_cache --customer <uuid>

Mevcut müşterilerin legacy `receivable_hs` / `payable_hs` stored
alanlarını canlı `balance_hs` property'sinden türetilen değere
eşitler. Bu, FAZ 27 öncesinde bu alanların güncellenmemiş olmasından
kaynaklanan reconciliation banner drift'ini bir defa için temizler.

Bundan sonra `CustomerLedger.post_save` signal'ı (apps/customers/signals.py)
otomatik senkron tutar.

Append-Only Uyum:
    Bu komut yalnız Customer modelindeki cache alanlarını yazar;
    CustomerLedger satırlarına dokunmaz.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.customers.models import Customers


Q3 = Decimal('0.001')


class Command(BaseCommand):
    help = (
        'Customer.receivable_hs / payable_hs legacy alanlarını '
        'canlı balance_hs property\'sinden türeterek backfill eder.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Yazma yapma, sadece raporla.',
        )
        parser.add_argument(
            '--customer',
            type=str,
            default=None,
            help='Sadece bu UUID için çalıştır.',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        target_id = options.get('customer')

        qs = Customers.objects.all()
        if target_id:
            qs = qs.filter(pk=target_id)

        total = qs.count()
        updated = 0
        skipped = 0
        errors = 0

        self.stdout.write(self.style.NOTICE(
            f'[sync_customer_balance_cache] {total} müşteri taranacak '
            f'(dry-run={dry_run}).'
        ))

        for cust in qs.iterator():
            try:
                balance = cust.balance_hs
                if balance > 0:
                    new_recv = balance.quantize(Q3)
                    new_pay = Decimal('0.000')
                elif balance < 0:
                    new_recv = Decimal('0.000')
                    new_pay = (-balance).quantize(Q3)
                else:
                    new_recv = Decimal('0.000')
                    new_pay = Decimal('0.000')

                if (cust.receivable_hs == new_recv
                        and cust.payable_hs == new_pay):
                    skipped += 1
                    continue

                old_recv = cust.receivable_hs
                old_pay = cust.payable_hs

                if not dry_run:
                    with transaction.atomic():
                        cust.receivable_hs = new_recv
                        cust.payable_hs = new_pay
                        cust.save(update_fields=[
                            'receivable_hs', 'payable_hs',
                        ])

                updated += 1
                self.stdout.write(
                    f'  • {cust.first_name} {cust.last_name} '
                    f'({cust.id}): '
                    f'recv {old_recv} → {new_recv}, '
                    f'pay {old_pay} → {new_pay}'
                )

            except Exception as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(
                    f'  ! {cust.id}: {exc}'
                ))

        self.stdout.write(self.style.SUCCESS(
            f'[sync_customer_balance_cache] Tamamlandı. '
            f'updated={updated}, skipped={skipped}, errors={errors}, '
            f'total={total}, dry_run={dry_run}'
        ))
