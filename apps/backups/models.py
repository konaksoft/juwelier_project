from django.db import models
from apps.stores.models import Company
import os, uuid


class CompanyBackup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='backups')
    backup_file = models.FileField(upload_to='CompanyBackups/')
    file_size = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by_user = models.CharField(max_length=150, blank=True, null=True)  # Yedek alan kişi
    note = models.TextField(blank=True, null=True)
    STATUS_CHOICES = [
        ('PENDING', 'Hazırlanıyor'),
        ('COMPLETED', 'Tamamlandı'),
        ('FAILED', 'Hata'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='COMPLETED')

    class Meta:
        db_table = 'CompanyBackup'
        ordering = ['-created_at']
        verbose_name = "Firma Yedeği"
        verbose_name_plural = "Firma Yedekleri"

    def __str__(self):
        return f"{self.company.title} - {self.created_at.strftime('%d.%m.%Y %H:%M')}"

    def save(self, *args, **kwargs):
        # Dosya boyutu hesaplama
        if self.backup_file and hasattr(self.backup_file, 'size'):
            mb_size = self.backup_file.size / (1024 * 1024)
            self.file_size = f"{mb_size:.2f} MB"
        super().save(*args, **kwargs)
