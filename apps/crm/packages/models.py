# apps/definitions/packages/models.py
import uuid
from decimal import Decimal
from django.db import models
from apps.roles.models import Permission


class SaaSModule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, verbose_name="Modül Adı")
    slug = models.SlugField(max_length=60, unique=True, help_text="URL-safe kısa ad, örn: perakende")
    description = models.TextField(blank=True, verbose_name="Açıklama")
    icon = models.CharField(max_length=80, blank=True, default="fa-solid fa-cube",
                            help_text="FontAwesome class, örn: fa-solid fa-store")

    CURRENCY_CHOICES = [
        ('TRY', 'TL'),
        ('USD', 'Dolar'),
        ('EUR', 'Euro'),
    ]

    license_price = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        verbose_name="Lisans Bedeli (Tek Seferlik)",
        help_text="Modülün tek seferlik lisans satış fiyatı."
    )
    currency = models.CharField(
        max_length=3, choices=CURRENCY_CHOICES, default='TRY',
        verbose_name="Para Birimi"
    )

    price_monthly = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name="Aylık Fiyat (₺)",
        help_text="İleriye dönük kiralama modeli için saklı alan."
    )
    price_yearly = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        verbose_name="Yıllık Fiyat (₺)",
        help_text="İleriye dönük kiralama modeli için saklı alan."
    )

    is_core = models.BooleanField(default=False, verbose_name="Çekirdek Modül mü?",
                                  help_text="Çekirdek modüller her pakete dahildir ve kaldırılamaz.")
    dependencies = models.ManyToManyField(
        'self', symmetrical=False, blank=True,
        related_name='required_by',
        verbose_name="Bağımlılıklar",
        help_text="Bu modül seçildiğinde otomatik seçilmesi gereken diğer modüller."
    )

    permissions = models.ManyToManyField(
        'roles.Permission', blank=True,
        related_name='saas_modules',
        verbose_name="Modül Yetkileri",
        help_text="Bu modül satın alındığında müşteriye verilecek sistem yetkileri."
    )

    order = models.PositiveSmallIntegerField(default=100, verbose_name="Sıralama")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")

    created_on = models.DateTimeField(auto_now_add=True)
    modified_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'SaaSModules'
        ordering = ('order', 'name')
        verbose_name = 'SaaS Modül'
        verbose_name_plural = 'SaaS Modüller'

    def __str__(self):
        return self.name

    @property
    def currency_symbol(self) -> str:
        return {'TRY': '₺', 'USD': '$', 'EUR': '€'}.get(self.currency, self.currency)

    def get_all_dependencies(self):
        visited = set()
        stack = list(self.dependencies.all())
        while stack:
            dep = stack.pop()
            if dep.id not in visited:
                visited.add(dep.id)
                stack.extend(dep.dependencies.all())
        return visited

    def collect_all_permissions(self):
        perm_ids = set(self.permissions.values_list('id', flat=True))
        for dep_id in self.get_all_dependencies():
            dep_module = SaaSModule.objects.get(id=dep_id)
            perm_ids.update(dep_module.permissions.values_list('id', flat=True))
        return perm_ids


class Packages(models.Model):
    CURRENCY_CHOICES = [
        ('TRY', 'TRY'),
        ('USD', 'USD'),
        ('EUR', 'EUR'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=50, unique=True, help_text="Örn: bronze, silver, gold")
    name = models.CharField(max_length=100, verbose_name="Paket Adı")
    order = models.PositiveSmallIntegerField(default=100, verbose_name="Sıralama")

    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD', verbose_name="Para Birimi")

    # --- YENİ FİYATLANDIRMA YAPISI ---
    # max_digits=14 yaptık ki milyar seviyesine kadar fiyat girilebilsin (Overflow hatasını çözer)
    price_license = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="Lisans Bedeli (Tek Seferlik)"
    )

    # max_digits=5 yeterlidir (999.99'a kadar izin verir)
    maintenance_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=Decimal('15.00'),
        verbose_name="Yıllık Bakım Oranı (%)",
        help_text="Lisans bedeli üzerinden hesaplanacak yıllık güncelleme/bakım oranı."
    )
    # ---------------------------------

    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    is_recommended = models.BooleanField(default=False, verbose_name="Önerilen")
    badge_text = models.CharField(max_length=30, blank=True, help_text='Örn: "En Popüler"')

    # FAZ 19 — Hızlı Onboarding (Fast-Track) için sanal demo paketi işaretleyicisi.
    # is_demo=True olan paketler satış arayüzlerinde GİZLENİR; yalnızca sistem
    # tarafından (create_demo_store servisi) kullanılır. 0 TL'lik gölge sipariş
    # mekanizmasının paket bağlantısı bu paket üzerinden kurulur.
    is_demo = models.BooleanField(
        default=False,
        verbose_name="Demo Paketi mi?",
        help_text=(
            "Yalnızca Hızlı Onboarding (Fast-Track) akışı tarafından kullanılır. "
            "Bu paket satış listelerinde gösterilmez; gölge sipariş mekanizması "
            "tarafından otomatik atanır."
        ),
    )

    created_on = models.DateTimeField(auto_now_add=True)
    modified_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Packages'
        ordering = ('order', 'name')
        verbose_name = 'Paket'
        verbose_name_plural = 'Paketler'

    def __str__(self):
        return f"{self.name} ({self.get_currency_display()} {self.price_license})"

    @property
    def currency_symbol(self) -> str:
        return {'TRY': '₺', 'USD': '$', 'EUR': '€'}.get(self.currency, self.currency)

    @property
    def maintenance_amount(self):
        """Hesaplanan yıllık bakım tutarı"""
        if not self.price_license:
            return Decimal('0.00')
        val = self.price_license * (self.maintenance_percent / Decimal('100.00'))
        return val.quantize(Decimal('0.01'))


class PackageModule(models.Model):
    """
    Package ↔ SaaSModule köprü tablosu.

    Bir pakete modüller atandığında, modüllerin permission'ları
    sync_package_permissions_from_modules() service fonksiyonu ile
    PackagePermissionMatrix'e otomatik yansıtılır.

    Çekirdek modüller (is_core=True) her pakete otomatik dahildir;
    bu tablo yalnızca ek modülleri tutar.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        Packages, on_delete=models.CASCADE, related_name='package_modules',
        verbose_name="Paket"
    )
    module = models.ForeignKey(
        SaaSModule, on_delete=models.CASCADE, related_name='in_packages',
        verbose_name="Modül"
    )
    added_on = models.DateTimeField(auto_now_add=True, verbose_name="Eklenme Tarihi")

    class Meta:
        db_table = 'PackageModules'
        unique_together = ('package', 'module')
        verbose_name = 'Paket Modül'
        verbose_name_plural = 'Paket Modüller'

    def __str__(self):
        return f"{self.package.code} → {self.module.name}"


class PackagePermissionMatrix(models.Model):
    SOURCE_CHOICES = [
        ('manual', 'Elle Atandı'),
        ('module', 'Modülden Geldi'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(Packages, on_delete=models.CASCADE, related_name='perm_matrix')
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name='package_matrix')
    available = models.BooleanField(default=False)
    note = models.CharField(max_length=120, blank=True)
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default='manual',
        verbose_name="Kaynak",
        help_text="Bu yetki pakete elle mi atandı yoksa modülden mi geldi?"
    )

    class Meta:
        db_table = 'PackagePermissionMatrix'
        unique_together = ('package', 'permission')

    def __str__(self):
        return f"{self.package.code} - {self.permission.code}"