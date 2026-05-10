"""
Django Management Command: audit_custody_stock_split
=====================================================

FAZ 48.2 + 48.3 — Emanet/Mağaza Stok İzolasyonu (Audit + Backfill).

İKİ MOD:

  1) READ-ONLY (DEFAULT) — `python manage.py audit_custody_stock_split`
     Hiçbir tablo değişmez. Sadece rapor üretir.

  2) APPLY — `python manage.py audit_custody_stock_split --apply`
     Atomik transaction içinde:
       - OK ve DRIFT satırları için:
            custody_gram   = pool
            custody_pieces = pool_pieces
            stock_gram    -= pool
            stock_pieces  -= pool_pieces
       - OVER_STOCK ve NEGATIVE_POOL satırları → ATLANIR + uyarı listesi
     Uygulama sonunda invariant doğrulaması yapılır:
       Σ (eski stock_gram) == Σ (yeni stock_gram + yeni custody_gram)
     Sapma varsa transaction tamamen rollback edilir.

AMAÇ:
    StockSnapshot tablosundaki mevcut tek-havuz yapısını taramak ve her
    (product, store) çifti için EMANET pozisyonunu fiziksel olarak
    custody_gram/custody_pieces alanlarına taşımak.

KOK MANTIK:
    Mevcut sistemde StockService.record_entry(reason=CUSTODY_IN), tüm
    emanet hareketlerini StockSnapshot.stock_gram alanına yazıyor. Mağaza
    stoğu ile emanet stoğu fiziksel olarak ayrışmış değil. FAZ 48.1 ile
    StockSnapshot'a custody_gram/custody_pieces alanları eklendi (default 0).
    FAZ 48.3 ile bu alanlar StockLedger üzerinden hesaplanıp doldurulur.

NET EMANET HESABI (StockLedger üzerinden):
    custody_pool_gram = Σ(CUSTODY_IN, dir=IN, gram)
                      - Σ(CUSTODY_OUT, dir=OUT, gram)
                      - Σ(CUSTODY_2_STK, dir=OUT, gram)

    Pieces için aynı formül.

ÇAPRAZ DOĞRULAMA (CustomerCustodyLedger üzerinden):
    aktif_emanet_gram = Σ(CCL.IN aktif, quantity_gram)
                      - Σ(CCL.OUT/OFFSET/STOCK aktif, quantity_gram)

    İdeal durumda: pool_gram (StockLedger) ≈ aktif_emanet_gram (CCL)
    Sapma varsa "DRIFT" olarak işaretlenir, backfill yine yapılır
    (StockLedger esas alınır).

ANOMALİ TÜRLERİ:
    OK              → Hesaplama temiz; backfill güvenli.
    DRIFT           → CCL ile sapma; backfill matematiksel güvenli.
    NEGATIVE_POOL   → Hesaplanan pool negatif. Backfill atlanır.
    OVER_STOCK      → pool > current_stock. Backfill atlanır.

KULLANIM:
    # Tüm anomalileri raporla (DEFAULT — yazma yok)
    python manage.py audit_custody_stock_split

    # CSV formatında çıktı
    python manage.py audit_custody_stock_split --csv > rapor.csv

    # Sadece anomalileri göster
    python manage.py audit_custody_stock_split --only-anomalies --verbose

    # Belirli mağaza/ürün
    python manage.py audit_custody_stock_split --store <store_uuid>
    python manage.py audit_custody_stock_split --product <product_uuid>

    # YAZMA MODU (FAZ 48.3) — atomik backfill
    python manage.py audit_custody_stock_split --apply

    # Yazma modunda audit log dosyasının yolunu belirt
    python manage.py audit_custody_stock_split --apply --log-file /tmp/faz48_apply.log

    # Yazma modunda özel anomali tölere et (NORMAL DURUMDA KULLANMA)
    python manage.py audit_custody_stock_split --apply --include-drift  (DEFAULT True)
    python manage.py audit_custody_stock_split --apply --no-include-drift

GÜVENLİK:
    - DEFAULT mod READ-ONLY. --apply flag'ini açıkça vermeden yazma yapılmaz.
    - --apply modunda transaction.atomic() içinde çalışır; her hata
      tam rollback ile sonuçlanır.
    - select_for_update() ile satır kilidi alınır (eşzamanlı stok
      güncellemelerine karşı koruma).
    - Invariant assertion başarısız olursa rollback.
    - StockLedger'a hiçbir kayıt yazılmaz (append-only ihlali yok).
    - CheckConstraint'ler (custody_gram >= 0, stock_gram >= 0) son savunma
      hattı olarak çalışır.
"""

import csv
import sys
from datetime import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from apps.stock_management.models import StockSnapshot, StockLedger


# Emanet havuzu için tolerans eşikleri
GRAM_EPSILON = Decimal('0.0005')  # 0.0005 gram altındaki sapmalar OK sayılır
PIECE_EPSILON = 0  # Adet için tolerans yok (tam eşitlik beklenir)

# Backfill için kabul edilen status'ler (DEFAULT)
DEFAULT_APPLY_STATUSES = ('OK', 'DRIFT')


