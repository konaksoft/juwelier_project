"""
FAZ 48 (2026-05-03): Manuel API fiyat güncelleme komutu.

Celery worker / Beat çalışmadığında Harem Altın API'sinden ürün fiyatlarını
manuel olarak çeker ve Products tablosunu günceller. Mevcut Celery task
(apps.products.tasks.update_products_from_api) doğrudan senkron çağrılır;
şema/iş mantığı dokunulmaz.

Kullanım:
    python manage.py update_products_now
    python manage.py update_products_now --silent

Tipik durumlar:
    - 22 Ayar Gram, Çeyrek vs. ürünlerin alış/satış fiyatlarının eski değerde
      kalması (Celery Beat down) → bu komut anlık tek seferlik düzeltir.
    - Cron / systemd timer ile periyodik tetiklenebilir (Celery Beat alternatifi).
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Harem Altın API'sinden ürün fiyatlarını manuel olarak çeker ve günceller. "
        "Celery worker yokken kullanılır."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--silent',
            action='store_true',
            help='Çıktı bastırmaz (cron için).',
        )

    def handle(self, *args, **options):
        silent = options.get('silent', False)

        if not silent:
            self.stdout.write("Harem Altın API'den fiyat güncellemesi başlatılıyor...")

        # Mevcut Celery task'ını senkron çağır.
        # update_products_from_api @shared_task ile sarılı; .apply() yerine
        # doğrudan fonksiyon çağrısı yapılır → Celery broker gerektirmez.
        from apps.products.tasks import update_products_from_api

        try:
            result = update_products_from_api()
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Hata: {exc}"))
            raise

        if not silent:
            if isinstance(result, str) and result.startswith("Hata"):
                self.stdout.write(self.style.WARNING(result))
            else:
                self.stdout.write(self.style.SUCCESS(f"Tamamlandı: {result}"))
