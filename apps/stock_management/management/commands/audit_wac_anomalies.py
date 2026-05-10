"""
Django Management Command: audit_wac_anomalies
================================================

FAZ 34 (2026-05-01) — Dashboard WAC anomalileri tespit ve onarim araci.

Saf altin 1.000 HS/gr (24 ayar) ust sinirini asan StockSnapshot
weighted_avg_cost_hs degerlerini bulur ve kullanici onayiyla onarir.

Kok neden:
    apps/bracelets/views.py:bracelet_add() icinde
    `buy_price_hs_per_gram = product_mileage / 1000` formulu, milyem
    alanina yanlislikla TL/gram fiyati (orn. 10275, 13731) girilince
    StockSnapshot.weighted_avg_cost_hs e 10.275, 13.731 gibi imkansiz
    degerler yaziyordu. Adim 3 (FAZ 34) ile bracelet_add a milyem<=1000
    guard i eklendi; bu komut MEVCUT bozuk verileri temizler.

Kullanim:
    # 1) Sadece raporla (default — guvenli, hicbir sey degistirmez)
    python manage.py audit_wac_anomalies

    # 2) Belirli magaza (ONERILEN: her magaza icin ayri ayri calistirin)
    python manage.py audit_wac_anomalies --store <store_uuid>

    # 3) Esik degeri ozellestir (default 1.05 — kucuk yuvarlama tolaransi)
    python manage.py audit_wac_anomalies --threshold 1.0

    # 4) Onar — silinmis urunlerin snapshot larini sifirla, aktif urunlerde
    #    products.product_mileage den dogru WAC i hesapla
    python manage.py audit_wac_anomalies --fix

    # 5) Detayli cikti
    python manage.py audit_wac_anomalies --verbose

    # 6) FAZ 65.1 / 65.2 — Hedef secimi:
    #    --target=snapshot : StockSnapshot.weighted_avg_cost_hs taramasi
    #    --target=products : Products.buy_price_hs (legacy total) taramasi
    #    --target=phantom  : FAZ 65.2 phantom stock_gram (satilan barkodlu urunler)
    #    --target=both     : snapshot + products (legacy compat, default)
    #    --target=all      : snapshot + products + phantom (full cleanup)
    #
    #    Restore edilmis legacy veride one cikan akis:
    #      python manage.py audit_wac_anomalies --store <uuid> --target=all
    #      # Once dry-run inceleyin, sonra:
    #      python manage.py audit_wac_anomalies --store <uuid> --target=all --fix

Onarim Stratejisi (asama sirali — ilk uygulanabilir secilir):
    Her anomali snapshot icin:
      1) product.is_deleted=True ise -> ZOMBIE_RESET
            stock_gram=0, stock_pieces=0, WAC=0
      2) WAC_OVER_GRAM (gold_purchases bug fix kok neden):
            apps/gold_purchases/views.py:763 te eskiden form'dan gelen
            TOPLAM Has degeri (orn. 15gr x 0.685 = 10.275) yanlislikla
            dogrudan unit_cost_hs olarak StockService.record_entry'e
            gonderiliyordu; record_entry WAC formulu gram bazli oldugundan
            WAC = totalHas degerine esitleniyordu. Tersine cevirme:
                gercek_birim_WAC = bozuk_WAC / stock_gram
            Eger bu sonuc 0 < x <= 1.0 araligindaysa kabul edilir; aksi
            halde bir sonraki stratejiye dusulur.
      3) MILEAGE_DERIVE (bracelet_add havuz / 1 gram tek ayar):
            product_mileage 0<m<=1000 ise weighted_avg_cost_hs = m/1000.
            Yalin altin milyemine dayanir; iscilik milyemi yok sayilir
            (cogu zaman hata payi makul).
      4) MANUAL_NEEDED:
            Hicbir strateji uygulanamadi -> WAC=0 yazilir, operator
            urunu acip dogru milyem/alis fiyatiyla yeniden girmeli.

    StockLedger'a hicbir kayit yazilmaz — append-only kuralina sadik kalmak
    icin yalniz cache tablosu (StockSnapshot) duzeltilir.

Cikti:
    Her anomali bir satir; raporda product_id, store_id, name, current_wac,
    fixed_wac, action verilir. Sonunda ozet tablo basilir.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = (
        'Dashboard WAC anomalileri (weighted_avg_cost_hs > esik) tespit eder. '
        '--fix ile silinmis urunlerin snapshot larini sifirlar ve aktif '
        'urunlerde product_mileage uzerinden makul WAC hesaplar.'
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
            help='WAC esik degeri (HS/gr). Default 1.05 (1.0 saf altin + 5%% tolerans).',
        )
        parser.add_argument(
            '--fix',
            action='store_true',
            default=False,
            help='Anomalileri otomatik onar. DIKKAT: cache tablolari (StockSnapshot, Products.buy_price_*) guncellenir. StockLedger ASLA mutate edilmez.',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            default=False,
            help='Her anomali kaydini ayrintili yaz.',
        )
        parser.add_argument(
            '--target',
            type=str,
            default='both',
            choices=['snapshot', 'products', 'phantom', 'both', 'all'],
            help=(
                'Hangi cache tablosu taransin? '
                'snapshot = StockSnapshot WAC, '
                'products = Products.buy_price_hs (FAZ 65.1 legacy total), '
                'phantom = StockSnapshot phantom stock_gram (FAZ 65.2 satilan barkodlu), '
                'both = snapshot + products (default, legacy compat), '
                'all = snapshot + products + phantom (full cleanup).'
            ),
        )

    def handle(self, *args, **options):
        from apps.stock_management.models import StockSnapshot

        store_filter = options.get('store')
        threshold = Decimal(str(options.get('threshold') or 1.05))
        do_fix = bool(options.get('fix'))
        verbose = bool(options.get('verbose'))
        target = (options.get('target') or 'both').lower()

        # Magaza-basli calistirma uyarisi
        if not store_filter:
            self.stdout.write(self.style.WARNING(
                'UYARI: --store argumani verilmedi. TUM magazalar taranacak.\n'
                'Onerilen: her magaza icin ayri ayri --store <uuid> ile calistirin.'
            ))
            self.stdout.write('')

        # ========================================================
        # FAZ 65.1 — Products.buy_price_hs taramasi (legacy total)
        # ========================================================
        # Eski sistem (FAZ 34 oncesi) Products.buy_price_hs alanini TOPLAM HS
        # tutuyordu. Restore edilmis veride veya hic guncellenmemis kayitlarda
        # bu legacy degerler hala var olabilir. Snapshot self-heal ve perakende
        # akislari bu alani BIRIM bekledigi icin 1.05 ustu degerler kirli.
        if target in ('products', 'both', 'all'):
            products_fixed = self._audit_products(
                store_filter=store_filter,
                threshold=threshold,
                do_fix=do_fix,
                verbose=verbose,
            )
        else:
            products_fixed = 0

        # ========================================================
        # FAZ 65.2 — Phantom stock_gram taramasi (satilan barkodlu)
        # ========================================================
        # Barkodli parca satislarinda (FAZ 42) Process.gram=0 oldugu icin
        # record_exit'te quantity_gram=0 -> snapshot.stock_gram azalmaz;
        # stock_pieces 0'a duser ama stock_gram=product.gram olarak kalir.
        # Dashboard "Fiziksel Stok HAS" satilmis urunleri saymaya devam eder.
        # FAZ 65.2 record_exit fix tarihinden once satilmis urunlerin phantom
        # stock_gram degerlerini sifirlar (StockLedger ASLA mutate edilmez).
        if target in ('phantom', 'all'):
            phantom_fixed = self._audit_phantom_stock_gram(
                store_filter=store_filter,
                do_fix=do_fix,
                verbose=verbose,
            )
        else:
            phantom_fixed = 0

        # ========================================================
        # Mevcut StockSnapshot taramasi (FAZ 34)
        # ========================================================
        if target not in ('snapshot', 'both', 'all'):
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                f'Tamamlandi (target={target}). '
                f'Products: {products_fixed}, Phantom: {phantom_fixed}'
            ))
            return

        qs = (
            StockSnapshot.objects
            .select_related('product', 'store')
            .filter(weighted_avg_cost_hs__gt=threshold)
        )
        if store_filter:
            qs = qs.filter(store_id=store_filter)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                f'StockSnapshot anomali bulunamadi (esik: {threshold} HS/gr).'
            ))
            if target == 'both':
                self.stdout.write(self.style.SUCCESS(
                    f'Onarilan Products kaydi: {products_fixed}'
                ))
            return

        self.stdout.write(self.style.WARNING(
            f'{total} anomali snapshot tespit edildi (WAC > {threshold} HS/gr).'
        ))
        self.stdout.write('')

        # Sayaclar
        zombie_count = 0       # is_deleted=True olan urunler
        wac_over_gram = 0      # gold_purchases TOTAL/gram tersine cevirme
        mileage_derive = 0     # product_mileage / 1000
        manual_needed = 0      # operator mudahalesi gerekiyor
        fixed = 0

        # Tablo basligi
        if verbose or do_fix:
            self.stdout.write(
                f'{"Snapshot":>8} | {"Product":<36} | {"Magaza":<10} | '
                f'{"Gram":>10} | {"Eski WAC":>10} | {"Yeni WAC":>10} | Aksiyon'
            )
            self.stdout.write('-' * 120)

        for snap in qs.iterator(chunk_size=200):
            product = snap.product
            store = snap.store
            old_wac = Decimal(str(snap.weighted_avg_cost_hs or 0))
            old_wac_tl = Decimal(str(snap.weighted_avg_cost_eur or 0))
            old_gram = Decimal(str(snap.stock_gram or 0))
            is_deleted = bool(getattr(product, 'is_deleted', False))

            # Onarim degerlerini hesapla — siralu deneme
            new_wac_tl = Decimal('0.00')
            new_gram = old_gram
            new_pieces = snap.stock_pieces

            if is_deleted:
                action = 'ZOMBIE_RESET'
                new_wac = Decimal('0.0000')
                new_wac_tl = Decimal('0.00')
                new_gram = Decimal('0.0000')
                new_pieces = 0
                zombie_count += 1
            else:
                # 1) WAC_OVER_GRAM (kok neden — gold_purchases formundan
                #    gelen TOPLAM Has yanlislikla unit_cost_hs olunca
                #    WAC = total_has olur; tersine: gercek_unit = WAC/gram).
                _candidate_unit = None
                if old_gram > 0:
                    _candidate_unit = (old_wac / old_gram).quantize(Decimal('0.0001'))

                pm = Decimal(str(getattr(product, 'product_mileage', 0) or 0))

                if _candidate_unit is not None and Decimal('0') < _candidate_unit <= Decimal('1.0000'):
                    action = 'WAC_OVER_GRAM'
                    new_wac = _candidate_unit
                    wac_over_gram += 1
                elif Decimal('0') < pm <= Decimal('1000'):
                    action = 'MILEAGE_DERIVE'
                    new_wac = (pm / Decimal('1000')).quantize(Decimal('0.0001'))
                    mileage_derive += 1
                else:
                    action = 'MANUAL_NEEDED'
                    new_wac = Decimal('0.0000')
                    manual_needed += 1

            # Raporla
            if verbose or do_fix:
                pname = (getattr(product, 'name', '') or '')[:34]
                store_id_short = str(getattr(store, 'id', ''))[:8]
                self.stdout.write(
                    f'{snap.id:>8} | {pname:<36} | {store_id_short:<10} | '
                    f'{float(old_gram):>10.4f} | '
                    f'{float(old_wac):>10.4f} | {float(new_wac):>10.4f} | {action}'
                )

            # Onarim uygula
            if do_fix:
                try:
                    with transaction.atomic():
                        # Snapshot u select_for_update ile kilitli al ve guncelle
                        from apps.stock_management.models import StockSnapshot as SS
                        locked = SS.objects.select_for_update().get(pk=snap.pk)
                        locked.weighted_avg_cost_hs = new_wac
                        locked.weighted_avg_cost_eur = new_wac_tl
                        if action == 'ZOMBIE_RESET':
                            locked.stock_gram = new_gram
                            locked.stock_pieces = new_pieces
                            locked.save(update_fields=[
                                'weighted_avg_cost_hs', 'weighted_avg_cost_eur',
                                'stock_gram', 'stock_pieces', 'updated_on',
                            ])
                        else:
                            locked.save(update_fields=[
                                'weighted_avg_cost_hs', 'weighted_avg_cost_eur',
                                'updated_on',
                            ])
                    fixed += 1
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(
                        f'  -> Onarim BASARISIZ snapshot_id={snap.pk}: {exc}'
                    ))

        # Ozet
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('OZET'))
        self.stdout.write(f'  Toplam anomali              : {total}')
        self.stdout.write(f'  Silinmis urun (zombie)      : {zombie_count}')
        self.stdout.write(f'  WAC/gram tersine cevirme    : {wac_over_gram}')
        self.stdout.write(f'  Milyemden turetilebilir     : {mileage_derive}')
        self.stdout.write(f'  Manuel mudahale gerekli     : {manual_needed}')
        if do_fix:
            self.stdout.write(self.style.SUCCESS(f'  Onarilan kayit           : {fixed}'))
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Onarim sonrasi Dashboard cache i bir sonraki istek de '
                'otomatik yenilenir (delete_single ve stok hareketleri '
                'cache invalidator i tetikler). Hemen gormek icin:'
                '\n  /dashboard/assets-v2/?refresh=1'
            ))
        else:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Onarim icin --fix bayragi ile tekrar calistirin.'
            ))

        if manual_needed > 0:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'Dikkat: {manual_needed} kayitta product_mileage de bozuk '
                f'(0 veya >1000). Bu urunler operator tarafindan acilip '
                f'dogru milyem ve alis fiyatiyla yeniden girilmeli.'
            ))

        if target in ('both', 'all'):
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING(f'GENEL OZET (target={target})'))
            self.stdout.write(f'  Products onarilan : {products_fixed}')
            self.stdout.write(f'  Snapshot onarilan : {fixed if do_fix else 0}')
            if target == 'all':
                self.stdout.write(f'  Phantom onarilan  : {phantom_fixed}')

    # ============================================================
    # FAZ 65.1 — Products.buy_price_hs Legacy Total Tarama Helper'i
    # ============================================================
    def _audit_products(self, store_filter, threshold, do_fix, verbose):
        """
        Products.buy_price_hs > threshold olan satirlari tespit eder.

        Restore edilmis veya FAZ 34 oncesi gold_purchases formundan giren
        urunlerde buy_price_hs hala TOPLAM HS tutuyor olabilir. Yeni sistem
        bu alani BIRIM bekliyor. 1.05 ustu degeri product.gram'a bolerek
        normalize eder.

        StockLedger'a hicbir kayit yazilmaz (APPEND-ONLY). Sadece Products
        cache alanlari (buy_price_hs, buy_price_eur) guncellenir.

        Returns:
            int: Onarilan kayit sayisi (do_fix=False ise 0).
        """
        from apps.products.models import Products

        self.stdout.write(self.style.MIGRATE_HEADING(
            'FAZ 65.1 — Products.buy_price_hs Legacy Total Taramasi'
        ))

        qs = Products.objects.filter(
            is_deleted=False,
            buy_price_hs__gt=threshold,
            gram__gt=Decimal('0'),
        ).select_related('store')
        if store_filter:
            qs = qs.filter(store_id=store_filter)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                f'Products anomali bulunamadi (buy_price_hs > {threshold}, gram > 0).'
            ))
            self.stdout.write('')
            return 0

        self.stdout.write(self.style.WARNING(
            f'{total} Products kaydi tespit edildi (buy_price_hs > {threshold}).'
        ))

        if verbose or do_fix:
            self.stdout.write(
                f'{"Product":<36} | {"Magaza":<10} | {"Gram":>8} | '
                f'{"Eski buy_hs":>12} | {"Yeni buy_hs":>12} | {"Eski buy_tl":>14} | {"Yeni buy_tl":>14}'
            )
            self.stdout.write('-' * 130)

        fixed = 0
        for prod in qs.iterator(chunk_size=200):
            old_buy_hs = Decimal(str(prod.buy_price_hs or 0))
            old_buy_tl = Decimal(str(prod.buy_price_eur or 0))
            gram_val = Decimal(str(prod.gram or 0))

            if gram_val <= Decimal('0'):
                continue  # gram > 0 filtresine ragmen guvenlik

            new_buy_hs = (old_buy_hs / gram_val).quantize(
                Decimal('0.0001'), rounding=ROUND_HALF_UP
            )
            new_buy_tl = old_buy_tl
            if old_buy_tl > Decimal('0'):
                new_buy_tl = (old_buy_tl / gram_val).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )

            if verbose or do_fix:
                pname = (getattr(prod, 'name', '') or '')[:34]
                store_id_short = str(getattr(prod.store, 'id', ''))[:8] if prod.store else '-'
                self.stdout.write(
                    f'{pname:<36} | {store_id_short:<10} | {float(gram_val):>8.2f} | '
                    f'{float(old_buy_hs):>12.4f} | {float(new_buy_hs):>12.4f} | '
                    f'{float(old_buy_tl):>14.2f} | {float(new_buy_tl):>14.2f}'
                )

            if do_fix:
                try:
                    with transaction.atomic():
                        # Products.objects.filter(...).update(...) ile atomik:
                        # Products.save() override'i full_clean tetikleyerek
                        # negatif gram gibi legacy alanlarda ValidationError
                        # firlatabiliyor (UAT REGRESYON FIX patterni).
                        Products.objects.filter(pk=prod.pk).update(
                            buy_price_hs=new_buy_hs,
                            buy_price_eur=new_buy_tl,
                        )
                    fixed += 1
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(
                        f'  -> Onarim BASARISIZ product_id={prod.pk}: {exc}'
                    ))

        self.stdout.write('')
        if do_fix:
            self.stdout.write(self.style.SUCCESS(
                f'Products onarim tamamlandi: {fixed} / {total} kayit.'
            ))
            self.stdout.write(self.style.WARNING(
                'Not: StockSnapshot kayitlari etkilenmedi. Onlari da temizlemek '
                'icin --target=all veya --target=snapshot ile calistirin.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                'Onarim icin --fix bayragi ile tekrar calistirin.'
            ))
        self.stdout.write('')
        return fixed

    # ============================================================
    # FAZ 65.2 — Phantom stock_gram Tarama Helper'i
    # ============================================================
    def _audit_phantom_stock_gram(self, store_filter, do_fix, verbose):
        """
        Satilmis barkodli urunlerin phantom stock_gram degerlerini bulur ve sifirlar.

        Senaryo:
          1) Barkodli urun stok'a girer -> snapshot.stock_gram=product.gram, stock_pieces=1
          2) Urun satilir -> record_exit cagrilir (FAZ 42: quantity_gram=0, pieces=1)
          3) FAZ 65.2 oncesi: stock_pieces=0 olur ama stock_gram=product.gram kalir
             -> Dashboard "Fiziksel Stok HAS" satilmis urunleri saymaya devam eder.

        Tespit kriteri:
          - StockSnapshot.stock_pieces = 0
          - StockSnapshot.stock_gram > 0
          - Products.barcode bos degil (barkodli urun isareti)
          - Products.is_currency = False (doviz haric)

        Onarim:
          - StockSnapshot.stock_gram = 0
          - StockLedger DOKUNULMAZ (APPEND-ONLY garantisi)
          - Products.gram DOKUNULMAZ (urun karti tanimini bozmaz)

        Returns:
            int: Onarilan kayit sayisi (do_fix=False ise 0).
        """
        from apps.stock_management.models import StockSnapshot

        self.stdout.write(self.style.MIGRATE_HEADING(
            'FAZ 65.2 — Phantom stock_gram Taramasi (satilan barkodli urunler)'
        ))

        qs = (
            StockSnapshot.objects
            .select_related('product', 'store')
            .filter(
                stock_pieces=0,
                stock_gram__gt=Decimal('0'),
                product__is_deleted=False,
                product__is_currency=False,
            )
            .exclude(product__barcode__isnull=True)
            .exclude(product__barcode__exact='')
        )
        if store_filter:
            qs = qs.filter(store_id=store_filter)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                'Phantom stock_gram bulunamadi (stock_pieces=0 ve stock_gram>0 olan barkodli urun yok).'
            ))
            self.stdout.write('')
            return 0

        self.stdout.write(self.style.WARNING(
            f'{total} phantom snapshot tespit edildi (stock_pieces=0, stock_gram>0, barkodli).'
        ))

        if verbose or do_fix:
            self.stdout.write(
                f'{"Snapshot":>8} | {"Product":<32} | {"Barkod":<14} | '
                f'{"Magaza":<10} | {"Phantom Gram":>14} | {"WAC":>10} | {"Phantom HAS":>14}'
            )
            self.stdout.write('-' * 130)

        fixed = 0
        total_phantom_gram = Decimal('0')
        total_phantom_has = Decimal('0')

        for snap in qs.iterator(chunk_size=200):
            product = snap.product
            store = snap.store
            phantom_gram = Decimal(str(snap.stock_gram or 0))
            wac_hs = Decimal(str(snap.weighted_avg_cost_hs or 0))
            phantom_has = (phantom_gram * wac_hs).quantize(
                Decimal('0.0001'), rounding=ROUND_HALF_UP
            )
            total_phantom_gram += phantom_gram
            total_phantom_has += phantom_has

            if verbose or do_fix:
                pname = (getattr(product, 'name', '') or '')[:30]
                barkod = (getattr(product, 'barcode', '') or '')[:12]
                store_id_short = str(getattr(store, 'id', ''))[:8]
                self.stdout.write(
                    f'{snap.id:>8} | {pname:<32} | {barkod:<14} | '
                    f'{store_id_short:<10} | {float(phantom_gram):>14.4f} | '
                    f'{float(wac_hs):>10.4f} | {float(phantom_has):>14.4f}'
                )

            if do_fix:
                try:
                    with transaction.atomic():
                        from apps.stock_management.models import StockSnapshot as SS
                        locked = SS.objects.select_for_update().get(pk=snap.pk)
                        locked.stock_gram = Decimal('0.0000')
                        locked.save(update_fields=['stock_gram', 'updated_on'])
                    fixed += 1
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(
                        f'  -> Onarim BASARISIZ snapshot_id={snap.pk}: {exc}'
                    ))

        self.stdout.write('')
        self.stdout.write(
            f'  Toplam phantom gram : {float(total_phantom_gram):.4f} gr'
        )
        self.stdout.write(
            f'  Toplam phantom HAS  : {float(total_phantom_has):.4f} HS '
            f'(dashboard Stok HAS\'ina yanlislikla eklenen miktar)'
        )

        if do_fix:
            self.stdout.write(self.style.SUCCESS(
                f'Phantom onarim tamamlandi: {fixed} / {total} kayit.'
            ))
            self.stdout.write(self.style.WARNING(
                'StockLedger DOKUNULMADI (append-only garantisi). '
                'Sadece StockSnapshot.stock_gram sifirlandi. '
                'Products.gram urun karti tanimi olarak korundu.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                'Onarim icin --fix bayragi ile tekrar calistirin.'
            ))
        self.stdout.write('')
        return fixed
