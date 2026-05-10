"""
Django Management Command: export_barcoded_wac_report
======================================================

Barkodlu ürünlerin StockSnapshot WAC durumu raporu.

Mimari kurallar (context/68):
  - Products.buy_price_hs  = TOPLAM Has (barkod etiketi değeri) — tasarım gereği, DEĞİŞTİRİLMEZ
  - StockSnapshot.weighted_avg_cost_hs = BİRİM Has/gram (WAC cache) — fix uygulanabilir
  - Tüm mağazalar MALIYET modundadır (etikette buy_price_hs görünür)

Bu komut sadece StockSnapshot WAC'ı analiz eder:
  - WAC Normal  (≤ threshold) → sorun yok
  - WAC ANOMALİ (> threshold) → audit_wac_anomalies --target=snapshot --fix ile düzeltilebilir
  - WAC MANUAL  → otomatik düzeltilemiyor, operator müdahalesi gerekli

buy_price_hs hiçbir koşulda "hatalı" olarak etiketlenmez.

Kullanim:
    python manage.py export_barcoded_wac_report
    python manage.py export_barcoded_wac_report --store <uuid>
    python manage.py export_barcoded_wac_report --output /tmp/rapor.csv
    python manage.py export_barcoded_wac_report --threshold 1.05
"""

import csv
import os
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand

_Q4 = Decimal('0.0001')


