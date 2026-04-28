# ============================================================================
# DOSYA: apps/banking/management/commands/backfill_payment_recon_status.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v3 — Mutabakat Altyapısı (Faz 1)
#
# AMAÇ:
#   Mevcut Payment kayıtlarından payment_type'ı CREDIT_CARD veya TRANSFER
#   olanların reconciliation_status'unu NOT_REQUIRED → PENDING olarak günceller.
#
#   Bu script geriye dönük banka hareketlerini ARAMAAZ; sadece mevcut Payment
#   kayıtlarının mutabakat durumunu düzeltir. Gerçek eşleştirme Faz 3'te
#   ReconciliationService tarafından yapılacaktır.
#
# KULLANIM:
#   python manage.py backfill_payment_recon_status
#   python manage.py backfill_payment_recon_status --dry-run
#   python manage.py backfill_payment_recon_status --batch-size=500
#
# GÜVENLİK:
#   - İdempotent: Birden fazla çalıştırılabilir, sadece NOT_REQUIRED olanları etkiler
#   - --dry-run: Gerçek güncelleme yapmadan kaç kayıt etkileneceğini gösterir
#   - Batch processing: Büyük tablolarda memory spike'ı önler
#   - Mevcut PENDING/MATCHED/MANUAL vb. kayıtlara dokunmaz
# ============================================================================

import logging
from django.core.management.base import BaseCommand
from django.db import transaction

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Mevcut CREDIT_CARD ve TRANSFER Payment kayıtlarının "
        "reconciliation_status'unu NOT_REQUIRED → PENDING olarak günceller. "
        "Geriye dönük banka hareketi aramaz; sadece statü backfill'i yapar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Gerçek güncelleme yapmadan kaç kayıt etkileneceğini gösterir.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Her batch\'te güncellenecek kayıt sayısı (varsayılan: 1000).',
        )

    def handle(self, *args, **options):
        from apps.process.models import Payment

        dry_run = options['dry_run']
        batch_size = options['batch_size']

        # Etkilenecek kayıtlar:
        # - payment_type CREDIT_CARD veya TRANSFER (banka mutabakatı gereken tipler)
        # - reconciliation_status hâlâ NOT_REQUIRED (migration default'u)
        # - Zaten PENDING/MATCHED/vb. olan kayıtlara DOKUNMA
        target_qs = Payment.objects.filter(
            payment_type__in=['CREDIT_CARD', 'TRANSFER'],
            reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
        )

        total_count = target_qs.count()

        if total_count == 0:
            self.stdout.write(self.style.SUCCESS(
                "Guncellenecek kayit bulunamadi. "
                "Tum CREDIT_CARD/TRANSFER odemeleri zaten PENDING veya ustunde."
            ))
            return

        self.stdout.write(
            f"Toplam {total_count} Payment kaydi "
            f"(CREDIT_CARD + TRANSFER, status=NOT_REQUIRED) bulundu."
        )

        if dry_run:
            # Detaylı breakdown göster
            cc_count = target_qs.filter(payment_type='CREDIT_CARD').count()
            tr_count = target_qs.filter(payment_type='TRANSFER').count()
            self.stdout.write(self.style.WARNING(
                f"[DRY RUN] Guncelleme YAPILMADI.\n"
                f"  - CREDIT_CARD:  {cc_count} kayit\n"
                f"  - TRANSFER:     {tr_count} kayit\n"
                f"  - TOPLAM:       {total_count} kayit PENDING yapilacak."
            ))
            return

        # Batch processing ile güncelleme
        # Django'nun .update() bulk SQL kullanır — memory-safe
        updated_total = 0
        while True:
            # Her batch'te belirli sayıda PK al, sonra o PK'ları güncelle
            batch_pks = list(
                target_qs.values_list('pk', flat=True)[:batch_size]
            )
            if not batch_pks:
                break

            with transaction.atomic():
                batch_updated = Payment.objects.filter(
                    pk__in=batch_pks,
                ).update(
                    reconciliation_status=Payment.ReconciliationStatus.PENDING,
                )

            updated_total += batch_updated
            self.stdout.write(
                f"  Batch: {batch_updated} kayit guncellendi "
                f"(toplam: {updated_total}/{total_count})"
            )

        log.info(
            "backfill_payment_recon_status: %d Payment kaydi PENDING yapildi.",
            updated_total,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Tamamlandi: {updated_total} Payment kaydi "
            f"NOT_REQUIRED -> PENDING olarak guncellendi."
        ))
