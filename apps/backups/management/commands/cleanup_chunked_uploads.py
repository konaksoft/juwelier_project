"""
==============================================================================
 FAZ 60.2 — Chunked Upload Cleanup Komutu
==============================================================================

Süresi dolmuş veya yarım kalan parçalı yükleme oturumlarını temizler.
Celery beat ile günde 1 kere çalıştırılması önerilir.

Kullanım:
    python manage.py cleanup_chunked_uploads
    python manage.py cleanup_chunked_uploads --force-all  # tüm terminal-olmayanlar

Default davranış: expires_at < now olanları sil.
COMPLETED kayıtlar audit için saklanır (sadece geçici dosya silinmiş olur).
==============================================================================
"""

from django.core.management.base import BaseCommand

from apps.backups.chunked_upload import ChunkedUploadService
from apps.backups.models import ChunkedUploadSession


class Command(BaseCommand):
    help = 'Süresi dolmuş veya yarım kalan parçalı yükleme oturumlarını temizler.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force-all',
            action='store_true',
            help='COMPLETED hariç tüm non-terminal oturumları sil (tehlikeli).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Hiçbir şeyi silme, sadece raporla.',
        )

    def handle(self, *args, **options):
        force = options.get('force_all', False)
        dry = options.get('dry_run', False)

        if dry:
            from django.utils import timezone
            qs = ChunkedUploadSession.objects.filter(
                expires_at__lt=timezone.now(),
            ).exclude(status='COMPLETED')
            self.stdout.write(f"DRY-RUN: {qs.count()} oturum silinecek.")
            for s in qs[:50]:
                self.stdout.write(
                    f"  - {s.id} [{s.status}] {s.filename} "
                    f"({s.received_chunks}/{s.total_chunks}) "
                    f"expires={s.expires_at}"
                )
            return

        if force:
            # Tüm non-terminal oturumları zorla iptal et
            qs = ChunkedUploadSession.objects.exclude(
                status__in=['COMPLETED', 'ABORTED', 'EXPIRED', 'FAILED']
            )
            count = qs.count()
            for s in qs:
                ChunkedUploadService.abort(s.id)
            self.stdout.write(self.style.WARNING(
                f"FORCE: {count} aktif oturum iptal edildi."
            ))

        result = ChunkedUploadService.cleanup_expired()
        self.stdout.write(self.style.SUCCESS(
            f"Cleanup tamamlandı: "
            f"expired={result['expired']}, "
            f"removed_files={result['removed_files']}, "
            f"kept_completed={result['kept_completed']}"
        ))
