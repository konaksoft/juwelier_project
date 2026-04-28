import uuid
from apps.stores.models import *
from apps.accounts.models import Users
from django.db import models


class Brands(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='brand_created_by')
    created_on = models.DateTimeField()
    modified_by = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='brand_modified_by', null=True)
    modified_on = models.DateTimeField(null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)


    def __str__(self):
        return self.name

    class Meta:
        db_table = 'Brands'
