"""FAZ 39 — CustomerLedger.amount_eur backfill komutu.

Kullanım:
    .venv/bin/python manage.py backfill_ledger_amount_eur
    .venv/bin/python manage.py backfill_ledger_amount_eur --dry-run
    .venv/bin/python manage.py backfill_ledger_amount_eur --customer <uuid>
    .venv/bin/python manage.py backfill_ledger_amount_eur --store <store_id>

Sorun (FAZ 39):
    FAZ 33.3 öncesi yazılmış `CustomerLedger` satırlarında `amount_eur`
    alanı 0 (sıfır) olarak kalmış. `Customers.balance_eur` property ve
    `_balance_eur_subquery()` (customers/views.py) bu alanı topladığı
    için liste ekranlarında TL bakiyesi eksik görünüyor (HS doğru,
    TL yanlış). Detay ekranı da aynı veriyi okuyor.

Çözüm:
    Eksik amount_eur satırlarını `amount_hs × exchange_rate_eur` ile
    doldurmak. `exchange_rate_eur` o satırın yazıldığı anki kuru
    sakladığı için "stored TL" SSOT korunur — anlık piyasa kuru
    kullanılmaz.

    `exchange_rate_eur` da 0 olan satırlar için `Process.hs_rate_sale_eur`
    (varsa) ile doldurma denenir; o da yoksa satır loglanıp atlanır
    (kullanıcı manuel düzeltmeli ya da tarihsel kur değeri girmeli).

APPEND-ONLY Uyum:
    Bu komut, mevcut satırların `amount_eur` ve `exchange_rate_eur`
    alanlarına UPDATE yazar. Normal koşullarda CustomerLedger
    APPEND-ONLY'dir; bu komut yalnızca **eksik veri tamiri** için
    kullanılır (FAZ 33.3 öncesi yazılmış satırlar). Yeni iş akışı
    `LedgerService.write_*` API'leri ile zaten doğru `amount_eur`
    yazıyor.

Test:
    Önce `--dry-run` ile etkilenecek satır sayısını ve örnek
    kayıtları gör; sonra çalıştır.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from apps.customers.models import CustomerLedger, Customers
from apps.process.models import Process


CENT = Decimal('0.01')


class Command(BaseCommand):
    help = (
        'CustomerLedger satırlarında amount_eur=0 olanları '
        'amount_hs × exchange_rate_eur ile doldurur (FAZ 39).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Yazma yapma, sadece etkilenecek satırları raporla.',
        )
        parser.add_argument(
            '--customer',
            type=str,
            default=None,
            help='Sadece bu müşteri UUID için çalıştır.',
        )
        parser.add_argument(
            '--store',
            type=int,
            default=None,
            help='Sadece bu mağaza ID için çalıştır.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='En fazla kaç satır işle (0 = sınırsız).',
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get('dry_run'))
        cust_filter = opts.get('customer')
        store_filter = opts.get('store')
        limit = int(opts.get('limit') or 0)

        # amount_eur == 0 ya da NULL satırlar; amount_hs > 0 olanlar
        # (yani gerçekten "veri eksik", boş satır değil).
        qs = CustomerLedger.objects.filter(
            Q(amount_eur__isnull=True) | Q(amount_eur=Decimal('0')),
            amount_hs__gt=Decimal('0'),
        )
        if cust_filter:
            qs = qs.filter(customer_id=cust_filter)
        if store_filter:
            qs = qs.filter(store_id=store_filter)

        qs = qs.select_related('customer', 'store').order_by('created_on')

        total = qs.count()
        self.stdout.write(self.style.WARNING(
            f'amount_eur boş satır sayısı: {total}'
        ))

        if total == 0:
            self.stdout.write(self.style.SUCCESS('İşlenecek satır yok.'))
            return

        processed = 0
        filled_from_rate = 0
        filled_from_process = 0
        skipped_no_rate = 0
        per_customer = {}

        process_rate_cache = {}

        for entry in qs.iterator():
            if limit and processed >= limit:
                break

            tl_value = Decimal('0')
            rate_used = Decimal('0')
            source = ''

            stored_rate = entry.exchange_rate_eur or Decimal('0')
            if stored_rate > 0:
                rate_used = Decimal(stored_rate)
                tl_value = (Decimal(entry.amount_hs) * rate_used).quantize(
                    CENT, rounding=ROUND_HALF_UP,
                )
                source = 'ledger.exchange_rate_eur'
                filled_from_rate += 1
            else:
                # Fallback: Process.hs_rate_sale_eur tarihi kur olarak
                # kullanılır; bu da round-trip kayıp yapmaz çünkü stored
                # TL alanı zaten doluydu (FAZ 33.2 sonrası).
                pno = entry.process_no or ''
                if pno and pno not in process_rate_cache:
                    proc = (
                        Process.objects.filter(process_no=pno)
                        .values('hs_rate_sale_eur').first()
                    )
                    process_rate_cache[pno] = (
                        Decimal(proc['hs_rate_sale_eur'])
                        if proc and proc.get('hs_rate_sale_eur')
                        else Decimal('0')
                    )
                proc_rate = process_rate_cache.get(pno, Decimal('0'))
                if proc_rate > 0:
                    rate_used = proc_rate
                    tl_value = (Decimal(entry.amount_hs) * rate_used).quantize(
                        CENT, rounding=ROUND_HALF_UP,
                    )
                    source = 'process.hs_rate_sale_eur'
                    filled_from_process += 1
                else:
                    skipped_no_rate += 1
                    self.stdout.write(self.style.WARNING(
                        f'  ATLA id={entry.id} customer={entry.customer_id} '
                        f'process_no={pno} — kur bilgisi yok'
                    ))
                    continue

            cust_key = str(entry.customer_id)
            agg = per_customer.setdefault(
                cust_key,
                {'count': 0, 'tl_total': Decimal('0'), 'name': ''},
            )
            agg['count'] += 1
            agg['tl_total'] += tl_value
            agg['name'] = (
                f"{entry.customer.first_name} {entry.customer.last_name}".strip()
                if entry.customer else cust_key
            )

            if not dry:
                # FAZ 39 — DİKKAT: APPEND-ONLY istisna. Sadece eksik
                # alan tamiri; transaction_type/amount_hs/process_no
                # gibi içerik alanları DEĞİŞTİRİLMEZ.
                CustomerLedger.objects.filter(pk=entry.pk).update(
                    amount_eur=tl_value,
                    exchange_rate_eur=rate_used,
                )

            processed += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Toplam etkilenen satır: {processed}'
        ))
        self.stdout.write(
            f'  ledger.exchange_rate_eur ile dolduruldu: {filled_from_rate}'
        )
        self.stdout.write(
            f'  process.hs_rate_sale_eur ile dolduruldu: {filled_from_process}'
        )
        self.stdout.write(
            f'  kur bilgisi eksik (atlandı):           {skipped_no_rate}'
        )

        self.stdout.write('')
        self.stdout.write('Müşteri bazında özet:')
        for cust_id, info in sorted(
            per_customer.items(),
            key=lambda kv: kv[1]['tl_total'],
            reverse=True,
        )[:25]:
            self.stdout.write(
                f"  {info['name'] or cust_id}: "
                f"{info['count']} satır, +{info['tl_total']:.2f} TL"
            )

        if dry:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — hiçbir kayıt güncellenmedi.'
            ))
        else:
            # Customer.receivable_hs/payable_hs stored cache senkronu
            # gerekmiyor; balance_eur property'i live'dır. Liste
            # endpoint'leri _balance_eur_subquery() üzerinden bu update'i
            # otomatik yansıtır.
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                'amount_eur backfill tamamlandı. Liste/detay TL bakiyeleri '
                'artık doğru görünmeli.'
            ))
