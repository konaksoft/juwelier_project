from django.db import models
from apps.accounts.models import *


class Categories(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True, null=True)
    order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'Categories'


