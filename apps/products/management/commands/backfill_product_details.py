"""
Backfill Product Details — Eski WATCH/DIAMOND urunlerine Detay Eklemek
================================================================================

Bu yonetim komutu veritabanini tarar ve:
  * material_type='WATCH' olup WatchDetail OneToOne kaydi olmayan urunlere
    bos bir WatchDetail kaydi acar.
  * material_type='DIAMOND' olup DiamondDetail OneToOne kaydi olmayan urunlere
    bos bir DiamondDetail kaydi acar.

Onarim Fazi 1'de eklenen post_save sinyali yalnizca YENI olusturulan urunler
icin detay tablosu acar. Sinyal oncesi zamanlardan kalan orphan urunler icin
bu komut ile onarimi tamamlayabilirsiniz.

Calistirma:
    python manage.py backfill_product_details
    python manage.py backfill_product_details --dry-run
    python manage.py backfill_product_details --batch-size 500

Guvenlik:
    * get_or_create kullanilir (IntegrityError yarisi durumlarinda).
    * Her batch ayri transaction icinde calisir (buyuk veri icin bellek/DB
      kilidi guvenli).
    * --dry-run ile degisiklik yapmadan sadece sayilar raporlanir.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.utils import IntegrityError


class Command(BaseCommand):
    help = (
        "material_type='WATCH' veya 'DIAMOND' olup detay (WatchDetail / "
        "DiamondDetail) kaydi bulunmayan urunlere bos detay kaydi acar. "
        "Onarim Fazi 1 post_save sinyalinin kapsamadigi eski kayitlar "
        "icin kullanilir."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help="Sadece raporla, herhangi bir kayit olusturma.",
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help=(
                "Tek seferde kac urun islensin (default: 500). Buyuk "
                "veritabanlarinda bellek kullanimini sinirlar."
            ),
        )
        parser.add_argument(
            '--only',
            choices=['watch', 'diamond', 'both'],
            default='both',
            help="Sadece WATCH veya DIAMOND onarimi calistir (default: both).",
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        batch_size = max(1, int(options.get('batch_size', 500) or 500))
        only = options.get('only', 'both')

        # Lazy import — app registry hazir olduktan sonra.
        try:
            from apps.products.models import (
                Products, WatchDetail, DiamondDetail, MaterialType,
            )
        except Exception as exc:
            self.stderr.write(self.style.ERROR(
                f"Modeller yuklenemedi: {exc}"
            ))
            return

        self.stdout.write(self.style.NOTICE(
            f"[backfill_product_details] Baslatildi — "
            f"dry_run={dry_run}, batch_size={batch_size}, only={only}"
        ))

        watch_fixed = 0
        diamond_fixed = 0
        watch_skipped = 0
        diamond_skipped = 0

        # --------------------------------------------------------------
        # WATCH onarimi
        # --------------------------------------------------------------
        if only in ('watch', 'both'):
            try:
                watch_qs = Products.objects.filter(
                    material_type=MaterialType.WATCH,
                    watch_detail__isnull=True,
                ).only('id', 'name')
                total = watch_qs.count()
                self.stdout.write(self.style.NOTICE(
                    f"  WATCH orphan sayisi: {total}"
                ))

                processed = 0
                # iterator() ile bellek verimli taragerceklestirir
                batch = []
                for prod in watch_qs.iterator(chunk_size=batch_size):
                    batch.append(prod)
                    if len(batch) >= batch_size:
                        w_fixed, w_skip = self._apply_watch_batch(
                            batch, WatchDetail, dry_run,
                        )
                        watch_fixed += w_fixed
                        watch_skipped += w_skip
                        processed += len(batch)
                        self.stdout.write(
                            f"  WATCH ilerleme: {processed}/{total}"
                        )
                        batch = []

                # Kalanlari isle
                if batch:
                    w_fixed, w_skip = self._apply_watch_batch(
                        batch, WatchDetail, dry_run,
                    )
                    watch_fixed += w_fixed
                    watch_skipped += w_skip

            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f"WATCH onarimi sirasinda hata: {exc}"
                ))

        # --------------------------------------------------------------
        # DIAMOND onarimi
        # --------------------------------------------------------------
        if only in ('diamond', 'both'):
            try:
                diamond_qs = Products.objects.filter(
                    material_type=MaterialType.DIAMOND,
                    diamond_detail__isnull=True,
                ).only('id', 'name')
                total = diamond_qs.count()
                self.stdout.write(self.style.NOTICE(
                    f"  DIAMOND orphan sayisi: {total}"
                ))

                processed = 0
                batch = []
                for prod in diamond_qs.iterator(chunk_size=batch_size):
                    batch.append(prod)
                    if len(batch) >= batch_size:
                        d_fixed, d_skip = self._apply_diamond_batch(
                            batch, DiamondDetail, dry_run,
                        )
                        diamond_fixed += d_fixed
                        diamond_skipped += d_skip
                        processed += len(batch)
                        self.stdout.write(
                            f"  DIAMOND ilerleme: {processed}/{total}"
                        )
                        batch = []

                if batch:
                    d_fixed, d_skip = self._apply_diamond_batch(
                        batch, DiamondDetail, dry_run,
                    )
                    diamond_fixed += d_fixed
                    diamond_skipped += d_skip

            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f"DIAMOND onarimi sirasinda hata: {exc}"
                ))

        # --------------------------------------------------------------
        # OZET RAPORU
        # --------------------------------------------------------------
        self.stdout.write("")
        action = "bulunan" if dry_run else "onarilan"
        self.stdout.write(self.style.SUCCESS(
            f"{watch_fixed} adet Saat, {diamond_fixed} adet Pırlanta "
            f"detay tablosu {action}."
        ))

        if watch_skipped or diamond_skipped:
            self.stdout.write(self.style.WARNING(
                f"Atlanan kayitlar — Saat: {watch_skipped}, "
                f"Pırlanta: {diamond_skipped}"
            ))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "[DRY-RUN] Hic bir kayit olusturulmadi. Gercek onarim "
                "icin '--dry-run' flag'ini kaldirin."
            ))

    # ------------------------------------------------------------------
    # BATCH YARDIMCILARI
    # ------------------------------------------------------------------

    def _apply_watch_batch(self, products, WatchDetail, dry_run):
        """Bir batch WATCH urunu icin WatchDetail olustur."""
        fixed = 0
        skipped = 0
        if dry_run:
            # Dry-run: sadece say
            return len(products), 0

        for prod in products:
            try:
                with transaction.atomic():
                    _obj, created = WatchDetail.objects.get_or_create(
                        product=prod,
                    )
                if created:
                    fixed += 1
                else:
                    # Paralel bir sinyal/komut bu arada olusturmus olabilir
                    skipped += 1
            except IntegrityError:
                # OneToOne ihlalinde kayit zaten vardir
                skipped += 1
            except Exception as exc:
                self.stderr.write(self.style.WARNING(
                    f"    WatchDetail olusturulamadi "
                    f"(product_id={prod.pk}): {exc}"
                ))
                skipped += 1
        return fixed, skipped

    def _apply_diamond_batch(self, products, DiamondDetail, dry_run):
        """Bir batch DIAMOND urunu icin DiamondDetail olustur."""
        fixed = 0
        skipped = 0
        if dry_run:
            return len(products), 0

        for prod in products:
            try:
                with transaction.atomic():
                    _obj, created = DiamondDetail.objects.get_or_create(
                        product=prod,
                    )
                if created:
                    fixed += 1
                else:
                    skipped += 1
            except IntegrityError:
                skipped += 1
            except Exception as exc:
                self.stderr.write(self.style.WARNING(
                    f"    DiamondDetail olusturulamadi "
                    f"(product_id={prod.pk}): {exc}"
                ))
                skipped += 1
        return fixed, skipped
