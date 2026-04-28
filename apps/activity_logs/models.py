import uuid
from django.db import models

from apps.accounts.models import *


class ActivityLogs(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=400)
    description = models.TextField()
    ip_address = models.GenericIPAddressField()
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE)
    created_on = models.DateTimeField()
    is_deleted = models.BooleanField(default=False)

    def _str_(self):
        return self.id

    class Meta:
        db_table = 'ActivityLogs'
        indexes = [
            models.Index(fields=['created_by'], name='activitylogs_created_by_idx'),
            models.Index(fields=['-created_on'], name='actlogs_cre_desc_idx'),
            models.Index(fields=['is_deleted'], name='activitylogs_is_deleted_idx'),
        ]






