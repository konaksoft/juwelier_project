from django.db import models

class Testimonial(models.Model):
    full_name = models.CharField(max_length=100, verbose_name="Ad Soyad")
    company_name = models.CharField(max_length=100, verbose_name="Kuyumcu/Firma Adı")
    logo = models.ImageField(upload_to='testimonials/', verbose_name="Firma Logosu")
    message = models.TextField(verbose_name="Müşteri Yorumu", max_length=300)
    is_active = models.BooleanField(default=True, verbose_name="Yayında mı?")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Oluşturulma Tarihi")
    order = models.IntegerField(default=0, verbose_name="Sıralama")

    class Meta:
        verbose_name = "Referans / Müşteri Yorumu"
        verbose_name_plural = "Referanslar"
        ordering = ['order', '-created_at']

    def __str__(self):
        return self.company_name