class Command(BaseCommand):
    help = (
        'FAZ 48.2 + 48.3 — Emanet stoğunu mağaza stoğundan ayırmak için '
        'audit (READ-ONLY) ve backfill (--apply) komutu. StockLedger ve '
        'CustomerCustodyLedger üzerinden net emanet pozisyonunu hesaplar; '
        '--apply ile custody_gram/custody_pieces alanlarına taşır. '
        'Anomalili satırlar (OVER_STOCK, NEGATIVE_POOL) otomatik atlanır.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--store',
            type=str,
            default=None,
            help='Sadece belirli mağazayı denetle (UUID)',
        )
        parser.add_argument(
            '--product',
            type=str,
            default=None,
            help='Sadece belirli ürünü denetle (UUID)',
        )
        parser.add_argument(
            '--only-anomalies',
            action='store_true',
            default=False,
            help='OK satırlarını gizle, sadece anomalileri göster',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            default=False,
            help='Her satır için tam hesaplama detayı yazdır',
        )
        parser.add_argument(
            '--csv',
            action='store_true',
            default=False,
            help='Çıktıyı CSV formatında ver (stdout)',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help=(
                'YAZMA MODU. Atomik transaction içinde custody_gram/'
                'custody_pieces alanlarını doldurur ve stock_gram/'
                'stock_pieces alanlarından düşer. Anomalili satırlar atlanır.'
            ),
        )
        parser.add_argument(
            '--include-drift',
            action='store_true',
            default=True,
            help=(
                'DRIFT satırlarını backfill\'e dahil et (DEFAULT True). '
                'StockLedger esas alınır.'
            ),
        )
        parser.add_argument(
            '--no-include-drift',
            action='store_false',
            dest='include_drift',
            help='DRIFT satırlarını da atla (sadece OK satırları işle)',
        )
        parser.add_argument(
            '--log-file',
            type=str,
            default=None,
            help=(
                'YAZMA modunda audit log dosyası yolu. Verilmezse otomatik '
                'olarak /tmp/faz48_apply_<timestamp>.log oluşturulur.'
            ),
        )

    # ------------------------------------------------------------------
    # YARDIMCI: CustomerCustodyLedger'dan aktif emanet hesabı
    # ------------------------------------------------------------------

    def _get_ccl_active_balance(self, *, product_id, store_id):
        """
        Belirli (product, store) için CustomerCustodyLedger'dan aktif
        emanet net bakiyesini döndürür (gram, pieces).
        """
        try:
            from apps.custody.models import CustomerCustodyLedger
        except Exception:
            return None, None

        active_qs = CustomerCustodyLedger.objects.filter(
            product_id=product_id,
            store_id=store_id,
            is_active=True,
            is_deleted=False,
        )

        in_gram = active_qs.filter(custody_type='IN').aggregate(
            s=Sum('quantity_gram')
        )['s'] or Decimal('0.0000')
        in_pieces = active_qs.filter(custody_type='IN').aggregate(
            s=Sum('quantity_piece')
        )['s'] or 0

        out_qs = CustomerCustodyLedger.objects.filter(
            product_id=product_id,
            store_id=store_id,
            is_deleted=False,
            custody_type__in=['OUT', 'OFFSET', 'STOCK'],
        )
        out_gram = out_qs.aggregate(s=Sum('quantity_gram'))['s'] or Decimal('0.0000')
        out_pieces = out_qs.aggregate(s=Sum('quantity_piece'))['s'] or 0

        net_gram = (in_gram or Decimal('0.0000')) - (out_gram or Decimal('0.0000'))
        net_pieces = int(in_pieces or 0) - int(out_pieces or 0)

        if net_gram < Decimal('0.0000'):
            net_gram = Decimal('0.0000')
        if net_pieces < 0:
            net_pieces = 0

        return net_gram.quantize(Decimal('0.0001')), net_pieces

    # ------------------------------------------------------------------
    # YARDIMCI: Tek (product, store) için pool hesapla
    # ------------------------------------------------------------------

    def _compute_pool(self, *, product_id, store_id, ledger_qs):
        """StockLedger üzerinden net emanet havuzunu hesapla."""
        in_gram = ledger_qs.filter(
            product_id=product_id,
            store_id=store_id,
            reason=StockLedger.Reason.CUSTODY_IN,
            direction=StockLedger.Direction.IN,
        ).aggregate(s=Sum('quantity_gram'))['s'] or Decimal('0.0000')

        in_pieces = ledger_qs.filter(
            product_id=product_id,
            store_id=store_id,
            reason=StockLedger.Reason.CUSTODY_IN,
            direction=StockLedger.Direction.IN,
        ).aggregate(s=Sum('quantity_pieces'))['s'] or 0

        out_gram = ledger_qs.filter(
            product_id=product_id,
            store_id=store_id,
            reason=StockLedger.Reason.CUSTODY_OUT,
            direction=StockLedger.Direction.OUT,
        ).aggregate(s=Sum('quantity_gram'))['s'] or Decimal('0.0000')

        out_pieces = ledger_qs.filter(
            product_id=product_id,
            store_id=store_id,
            reason=StockLedger.Reason.CUSTODY_OUT,
            direction=StockLedger.Direction.OUT,
        ).aggregate(s=Sum('quantity_pieces'))['s'] or 0

        transfer_gram = ledger_qs.filter(
            product_id=product_id,
            store_id=store_id,
            reason=StockLedger.Reason.CUSTODY_TO_STOCK,
            direction=StockLedger.Direction.OUT,
        ).aggregate(s=Sum('quantity_gram'))['s'] or Decimal('0.0000')

        transfer_pieces = ledger_qs.filter(
            product_id=product_id,
            store_id=store_id,
            reason=StockLedger.Reason.CUSTODY_TO_STOCK,
            direction=StockLedger.Direction.OUT,
        ).aggregate(s=Sum('quantity_pieces'))['s'] or 0

        pool_gram = (in_gram - out_gram - transfer_gram).quantize(
            Decimal('0.0001')
        )
        pool_pieces = int(in_pieces) - int(out_pieces) - int(transfer_pieces)

        return pool_gram, pool_pieces

    # ------------------------------------------------------------------
    # YARDIMCI: Anomali tespiti ve önerilen değerler
    # ------------------------------------------------------------------

    def _classify(self, *, pool_gram, pool_pieces, current_stock_gram,
                  current_stock_pieces, ccl_gram, ccl_pieces):
        """
        Bir (product, store) için anomali türünü ve önerilen yeni
        stock_gram/custody_gram değerlerini hesaplar.
        """
        status = 'OK'
        notes_parts = []

        if pool_gram < Decimal('0.0000') or pool_pieces < 0:
            status = 'NEGATIVE_POOL'
            notes_parts.append(
                f'Negatif havuz: gram={pool_gram} pieces={pool_pieces}'
            )

        elif (pool_gram > current_stock_gram + GRAM_EPSILON or
              pool_pieces > current_stock_pieces):
            status = 'OVER_STOCK'
            notes_parts.append(
                f'Havuz mevcut stoğu aşıyor: '
                f'havuz_gram={pool_gram} > current_stock_gram={current_stock_gram} '
                f'veya havuz_pieces={pool_pieces} > current_stock_pieces='
                f'{current_stock_pieces}'
            )

        else:
            if ccl_gram is not None:
                gram_drift = abs(pool_gram - ccl_gram)
                pieces_drift = abs(pool_pieces - ccl_pieces)

                if gram_drift > GRAM_EPSILON or pieces_drift > PIECE_EPSILON:
                    status = 'DRIFT'
                    notes_parts.append(
                        f'StockLedger vs CCL sapması: '
                        f'gram_drift={gram_drift} pieces_drift={pieces_drift} '
                        f'(ledger={pool_gram}/{pool_pieces}, '
                        f'ccl={ccl_gram}/{ccl_pieces})'
                    )

        if status in ('NEGATIVE_POOL', 'OVER_STOCK'):
            proposed_new_stock_gram = current_stock_gram
            proposed_new_stock_pieces = current_stock_pieces
            proposed_new_custody_gram = Decimal('0.0000')
            proposed_new_custody_pieces = 0
        else:
            applied_gram = max(Decimal('0.0000'), pool_gram)
            applied_pieces = max(0, pool_pieces)

            proposed_new_stock_gram = (
                current_stock_gram - applied_gram
            ).quantize(Decimal('0.0001'))
            proposed_new_stock_pieces = current_stock_pieces - applied_pieces
            proposed_new_custody_gram = applied_gram
            proposed_new_custody_pieces = applied_pieces

        return (
            status,
            notes_parts,
            proposed_new_stock_gram,
            proposed_new_stock_pieces,
            proposed_new_custody_gram,
            proposed_new_custody_pieces,
        )

    # ------------------------------------------------------------------
    # ANA AKIŞ
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        store_filter = options.get('store')
        product_filter = options.get('product')
        only_anomalies = bool(options.get('only_anomalies'))
        verbose = bool(options.get('verbose'))
        csv_mode = bool(options.get('csv'))
        apply_mode = bool(options.get('apply'))
        include_drift = bool(options.get('include_drift'))
        log_file_arg = options.get('log_file')

        if apply_mode and csv_mode:
            raise CommandError(
                '--apply ve --csv aynı anda kullanılamaz. '
                '--apply çıktısı insan-okur log formatında verilir.'
            )

        # 1) Hangi (product, store) çiftleri için emanet hareketi var?
        custody_reasons = [
            StockLedger.Reason.CUSTODY_IN,
            StockLedger.Reason.CUSTODY_OUT,
            StockLedger.Reason.CUSTODY_TO_STOCK,
        ]

        ledger_qs = StockLedger.objects.filter(reason__in=custody_reasons)
        if store_filter:
            ledger_qs = ledger_qs.filter(store_id=store_filter)
        if product_filter:
            ledger_qs = ledger_qs.filter(product_id=product_filter)

        affected_pairs = list(
            ledger_qs
            .values('product_id', 'store_id')
            .distinct()
            .order_by('store_id', 'product_id')
        )

        total_pairs = len(affected_pairs)
        if total_pairs == 0:
            self.stdout.write(self.style.SUCCESS(
                'Emanet hareketi bulunan StockLedger kaydı yok.'
            ))
            return

        # 2) Mod ayrımı
        if apply_mode:
            self._run_apply_mode(
                affected_pairs=affected_pairs,
                ledger_qs=ledger_qs,
                include_drift=include_drift,
                log_file_arg=log_file_arg,
            )
        else:
            self._run_audit_mode(
                affected_pairs=affected_pairs,
                ledger_qs=ledger_qs,
                only_anomalies=only_anomalies,
                verbose=verbose,
                csv_mode=csv_mode,
            )

    # ------------------------------------------------------------------
    # AUDIT MOD (READ-ONLY)
    # ------------------------------------------------------------------

    def _run_audit_mode(self, *, affected_pairs, ledger_qs, only_anomalies,
                        verbose, csv_mode):
        writer = None
        if csv_mode:
            writer = csv.writer(sys.stdout)
            writer.writerow([
                'store_id', 'product_id', 'product_name',
                'current_stock_gram', 'current_stock_pieces',
                'custody_pool_gram_ledger', 'custody_pool_pieces_ledger',
                'custody_pool_gram_ccl', 'custody_pool_pieces_ccl',
                'proposed_new_stock_gram', 'proposed_new_stock_pieces',
                'proposed_new_custody_gram', 'proposed_new_custody_pieces',
                'status', 'notes',
            ])

        counters = {'OK': 0, 'NEGATIVE_POOL': 0, 'OVER_STOCK': 0, 'DRIFT': 0}
        rows_to_print = []

        for pair in affected_pairs:
            row = self._compute_row(pair=pair, ledger_qs=ledger_qs)
            counters[row['status']] += 1

            if only_anomalies and row['status'] == 'OK':
                continue

            if csv_mode:
                writer.writerow([row[k] for k in [
                    'store_id', 'product_id', 'product_name',
                    'current_stock_gram', 'current_stock_pieces',
                    'custody_pool_gram_ledger', 'custody_pool_pieces_ledger',
                    'custody_pool_gram_ccl', 'custody_pool_pieces_ccl',
                    'proposed_new_stock_gram', 'proposed_new_stock_pieces',
                    'proposed_new_custody_gram', 'proposed_new_custody_pieces',
                    'status', 'notes',
                ]])
            else:
                rows_to_print.append(row)

        if csv_mode:
            return

        # İnsan-okur tablo
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('═' * 78))
        self.stdout.write(self.style.MIGRATE_HEADING(
            'FAZ 48.2 — EMANET / MAĞAZA STOK İZOLASYON AUDİT RAPORU'
        ))
        self.stdout.write(self.style.MIGRATE_HEADING('═' * 78))
        self.stdout.write('')

        for row in rows_to_print:
            self._print_row(row, verbose=verbose)

        self._print_summary(counters, len(affected_pairs))

    # ------------------------------------------------------------------
    # APPLY MOD (YAZMA — FAZ 48.3)
    # ------------------------------------------------------------------

    def _run_apply_mode(self, *, affected_pairs, ledger_qs, include_drift,
                        log_file_arg):
        """
        Atomik transaction içinde backfill uygular.

        Hata durumunda:
          - Tüm değişiklikler rollback olur.
          - StockLedger'a hiçbir kayıt yazılmaz.
          - Audit log dosyası yine de oluşturulur (analiz için).
        """
        # Log dosyası hazırla
        if log_file_arg:
            log_file_path = log_file_arg
        else:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            log_file_path = f'/tmp/faz48_apply_{ts}.log'

        log_lines = []
        log_lines.append('=' * 78)
        log_lines.append(
            f'FAZ 48.3 — Backfill Apply Log | Başlangıç: {datetime.now().isoformat()}'
        )
        log_lines.append('=' * 78)
        log_lines.append('')
        log_lines.append(f'Toplam (product, store) çifti: {len(affected_pairs)}')
        log_lines.append(
            f'DRIFT satırları dahil mi: {"EVET" if include_drift else "HAYIR"}'
        )
        log_lines.append('')

        accepted_statuses = list(DEFAULT_APPLY_STATUSES)
        if not include_drift and 'DRIFT' in accepted_statuses:
            accepted_statuses.remove('DRIFT')

        # 1) Önce tüm satırları sınıflandır (transaction dışında, sadece okuma)
        classified = []
        for pair in affected_pairs:
            row = self._compute_row(pair=pair, ledger_qs=ledger_qs)
            classified.append(row)

        # 2) Uygulanacak ve atlanacak listelerini ayır
        to_apply = [r for r in classified if r['status'] in accepted_statuses]
        to_skip = [r for r in classified if r['status'] not in accepted_statuses]

        log_lines.append(f'Uygulanacak satır sayısı: {len(to_apply)}')
        log_lines.append(f'Atlanacak satır sayısı  : {len(to_skip)}')
        log_lines.append('')

        # 3) Önceki toplamlar (invariant referansı)
        snapshot_ids = []
        for r in to_apply:
            snap = StockSnapshot.objects.filter(
                product_id=r['product_id'],
                store_id=r['store_id'],
            ).first()
            if snap:
                snapshot_ids.append(snap.id)

        pre_total_gram, pre_total_pieces = self._sum_totals(snapshot_ids)
        pre_custody_gram, pre_custody_pieces = self._sum_custody_totals(
            snapshot_ids
        )
        log_lines.append('--- ÖNCEKİ TOPLAMLAR (etkilenen snapshot\'lar) ---')
        log_lines.append(f'Σ stock_gram                  : {pre_total_gram}')
        log_lines.append(f'Σ stock_pieces                : {pre_total_pieces}')
        log_lines.append(f'Σ custody_gram                : {pre_custody_gram}')
        log_lines.append(f'Σ custody_pieces              : {pre_custody_pieces}')
        log_lines.append('')

        # 4) Atomik transaction içinde uygula
        applied_count = 0
        applied_details = []
        try:
            with transaction.atomic():
                for r in to_apply:
                    snap = (
                        StockSnapshot.objects
                        .select_for_update()
                        .filter(
                            product_id=r['product_id'],
                            store_id=r['store_id'],
                        )
                        .first()
                    )
                    if not snap:
                        log_lines.append(
                            f'[SKIP-NO-SNAP] product={r["product_id"]} '
                            f'store={r["store_id"]} — Snapshot bulunamadı'
                        )
                        continue

                    # Backfill öncesi tekrar oku (eşzamanlı değişiklik kontrolü)
                    fresh_stock_gram = snap.stock_gram
                    fresh_stock_pieces = snap.stock_pieces

                    # Pool'u tekrar hesapla (StockLedger'da yeni hareket olmuş
                    # olabilir; select_for_update sadece snapshot'ı kilitler)
                    fresh_pool_gram, fresh_pool_pieces = self._compute_pool(
                        product_id=r['product_id'],
                        store_id=r['store_id'],
                        ledger_qs=ledger_qs,
                    )

                    # İDEMPOTENCY: Bu snapshot için zaten backfill yapıldı mı?
                    # Pool kadar miktarı emanet havuzuna taşımak istiyoruz; ama
                    # custody_gram'da zaten o kadar varsa (yani önceki bir
                    # apply çalıştırması başarılı olduysa) atlıyoruz.
                    # to_move = ne kadar daha taşımalıyım?
                    to_move_gram = (
                        max(Decimal('0.0000'), fresh_pool_gram) - snap.custody_gram
                    ).quantize(Decimal('0.0001'))
                    to_move_pieces = max(0, fresh_pool_pieces) - snap.custody_pieces

                    # Negatif ya da sıfır → zaten taşınmış, geç
                    if to_move_gram <= Decimal('0.0000') and to_move_pieces <= 0:
                        log_lines.append(
                            f'[ALREADY-APPLIED] {r["product_name"]} '
                            f'(product={r["product_id"]}) — custody_gram zaten '
                            f'pool ile eşit veya büyük (cust={snap.custody_gram}, '
                            f'pool={fresh_pool_gram}); atlandı.'
                        )
                        continue

                    # Negatif tarafları sıfıra clamp et (gram OK ama pieces
                    # negatif olabilir veya tersi)
                    apply_gram = max(Decimal('0.0000'), to_move_gram)
                    apply_pieces = max(0, to_move_pieces)

                    new_stock_gram = (
                        fresh_stock_gram - apply_gram
                    ).quantize(Decimal('0.0001'))
                    new_stock_pieces = fresh_stock_pieces - apply_pieces
                    new_custody_gram = (
                        snap.custody_gram + apply_gram
                    ).quantize(Decimal('0.0001'))
                    new_custody_pieces = snap.custody_pieces + apply_pieces

                    # Geriye uyumluluk için eski değişken adlarını koru
                    applied_gram = apply_gram
                    applied_pieces = apply_pieces

                    # Negatif kontrol (CheckConstraint zaten engelleyecek
                    # ama burada açık hata mesajı verelim)
                    if new_stock_gram < Decimal('0.0000') or new_stock_pieces < 0:
                        raise CommandError(
                            f'Backfill negatif stok üretiyor (eşzamanlı '
                            f'değişiklik?): product={r["product_id"]} '
                            f'new_stock_gram={new_stock_gram} '
                            f'new_stock_pieces={new_stock_pieces} — ROLLBACK'
                        )

                    # PER-SATIR İNVARİANT
                    pre_sum = fresh_stock_gram + snap.custody_gram
                    post_sum = new_stock_gram + new_custody_gram
                    if abs(pre_sum - post_sum) > Decimal('0.0001'):
                        raise CommandError(
                            f'Per-satır invariant ihlal edildi: '
                            f'product={r["product_id"]} pre={pre_sum} '
                            f'post={post_sum} — ROLLBACK'
                        )

                    # Atomik UPDATE
                    snap.stock_gram = new_stock_gram
                    snap.stock_pieces = new_stock_pieces
                    snap.custody_gram = new_custody_gram
                    snap.custody_pieces = new_custody_pieces
                    snap.save(update_fields=[
                        'stock_gram', 'stock_pieces',
                        'custody_gram', 'custody_pieces',
                        'updated_on',
                    ])

                    applied_count += 1
                    applied_details.append({
                        'product_id': r['product_id'],
                        'store_id': r['store_id'],
                        'product_name': r['product_name'],
                        'pre_stock_gram': str(fresh_stock_gram),
                        'pre_stock_pieces': fresh_stock_pieces,
                        'pool_gram': str(fresh_pool_gram),
                        'pool_pieces': fresh_pool_pieces,
                        'new_stock_gram': str(new_stock_gram),
                        'new_stock_pieces': new_stock_pieces,
                        'new_custody_gram': str(new_custody_gram),
                        'new_custody_pieces': new_custody_pieces,
                        'status': r['status'],
                    })

                # Tüm satırlar yazıldı; TOPLAM İNVARİANT (ek güvence)
                # Per-satır invariant zaten her döngüde kontrol edildi; bu
                # toplam kontrol ek bir doğrulama katmanıdır.
                post_total_gram, post_total_pieces = self._sum_totals(
                    snapshot_ids
                )
                post_custody_gram, post_custody_pieces = (
                    self._sum_custody_totals(snapshot_ids)
                )

                # Sıfır veri kaybı: (stok + emanet) toplamı korunmalı.
                expected_gram_total = pre_total_gram + pre_custody_gram
                actual_gram_total = post_total_gram + post_custody_gram
                expected_pieces_total = pre_total_pieces + pre_custody_pieces
                actual_pieces_total = post_total_pieces + post_custody_pieces

                if abs(expected_gram_total - actual_gram_total) > Decimal('0.001'):
                    raise CommandError(
                        f'TOPLAM GRAM İNVARİANT İHLALİ: '
                        f'önce(stok+emanet)={expected_gram_total} '
                        f'sonra(stok+emanet)={actual_gram_total} '
                        f'sapma={abs(expected_gram_total - actual_gram_total)} '
                        f'— ROLLBACK'
                    )

                if expected_pieces_total != actual_pieces_total:
                    raise CommandError(
                        f'TOPLAM ADET İNVARİANT İHLALİ: '
                        f'önce(stok+emanet)={expected_pieces_total} '
                        f'sonra(stok+emanet)={actual_pieces_total} '
                        f'sapma={abs(expected_pieces_total - actual_pieces_total)} '
                        f'— ROLLBACK'
                    )

                log_lines.append('--- SONRAKİ TOPLAMLAR ---')
                log_lines.append(f'Σ stock_gram                  : {post_total_gram}')
                log_lines.append(f'Σ stock_pieces                : {post_total_pieces}')
                log_lines.append(f'Σ custody_gram                : {post_custody_gram}')
                log_lines.append(f'Σ custody_pieces              : {post_custody_pieces}')
                log_lines.append('')
                log_lines.append('--- İNVARİANT KONTROLÜ ---')
                log_lines.append(
                    f'gram: önce(stok+emanet)={expected_gram_total} = '
                    f'sonra(stok+emanet)={actual_gram_total} ✓'
                )
                log_lines.append(
                    f'adet: önce(stok+emanet)={expected_pieces_total} = '
                    f'sonra(stok+emanet)={actual_pieces_total} ✓'
                )
                log_lines.append('')

                log_lines.append('--- BACKFILL DETAYI ---')
                for d in applied_details:
                    log_lines.append(
                        f'[APPLIED-{d["status"]}] {d["product_name"]} '
                        f'(product={d["product_id"]}, store={d["store_id"]})'
                    )
                    log_lines.append(
                        f'    Önce : stock_gram={d["pre_stock_gram"]} '
                        f'pieces={d["pre_stock_pieces"]}'
                    )
                    log_lines.append(
                        f'    Pool : gram={d["pool_gram"]} '
                        f'pieces={d["pool_pieces"]}'
                    )
                    log_lines.append(
                        f'    Sonra: stock_gram={d["new_stock_gram"]} '
                        f'pieces={d["new_stock_pieces"]} | '
                        f'custody_gram={d["new_custody_gram"]} '
                        f'pieces={d["new_custody_pieces"]}'
                    )
                log_lines.append('')

        except Exception as exc:
            log_lines.append('')
            log_lines.append('=' * 78)
            log_lines.append(f'!!! HATA — TÜM DEĞİŞİKLİKLER ROLLBACK EDİLDİ !!!')
            log_lines.append(f'Hata: {type(exc).__name__}: {exc}')
            log_lines.append('=' * 78)

            self._write_log_file(log_file_path, log_lines)

            self.stdout.write(self.style.ERROR(
                f'\n!!! BACKFILL BAŞARISIZ — ROLLBACK !!!'
            ))
            self.stdout.write(self.style.ERROR(f'Hata: {exc}'))
            self.stdout.write(self.style.ERROR(
                f'Log: {log_file_path}'
            ))
            raise

        # 5) Atlanacak satırları logla
        if to_skip:
            log_lines.append('--- ATLANAN SATIRLAR (manuel inceleme gerekir) ---')
            for r in to_skip:
                log_lines.append(
                    f'[SKIP-{r["status"]}] {r["product_name"]} '
                    f'(product={r["product_id"]}, store={r["store_id"]})'
                )
                log_lines.append(
                    f'    current_stock_gram={r["current_stock_gram"]} '
                    f'pool_gram={r["custody_pool_gram_ledger"]} '
                    f'ccl_gram={r["custody_pool_gram_ccl"]}'
                )
                if r['notes']:
                    log_lines.append(f'    Not: {r["notes"]}')
            log_lines.append('')

        # 6) Final özet
        log_lines.append('=' * 78)
        log_lines.append('SONUÇ')
        log_lines.append('=' * 78)
        log_lines.append(f'Uygulanan satır : {applied_count}')
        log_lines.append(f'Atlanan satır   : {len(to_skip)}')
        log_lines.append(f'Bitiş           : {datetime.now().isoformat()}')

        self._write_log_file(log_file_path, log_lines)

        # 7) Konsola özet
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('═' * 78))
        self.stdout.write(self.style.MIGRATE_HEADING(
            'FAZ 48.3 — BACKFILL APPLY TAMAMLANDI'
        ))
        self.stdout.write(self.style.MIGRATE_HEADING('═' * 78))
        self.stdout.write(self.style.SUCCESS(
            f'  ✓ Uygulanan satır : {applied_count}'
        ))
        if to_skip:
            self.stdout.write(self.style.WARNING(
                f'  ⚠ Atlanan satır   : {len(to_skip)} (manuel inceleme gerekir)'
            ))
        self.stdout.write(f'  📄 Log dosyası    : {log_file_path}')
        self.stdout.write('')

        # Özetle hangi ürünler atlandı?
        if to_skip:
            self.stdout.write(self.style.WARNING(
                'Atlanan satırların özeti (detay log dosyasında):'
            ))
            for r in to_skip:
                self.stdout.write(
                    f'  - [{r["status"]}] {r["product_name"]} '
                    f'(stok={r["current_stock_gram"]}gr/'
                    f'{r["current_stock_pieces"]}ad, '
                    f'pool={r["custody_pool_gram_ledger"]}gr/'
                    f'{r["custody_pool_pieces_ledger"]}ad, '
                    f'ccl={r["custody_pool_gram_ccl"]}gr/'
                    f'{r["custody_pool_pieces_ccl"]}ad)'
                )
            self.stdout.write('')

    # ------------------------------------------------------------------
    # YARDIMCI: Tek satır verisi üret
    # ------------------------------------------------------------------

    def _compute_row(self, *, pair, ledger_qs):
        """Tek (product, store) çifti için tüm metrikleri hesapla."""
        product_id = pair['product_id']
        store_id = pair['store_id']

        pool_gram, pool_pieces = self._compute_pool(
            product_id=product_id,
            store_id=store_id,
            ledger_qs=ledger_qs,
        )

        ccl_gram, ccl_pieces = self._get_ccl_active_balance(
            product_id=product_id,
            store_id=store_id,
        )

        snap = StockSnapshot.objects.filter(
            product_id=product_id,
            store_id=store_id,
        ).first()

        current_stock_gram = (
            snap.stock_gram if snap else Decimal('0.0000')
        )
        current_stock_pieces = snap.stock_pieces if snap else 0

        (
            status,
            notes_parts,
            new_stock_gram,
            new_stock_pieces,
            new_custody_gram,
            new_custody_pieces,
        ) = self._classify(
            pool_gram=pool_gram,
            pool_pieces=pool_pieces,
            current_stock_gram=current_stock_gram,
            current_stock_pieces=current_stock_pieces,
            ccl_gram=ccl_gram,
            ccl_pieces=ccl_pieces,
        )

        try:
            from apps.products.models import Products
            prod_obj = Products.objects.filter(id=product_id).only(
                'id', 'name'
            ).first()
            product_name = prod_obj.name if prod_obj else '(silinmiş)'
        except Exception:
            product_name = '(okunamadı)'

        return {
            'store_id': str(store_id),
            'product_id': str(product_id),
            'product_name': product_name,
            'current_stock_gram': str(current_stock_gram),
            'current_stock_pieces': current_stock_pieces,
            'custody_pool_gram_ledger': str(pool_gram),
            'custody_pool_pieces_ledger': pool_pieces,
            'custody_pool_gram_ccl': (
                str(ccl_gram) if ccl_gram is not None else 'N/A'
            ),
            'custody_pool_pieces_ccl': (
                ccl_pieces if ccl_pieces is not None else 'N/A'
            ),
            'proposed_new_stock_gram': str(new_stock_gram),
            'proposed_new_stock_pieces': new_stock_pieces,
            'proposed_new_custody_gram': str(new_custody_gram),
            'proposed_new_custody_pieces': new_custody_pieces,
            'status': status,
            'notes': ' | '.join(notes_parts) if notes_parts else '',
        }

    # ------------------------------------------------------------------
    # YARDIMCI: Toplam stok ve emanet hesapları (invariant kontrol)
    # ------------------------------------------------------------------

    def _sum_totals(self, snapshot_ids):
        """Belirli snapshot'lar için Σ stock_gram ve Σ stock_pieces."""
        if not snapshot_ids:
            return Decimal('0.0000'), 0
        agg = StockSnapshot.objects.filter(id__in=snapshot_ids).aggregate(
            g=Sum('stock_gram'),
            p=Sum('stock_pieces'),
        )
        return (
            agg['g'] or Decimal('0.0000'),
            int(agg['p'] or 0),
        )

    def _sum_custody_totals(self, snapshot_ids):
        """Belirli snapshot'lar için Σ custody_gram ve Σ custody_pieces."""
        if not snapshot_ids:
            return Decimal('0.0000'), 0
        agg = StockSnapshot.objects.filter(id__in=snapshot_ids).aggregate(
            g=Sum('custody_gram'),
            p=Sum('custody_pieces'),
        )
        return (
            agg['g'] or Decimal('0.0000'),
            int(agg['p'] or 0),
        )

    # ------------------------------------------------------------------
    # YARDIMCI: Log dosyası yaz
    # ------------------------------------------------------------------

    def _write_log_file(self, path, lines):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
                f.write('\n')
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f'Log dosyası yazılamadı ({path}): {exc}'
            ))

    # ------------------------------------------------------------------
    # YARDIMCI: Özet
    # ------------------------------------------------------------------

    def _print_summary(self, counters, total_pairs):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('─' * 78))
        self.stdout.write(self.style.MIGRATE_HEADING('ÖZET'))
        self.stdout.write(self.style.MIGRATE_HEADING('─' * 78))
        self.stdout.write(
            f'Toplam (product, store) çifti incelendi : {total_pairs}'
        )
        self.stdout.write(self.style.SUCCESS(
            f'  ✓ OK (backfill için güvenli)            : {counters["OK"]}'
        ))
        if counters['DRIFT']:
            self.stdout.write(self.style.WARNING(
                f'  ⚠ DRIFT (StockLedger vs CCL sapma)      : {counters["DRIFT"]}'
            ))
        if counters['NEGATIVE_POOL']:
            self.stdout.write(self.style.ERROR(
                f'  ✗ NEGATIVE_POOL (havuz negatif)         : {counters["NEGATIVE_POOL"]}'
            ))
        if counters['OVER_STOCK']:
            self.stdout.write(self.style.ERROR(
                f'  ✗ OVER_STOCK (havuz > mevcut stok)      : {counters["OVER_STOCK"]}'
            ))
        self.stdout.write('')

        if counters['NEGATIVE_POOL'] or counters['OVER_STOCK']:
            self.stdout.write(self.style.ERROR(
                'UYARI: Anomali bulunan satırlar için backfill --apply '
                'modunda OTOMATİK ATLANIR. El ile inceleyip veriyi düzeltin.'
            ))
        elif counters['DRIFT']:
            self.stdout.write(self.style.WARNING(
                'BİLGİ: DRIFT satırları StockLedger esas alınarak backfill\'e '
                'dahil edilir. CCL sapması genelde silinmiş kayıtlardan veya '
                'legacy parent=None OUT kayıtlarından kaynaklanır.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                'Tüm satırlar temiz. --apply ile backfill çalıştırılabilir.'
            ))
        self.stdout.write('')

    # ------------------------------------------------------------------
    # YARDIMCI: Tek satır insan-okur baskısı
    # ------------------------------------------------------------------

    def _print_row(self, row, *, verbose=False):
        status = row['status']
        if status == 'OK':
            style = self.style.SUCCESS
            icon = '✓'
        elif status == 'DRIFT':
            style = self.style.WARNING
            icon = '⚠'
        else:
            style = self.style.ERROR
            icon = '✗'

        header = (
            f'{icon} [{status}] store={row["store_id"]} '
            f'product={row["product_id"][:8]}… "{row["product_name"]}"'
        )
        self.stdout.write(style(header))

        if verbose or status != 'OK':
            self.stdout.write(
                f'    Mevcut Stok      : gram={row["current_stock_gram"]} '
                f'pieces={row["current_stock_pieces"]}'
            )
            self.stdout.write(
                f'    Havuz (Ledger)   : gram={row["custody_pool_gram_ledger"]} '
                f'pieces={row["custody_pool_pieces_ledger"]}'
            )
            self.stdout.write(
                f'    Havuz (CCL)      : gram={row["custody_pool_gram_ccl"]} '
                f'pieces={row["custody_pool_pieces_ccl"]}'
            )
            self.stdout.write(
                f'    Önerilen Stok    : gram={row["proposed_new_stock_gram"]} '
                f'pieces={row["proposed_new_stock_pieces"]}'
            )
            self.stdout.write(
                f'    Önerilen Emanet  : gram={row["proposed_new_custody_gram"]} '
                f'pieces={row["proposed_new_custody_pieces"]}'
            )
            if row['notes']:
                self.stdout.write(f'    Notlar           : {row["notes"]}')
            self.stdout.write('')
