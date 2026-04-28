"""
URL-tabanlı Permission senkronizasyon komutu.

Projedeki tüm urls.py dosyalarını tarayarak yalnızca path() ile
URL'ye bağlanmış view fonksiyonları için Permission kaydı oluşturur.

Eski davranış (views.py'deki TÜM fonksiyonları tarama) kaldırılmıştır.
Artık helper fonksiyonlar (_fmt_tl, d_quantize, parse_decimal vb.)
için gereksiz permission oluşturulmaz.

EXCLUDED_APPS listesindeki uygulamalar tamamen atlanır.

Kullanım:
    python manage.py sync_permissions
"""

import uuid

from django.core.management.base import BaseCommand

from apps.roles.models import Permission
from apps.roles.management.commands._permission_utils import (
    discover_url_routed_views,
    get_turkish_name,
    EXCLUDED_APPS,
)


class Command(BaseCommand):
    help = (
        "URL-routed view fonksiyonlarına göre Permission tablosunu "
        "otomatik senkronize eder. Yalnızca urls.py'deki path() "
        "çağrılarında referans edilen fonksiyonlar için kayıt oluşturur. "
        "EXCLUDED_APPS listesindeki uygulamalar atlanır."
    )

    def handle(self, *args, **options):
        created_count = 0
        updated_count = 0
        skipped_count = 0

        # İstisna uygulamaları bildir
        if EXCLUDED_APPS:
            self.stdout.write(self.style.WARNING(
                f"İstisna uygulamalar (atlanacak): "
                f"{', '.join(sorted(EXCLUDED_APPS))}"
            ))
            self.stdout.write("")

        routed_views = discover_url_routed_views()

        if not routed_views:
            self.stdout.write(self.style.WARNING(
                "Hiçbir URL-routed view fonksiyonu bulunamadı. "
                "apps/ dizini altında urls.py dosyalarını kontrol edin."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Toplam {len(routed_views)} URL-routed view fonksiyonu keşfedildi."
        ))
        self.stdout.write("")

        for view_info in routed_views:
            code = view_info['code']
            group = view_info['group']
            is_system_only = view_info['is_system_only']
            func_name = view_info['func_name']
            name = get_turkish_name(func_name)

            perm = Permission.objects.filter(code=code).first()

            if perm is None:
                # Yeni permission oluştur
                Permission.objects.create(
                    id=uuid.uuid4(),
                    code=code,
                    name=name,
                    group=group,
                    is_system_only=is_system_only,
                )
                created_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  + Eklendi: {code} ({name}) "
                    f"[group={group}, system={is_system_only}]"
                ))
            else:
                # Mevcut kaydın group ve is_system_only değerlerini güncelle
                changed = False
                changes = []

                if perm.group != group:
                    changes.append(f"group: {perm.group!r} → {group!r}")
                    perm.group = group
                    changed = True

                if perm.is_system_only != is_system_only:
                    changes.append(
                        f"is_system_only: {perm.is_system_only} → {is_system_only}"
                    )
                    perm.is_system_only = is_system_only
                    changed = True

                if changed:
                    perm.save(update_fields=['group', 'is_system_only'])
                    updated_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"  ~ Güncellendi: {code} — {', '.join(changes)}"
                    ))
                else:
                    skipped_count += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Senkronizasyon tamamlandı. "
            f"Yeni: {created_count}, "
            f"Güncellenen: {updated_count}, "
            f"Değişmeyen: {skipped_count}"
        ))

        # Özet: app bazlı dağılım
        app_counts = {}
        for v in routed_views:
            app_counts[v['app_name']] = app_counts.get(v['app_name'], 0) + 1

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Uygulama bazlı dağılım:"))
        for app, count in sorted(app_counts.items()):
            self.stdout.write(f"  {app:30s} → {count:3d} yetki")
