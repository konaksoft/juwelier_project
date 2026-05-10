"""
Django Management Command: export_manual_needed
================================================

FAZ 65.1 (2026-05-07) — Otomatik onarilemeyen WAC anomalilerini disa aktarir.

MANUAL_NEEDED kriteri (audit_wac_anomalies.py ile ayni mantik):
  - Products.is_deleted = False  (zombie degildir)
  - stock_gram <= 0 VEYA (WAC / stock_gram) > 1.0  (WAC_OVER_GRAM uygulanamaz)
  - product_mileage <= 0 VEYA product_mileage > 1000  (MILEAGE_DERIVE uygulanamaz)

Kullanim:
    # Varsayilan: calisma dizinine YYYY-MM-DD_manual_needed.csv yazar
    python manage.py export_manual_needed

    # Belirli magaza
    python manage.py export_manual_needed --store <store_uuid>

    # Ozel cikti dosyasi
    python manage.py export_manual_needed --output /tmp/manual_needed.csv

    # Esik degeri ozellestir (default 1.05)
    python manage.py export_manual_needed --threshold 1.1
"""

import csv
import os
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Otomatik onarilemeyen WAC anomalilerini (MANUAL_NEEDED) CSV olarak disa aktarir. '
        'Operator bu urunleri acip dogru milyem/alis fiyatiyla yeniden girmeli.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--store',
            type=str,
            default=None,
            help='Belirli bir magaza UUID si. Bos ise tum magazalar.',
        )
        parser.add_argument(
            '--threshold',
            type=float,
            default=1.05,
            help='WAC esik degeri (HS/gr). Default 1.05.',
        )
        parser.add_argument(
            '--output',
            type=str,
            default=None,
            help='Cikti CSV dosyasi yolu. Default: <calisma_dizini>/YYYY-MM-DD_manual_needed.csv',
        )

    def handle(self, *args, **options):
        from apps.stock_management.models import StockSnapshot

        store_filter = options.get('store')
        threshold = Decimal(str(options.get('threshold') or 1.05))
        output_path = options.get('output') or os.path.join(
            os.getcwd(),
            f'{date.today().isoformat()}_manual_needed.csv',
        )

        qs = (
            StockSnapshot.objects
            .select_related('product', 'product__category', 'store')
            .filter(
                weighted_avg_cost_hs__gt=threshold,
                product__is_deleted=False,
            )
        )
        if store_filter:
            qs = qs.filter(store_id=store_filter)

        rows = []
        for snap in qs.iterator(chunk_size=200):
            product = snap.product
            old_wac = Decimal(str(snap.weighted_avg_cost_hs or 0))
            old_gram = Decimal(str(snap.stock_gram or 0))
            pm = Decimal(str(getattr(product, 'product_mileage', 0) or 0))

            # WAC_OVER_GRAM uygulanabilir mi?
            wac_over_gram_ok = False
            if old_gram > 0:
                candidate = old_wac / old_gram
                wac_over_gram_ok = Decimal('0') < candidate <= Decimal('1.0000')

            # MILEAGE_DERIVE uygulanabilir mi?
            mileage_ok = Decimal('0') < pm <= Decimal('1000')

            if wac_over_gram_ok or mileage_ok:
                continue  # otomatik duzeltilecek, es gec

            # MANUAL_NEEDED — neden duzeltilemiyor acikla
            reasons = []
            if old_gram <= 0:
                reasons.append('stock_gram=0 (WAC_OVER_GRAM uygulanamaz)')
            else:
                candidate = old_wac / old_gram
                if candidate > Decimal('1.0000'):
                    reasons.append(f'WAC/gram={float(candidate):.4f}>1.0 (tersine cevirme imkansiz)')
            if pm <= 0:
                reasons.append('product_mileage=0')
            elif pm > 1000:
                reasons.append(f'product_mileage={float(pm):.0f}>1000 (gecersiz)')

            store = snap.store
            rows.append({
                'snapshot_id': snap.id,
                'product_id': product.id,
                'urun_adi': getattr(product, 'name', '') or '',
                'barkod': getattr(product, 'barcode', '') or '',
                'kategori': (
                    getattr(product.category, 'name', '') if product.category else ''
                ),
                'magaza_id': str(getattr(store, 'id', '')),
                'magaza_adi': getattr(store, 'title', '') or '',
                'stock_gram': float(old_gram),
                'stock_pieces': snap.stock_pieces,
                'mevcut_wac_hs': float(old_wac),
                'product_mileage': float(pm),
                'neden_manuel': ' | '.join(reasons),
            })

        if not rows:
            self.stdout.write(self.style.SUCCESS(
                f'MANUAL_NEEDED kaydi bulunamadi (esik: {threshold} HS/gr).'
            ))
            return

        fieldnames = [
            'snapshot_id', 'product_id', 'urun_adi', 'barkod', 'kategori',
            'magaza_id', 'magaza_adi', 'stock_gram', 'stock_pieces',
            'mevcut_wac_hs', 'product_mileage', 'neden_manuel',
        ]

        with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        self.stdout.write(self.style.SUCCESS(
            f'{len(rows)} MANUAL_NEEDED kaydi disa aktarildi: {output_path}'
        ))

        # Magaza bazli ozet
        store_counts: dict[str, int] = {}
        for row in rows:
            key = f"{row['magaza_id'][:8]}  {row['magaza_adi']}"
            store_counts[key] = store_counts.get(key, 0) + 1

        self.stdout.write('')
        self.stdout.write('Magaza bazli dagilim:')
        for store_key, count in sorted(store_counts.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {store_key:<40} {count:>4} kayit')
