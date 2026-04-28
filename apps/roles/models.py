# apps/roles/models.py
import uuid
from django.db import models


class Permission(models.Model):
    """
    Sistem yetki birimi.

    group alanı standardizasyonu:
      - 'Dashboard'  → Kuyumcu mağazası operasyonları (ürünler, satış,
                        müşteri, tamir, atölye vb.). Mağaza yöneticisi
                        personel rolü oluştururken yalnızca bu gruptaki
                        yetkileri görebilir/seçebilir.
      - Diğer değerler (app adları: 'stores', 'roles', 'accounts' vb.)
                      → SaaS yönetim yetkileri. Sadece superadmin ve
                        Konasoft personeli (is_staff=True) tarafından
                        görülür.

    is_system_only alanı:
      - True  → Yalnızca superadmin / is_staff kullanıcılara atanabilir.
      - False → Mağaza yöneticisi tarafından personel rolüne atanabilir.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    group = models.CharField(max_length=50, null=True, blank=True)
    order = models.IntegerField(null=True, blank=True)
    is_system_only = models.BooleanField(default=False)

    class Meta:
        db_table = 'Permission'
        ordering = ['group', 'order', 'code']

    def __str__(self):
        return f"{self.code} - {self.name}"


class Roles(models.Model):
    """
    Sistem rol modeli.

    Faz 12.4 — Rol İzolasyonu:
        store alanı (opsiyonel FK) eklenerek mağaza bazlı rol izolasyonu
        sağlanmıştır.

        - store = NULL  → Global rol. SYSTEM ve CHAMBER kategorisindeki
                          roller ile eski (Faz 12.4 öncesi) tüm roller
                          bu gruba girer. Superadmin ve Konasoft personeli
                          tarafından yönetilir.
        - store = <FK>  → Mağazaya özel izole rol. İlgili mağaza yöneticisi
                          tarafından oluşturulur. Yalnızca o mağazanın
                          personeline atanabilir.

    Kurallar:
        - category='STORE' + store=NULL → Tüm mağazalarda kullanılabilen
          genel mağaza rolü (superadmin oluşturur).
        - category='STORE' + store=<FK> → Yalnızca ilgili mağazada
          geçerli izole rol (mağaza yöneticisi oluşturur).
        - category='SYSTEM' / 'CHAMBER' → store her zaman NULL olmalı.
    """
    CATEGORY_CHOICES = (
        ('SYSTEM', 'Sistem (Konasoft)'),
        ('STORE', 'Mağaza (Kuyumcu)'),
        ('CHAMBER', 'Dernek / Oda'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, default='STORE', db_index=True)

    # Faz 12.4: Mağaza bazlı rol izolasyonu
    store = models.ForeignKey(
        'stores.Stores', on_delete=models.CASCADE, null=True, blank=True,
        related_name='store_roles',
        verbose_name='Mağaza',
        help_text='NULL ise global rol; dolu ise yalnızca bu mağazada geçerli izole rol.'
    )

    name = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        db_table = 'Role'
        indexes = [
            models.Index(fields=['category', 'store', 'is_active']),
        ]

    def __str__(self):
        if self.store:
            store_label = self.store.title or self.store.store_id or str(self.store.id)
            return f"{self.name} ({store_label})"
        return self.name

    @property
    def is_global(self):
        """Bu rol global mi (tüm mağazalarda geçerli mi)?"""
        return self.store_id is None


class RoleDetail(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Roles, on_delete=models.CASCADE)
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    status = models.BooleanField(default=False)

    class Meta:
        db_table = 'RoleDetail'
        constraints = [
            models.UniqueConstraint(
                fields=['role', 'permission'],
                name='uniq_role_permission_package'
            )
        ]