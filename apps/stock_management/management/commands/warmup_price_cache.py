"""
Django Management Command: warmup_price_cache
===============================================

Redis cache'i veritabanindan doldurur.

Kullanim:
    python manage.py warmup_price_cache
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Redis fiyat cache ini veritabanindan doldurur (sunucu restart sonrasi).'

    def handle(self, *args, **options):
        from apps.stock_management.services.price_service import PriceService

        self.stdout.write('Fiyat cache warmup basliyor...')

        loaded = PriceService.warmup_cache()

        self.stdout.write(self.style.SUCCESS(
            f'Tamamlandi: {loaded} fiyat kaydi Redis cache e yuklendi.'
        ))