class Command(BaseCommand):
    help = 'Barkodlu ürünlerin StockSnapshot WAC durumunu analiz eder, CSV disa aktarir.'

    def add_arguments(self, parser):
        parser.add_argument('--store', type=str, default=None,
                            help='Belirli magaza UUID. Bos ise tum magazalar.')
        parser.add_argument('--threshold', type=float, default=1.05,
                            help='WAC esik degeri HS/gram (default 1.05).')
        parser.add_argument('--output', type=str, default=None,
                            help='CSV dosya yolu. Default: <cwd>/YYYY-MM-DD_barcoded_wac_report.csv')

    def handle(self, *args, **options):
        from apps.products.models import Products
        from apps.stock_management.models import StockSnapshot

        store_filter = options.get('store')
        threshold = Decimal(str(options.get('threshold') or 1.05))
        output_path = options.get('output') or os.path.join(
            os.getcwd(),
            f'{date.today().isoformat()}_barcoded_wac_report.csv',
        )

        # Barkodlu aktif ürünler
        products_qs = (
            Products.objects
            .select_related('store', 'category')
            .filter(is_deleted=False, is_currency=False)
            .exclude(barcode__isnull=True)
            .exclude(barcode__exact='')
            .order_by('store_id', 'id')
        )
        if store_filter:
            products_qs = products_qs.filter(store_id=store_filter)

        # StockSnapshot'ları tek sorguda çek
        snap_qs = StockSnapshot.objects.filter(
            product__is_deleted=False,
            product__is_currency=False,
        ).exclude(product__barcode__isnull=True).exclude(product__barcode__exact='')
        if store_filter:
            snap_qs = snap_qs.filter(store_id=store_filter)

        snap_map: dict[int, StockSnapshot] = {
            s.product_id: s for s in snap_qs.only(
                'product_id', 'weighted_avg_cost_hs',
                'stock_gram', 'stock_pieces',
            )
        }

        # Mağaza bazlı istatistik
        store_stats: dict[str, dict] = defaultdict(lambda: {
            'magaza_adi': '',
            'normal': 0,
            'anomali_otomatik': 0,
            'anomali_manual': 0,
            'snapshot_yok': 0,
        })

        rows = []

        for prod in products_qs.iterator(chunk_size=500):
            sid = str(prod.store_id) if prod.store_id else ''
            magaza_adi = getattr(prod.store, 'title', '') or ''
            store_stats[sid]['magaza_adi'] = magaza_adi

            gram     = Decimal(str(prod.gram or 0))
            pm       = Decimal(str(prod.product_mileage or 0))
            lm       = Decimal(str(getattr(prod, 'labor_mileage', 0) or 0))
            pl       = Decimal(str(getattr(prod, 'piece_labor', 0) or 0))
            buy_hs   = Decimal(str(prod.buy_price_hs or 0))   # TOPLAM — barkod etiketi

            # Beklenen toplam maliyet (formül: JS ile aynı)
            beklenen_toplam = ((pm + lm) / 1000 * gram + pl).quantize(_Q4) if gram > 0 else Decimal('0')

            snap = snap_map.get(prod.id)

            if snap is None:
                wac_durum        = 'SNAPSHOT YOK'
                mevcut_wac       = Decimal('0')
                stok_gram        = Decimal('0')
                stok_adet        = 0
                wac_fix_sonrasi  = Decimal('0')
                fix_aksiyon      = '-'
                store_stats[sid]['snapshot_yok'] += 1
            else:
                mevcut_wac = Decimal(str(snap.weighted_avg_cost_hs or 0))
                stok_gram  = Decimal(str(snap.stock_gram or 0))
                stok_adet  = snap.stock_pieces or 0

                if mevcut_wac <= threshold:
                    wac_durum       = 'Normal'
                    wac_fix_sonrasi = mevcut_wac
                    fix_aksiyon     = '-'
                    store_stats[sid]['normal'] += 1
                else:
                    # WAC_OVER_GRAM denenebilir mi?
                    candidate = None
                    if stok_gram > 0:
                        candidate = (mevcut_wac / stok_gram).quantize(_Q4)

                    if candidate is not None and Decimal('0') < candidate <= Decimal('1.0000'):
                        wac_durum       = 'ANOMALİ — OTOMATİK DÜZELTİLEBİLİR'
                        wac_fix_sonrasi = candidate
                        fix_aksiyon     = 'WAC/gram tersine çevirme'
                        store_stats[sid]['anomali_otomatik'] += 1
                    elif Decimal('0') < pm <= Decimal('1000'):
                        wac_durum       = 'ANOMALİ — OTOMATİK DÜZELTİLEBİLİR'
                        wac_fix_sonrasi = (pm / Decimal('1000')).quantize(_Q4)
                        fix_aksiyon     = 'Milyemden türetme'
                        store_stats[sid]['anomali_otomatik'] += 1
                    else:
                        wac_durum       = 'ANOMALİ — MANUEL GEREKLİ'
                        wac_fix_sonrasi = Decimal('0')
                        fix_aksiyon     = 'Operator müdahalesi'
                        store_stats[sid]['anomali_manual'] += 1

            # Dashboard'un hesapladığı toplam stok HAS (stok_gram × WAC)
            dashboard_has_mevcut     = (stok_gram * mevcut_wac).quantize(_Q4)
            dashboard_has_fix_sonrasi = (stok_gram * wac_fix_sonrasi).quantize(_Q4)

            rows.append({
                'wac_durum'                : wac_durum,
                'product_id'               : prod.id,
                'barkod'                   : prod.barcode or '',
                'urun_adi'                 : (prod.name or '')[:40],
                'kategori'                 : (prod.category.name if prod.category else ''),
                'magaza_id'                : sid[:8],
                'magaza_adi'               : magaza_adi,
                'gram'                     : float(gram),
                'urun_milyemi'             : float(pm),
                'iscilik_milyemi'          : float(lm),
                'adetli_iscilik_tl'        : float(pl),
                # ── Barkod etiketi (buy_price_hs) — TOPLAM Has — hiçbir koşulda değişmez
                'barkod_etiketi_toplam_has': float(buy_hs),
                # ── Dashboard (snapshot WAC = gram başı oran) ────────────────────────────
                # WAC, stock_gram ile çarpılarak toplam stok HAS hesaplanır.
                # Anomali varsa: wac_gram_basi_mevcut TOPLAM gibi yazılmış → dashboard şişiyor.
                # Fix sonrası: wac_gram_basi_duzeltilmis × stok_gram = barkod etiketi ile aynı toplam.
                'wac_gram_basi_mevcut'     : float(mevcut_wac),
                'wac_gram_basi_duzeltilmis': float(wac_fix_sonrasi),
                'fix_aksiyon'              : fix_aksiyon,
                # ── Dashboard HAS karşılaştırması ────────────────────────────────────────
                # Bu ürün için dashboard ŞUAN kaç HAS sayıyor vs fix sonrası kaç sayar.
                'dashboard_has_simdi'      : float(dashboard_has_mevcut),
                'dashboard_has_duzeltilmis': float(dashboard_has_fix_sonrasi),
                'stok_gram'                : float(stok_gram),
                'stok_adet'                : stok_adet,
            })

        if not rows:
            self.stdout.write(self.style.SUCCESS('Barkodlu ürün bulunamadı.'))
            return

        fieldnames = [
            'wac_durum',
            'product_id', 'barkod', 'urun_adi', 'kategori',
            'magaza_id', 'magaza_adi',
            'gram', 'urun_milyemi', 'iscilik_milyemi', 'adetli_iscilik_tl',
            # Barkod etiketi — değişmez
            'barkod_etiketi_toplam_has',
            # WAC (gram başı oran) — fix bu değeri düzeltir
            'wac_gram_basi_mevcut', 'wac_gram_basi_duzeltilmis', 'fix_aksiyon',
            # Dashboard'da bu ürün kaç HAS sayılıyor (şimdi vs fix sonrası)
            'dashboard_has_simdi', 'dashboard_has_duzeltilmis',
            'stok_gram', 'stok_adet',
        ]

        # ANOMALİ satırları üste gelsin
        rows.sort(key=lambda r: (
            0 if 'ANOMALİ' in r['wac_durum'] else
            1 if r['wac_durum'] == 'SNAPSHOT YOK' else 2
        ))

        with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Terminal özet
        toplam = len(rows)
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('MAĞAZA BAZLI ÖZET'))
        self.stdout.write(
            f'{"Mağaza":<32} {"Normal":>8} {"Otomatik":>10} {"Manuel":>8} {"Snap Yok":>10}'
        )
        self.stdout.write('-' * 72)
        for sid, s in sorted(store_stats.items(), key=lambda x: -(x[1]['anomali_otomatik'] + x[1]['anomali_manual'])):
            self.stdout.write(
                f'{(s["magaza_adi"] or sid[:8])[:32]:<32} '
                f'{s["normal"]:>8} {s["anomali_otomatik"]:>10} '
                f'{s["anomali_manual"]:>8} {s["snapshot_yok"]:>10}'
            )
        self.stdout.write('-' * 72)

        total_normal   = sum(s['normal']           for s in store_stats.values())
        total_otomatik = sum(s['anomali_otomatik'] for s in store_stats.values())
        total_manuel   = sum(s['anomali_manual']   for s in store_stats.values())
        total_yok      = sum(s['snapshot_yok']     for s in store_stats.values())

        self.stdout.write(
            f'{"TOPLAM":<32} {total_normal:>8} {total_otomatik:>10} '
            f'{total_manuel:>8} {total_yok:>10}'
        )
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('GENEL ÖZET'))
        self.stdout.write(f'  Toplam barkodlu ürün             : {toplam}')
        self.stdout.write(f'  Snapshot WAC Normal              : {total_normal}')
        self.stdout.write(f'  Snapshot WAC ANOMALİ — Otomatik  : {total_otomatik}')
        self.stdout.write(f'  Snapshot WAC ANOMALİ — Manuel    : {total_manuel}')
        self.stdout.write(f'  Snapshot Yok                     : {total_yok}')
        self.stdout.write('')
        self.stdout.write('  NOT: etiket_maliyet_has (buy_price_hs) = TOPLAM Has — barkod etiketi')
        self.stdout.write('       Bu değer DEĞİŞTİRİLMEZ. Sadece snapshot_wac fix uygulanabilir.')
        self.stdout.write('')
        if total_otomatik > 0:
            self.stdout.write(self.style.WARNING(
                f'  Fix için: python manage.py audit_wac_anomalies '
                f'--target=snapshot --store <uuid> --fix'
            ))
        self.stdout.write(f'  CSV: {output_path}')
