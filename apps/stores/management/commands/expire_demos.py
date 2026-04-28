"""
Django Management Command: expire_demos
========================================

FAZ 19 — Hızlı Onboarding (Fast-Track) cron komutu.

Süresi dolan DEMO mağazaları otomatik olarak EXPIRED statüsüne çeker.
expire_demo_stores() servisini çağırır; servis zaten idempotent ve atomik.

Kullanım:
    python manage.py expire_demos
    python manage.py expire_demos --dry-run        # sadece raporla, dokunma
    python manage.py expire_demos --verbose

Önerilen cron:
    # Her gece 03:00'te
    0 3 * * * /path/to/venv/bin/python manage.py expire_demos
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        'Süresi dolan DEMO mağazaları EXPIRED statüsüne çeker. '
        'Idempotent: zaten EXPIRED olanları yeniden işlemez.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Sadece kaç mağaza etkileneceğini raporla; veriyi değiştirme.',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            default=False,
            help='Etkilenen her mağazayı tek tek listele.',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        verbose = options.get('verbose', False)

        from apps.stores.models import Stores

        now = timezone.now()
        candidates = Stores.objects.filter(
            status='DEMO',
            demo_expires_at__lt=now,
            is_deleted=False,
        )
        candidate_count = candidates.count()

        self.stdout.write(self.style.NOTICE(
            f"[expire_demos] {now.isoformat()} — Süresi dolan DEMO sayısı: {candidate_count}"
        ))

        if verbose:
            for s in candidates:
                self.stdout.write(
                    f"  - store_id={s.store_id} title={s.title} "
                    f"expires_at={s.demo_expires_at.isoformat() if s.demo_expires_at else '-'}"
                )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "[expire_demos] --dry-run aktif: hiçbir kayıt güncellenmedi."
            ))
            return

        if candidate_count == 0:
            self.stdout.write(self.style.SUCCESS(
                "[expire_demos] İşlenecek mağaza yok."
            ))
            return

        from apps.stores.services import expire_demo_stores
        result = expire_demo_stores()

        self.stdout.write(self.style.SUCCESS(
            f"[expire_demos] Tamamlandı: {result['expired_count']} mağaza EXPIRED."
        ))
        if verbose and result.get('store_ids'):
            for sid in result['store_ids']:
                self.stdout.write(f"  ✔ {sid}")
