import uuid
from django.db import models




class City(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, verbose_name="Şehir Adı")
    slug = models.CharField(max_length=100, blank=True, null=True)
    plate_code = models.CharField(max_length=5, blank=True, null=True, verbose_name="Plaka Kodu")

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        verbose_name = 'Şehir'
        verbose_name_plural = 'Şehirler'


class District(models.Model):
    """
    İlçe bilgileri model olarak duruyor ancak bu veri setinde
    Vergi Daireleri doğrudan Şehre bağlandığı için şimdilik boş kalabilir
    veya başka bir kaynaktan doldurulabilir.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name='districts', verbose_name="Şehir")
    name = models.CharField(max_length=100, verbose_name="İlçe Adı")
    slug = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        verbose_name = 'İlçe'
        verbose_name_plural = 'İlçeler'


class TaxOffice(models.Model):
    """
    Vergi Daireleri ve Malmüdürlükleri.
    Excel yapısına uygun olarak doğrudan City (İl) modeline bağlanmıştır.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Doğrudan şehre bağlanıyor (İlçe bağı kaldırıldı)
    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name='tax_offices', verbose_name="Şehir")
    code = models.CharField(max_length=50, unique=True, verbose_name="Vergi Dairesi Kodu")
    name = models.CharField(max_length=255, verbose_name="Vergi Dairesi/Malmüdürlüğü Adı")

    TYPE_CHOICES = (
        ('VD', 'Vergi Dairesi'),
        ('MAL', 'Malmüdürlüğü'),
    )
    office_type = models.CharField(max_length=3, choices=TYPE_CHOICES, default='VD', verbose_name="Tip")

    def __str__(self):
        return f"{self.code} - {self.name}"

    class Meta:
        ordering = ['code']
        verbose_name = 'Vergi Dairesi'
        verbose_name_plural = 'Vergi Daireleri'