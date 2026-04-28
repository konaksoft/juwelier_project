"""
Django Management Command: seed_demo_assets
============================================

FAZ 19 — Hızlı Onboarding (Fast-Track) için gerekli VERİ kayıtlarını üretir:

  1. SaaSModule(slug='demo-access', is_active=True)
       → DEMO mağazaların efektif yetki kümesini sağlayan modül.
       → Permissions opsiyoneldir; ihtiyaç olan yetkiler admin'den eklenir.

  2. Packages(code='demo', is_demo=True, is_active=True, price_license=0.00)
       → Gölge sipariş mekanizmasının iliştirildiği 0 TL'lik sanal demo paketi.
       → Satış arayüzlerinde gizlenir (is_demo=True filtresi).

İdempotent: Her iki kayıt da get_or_create ile oluşturulur; mevcut veri korunur.

Kullanım:
    python manage.py seed_demo_assets
    python manage.py seed_demo_assets --with-permissions   # demo modülüne tüm CORE yetkilerini ata
"""

from decimal import Decimal

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'FAZ 19 demo SaaSModule ve demo Packages kayıtlarını idempotent oluşturur.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--with-permissions',
            action='store_true',
            default=False,
            help='Demo modülüne, sistemdeki çekirdek modüllerin yetkilerini otomatik bağla.',
        )

    def handle(self, *args, **options):
        with_permissions = options.get('with_permissions', False)

        from apps.crm.packages.models import SaaSModule, Packages

        # ── 1. DEMO SaaSModule ──
        demo_module, m_created = SaaSModule.objects.get_or_create(
            slug='demo-access',
            defaults={
                'name': 'Demo Erişim',
                'description': (
                    'FAZ 19 — Fast-Track demo mağazaların efektif yetki kümesini '
                    'sağlayan modül. Sadece sistem tarafından kullanılır.'
                ),
                'icon': 'fa-solid fa-clock',
                'license_price': Decimal('0.00'),
                'currency': 'TRY',
                'is_core': False,
                'order': 9999,
                'is_active': True,
            },
        )
        if m_created:
            self.stdout.write(self.style.SUCCESS(
                f"[+] SaaSModule oluşturuldu: slug={demo_module.slug}"
            ))
        else:
            self.stdout.write(
                f"[=] SaaSModule zaten mevcut: slug={demo_module.slug}"
            )

        # ── 1b. (Opsiyonel) Çekirdek modüllerin yetkilerini demo'ya bağla ──
        if with_permissions:
            core_modules = SaaSModule.objects.filter(is_core=True, is_active=True)
            attached = 0
            for cm in core_modules:
                for perm in cm.permissions.all():
                    if not demo_module.permissions.filter(id=perm.id).exists():
                        demo_module.permissions.add(perm)
                        attached += 1
            self.stdout.write(self.style.SUCCESS(
                f"[+] Demo modülüne {attached} çekirdek yetki bağlandı."
            ))

        # ── 2. DEMO Packages ──
        demo_pkg, p_created = Packages.objects.get_or_create(
            code='demo',
            defaults={
                'name': 'Demo Paketi',
                'order': 9999,
                'currency': 'TRY',
                'price_license': Decimal('0.00'),
                'maintenance_percent': Decimal('0.00'),
                'is_active': True,
                'is_recommended': False,
                'badge_text': '',
                'is_demo': True,
            },
        )
        if p_created:
            self.stdout.write(self.style.SUCCESS(
                f"[+] Packages oluşturuldu: code={demo_pkg.code} (is_demo={demo_pkg.is_demo})"
            ))
        else:
            # Mevcut kayıt varsa yalnızca is_demo bayrağını garantiye al
            if not demo_pkg.is_demo:
                demo_pkg.is_demo = True
                demo_pkg.save(update_fields=['is_demo'])
                self.stdout.write(self.style.WARNING(
                    f"[~] Mevcut 'demo' paketinde is_demo=True olarak güncellendi."
                ))
            else:
                self.stdout.write(
                    f"[=] Packages zaten mevcut: code={demo_pkg.code}"
                )

        self.stdout.write(self.style.SUCCESS(
            "[seed_demo_assets] Tamamlandı."
        ))
