import uuid
from django.db import models
from apps.accounts.models import *


class Currencies(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField(null=True, blank=True)
    code = models.TextField(null=True, blank=True)
    symbol = models.CharField(max_length=3, null=True, blank=True)
    digit_group_separator = models.TextField(null=True, blank=True)
    decimal_character = models.TextField(null=True, blank=True)
    round = models.CharField(max_length=5, null=True, blank=True)
    splice = models.CharField(max_length=1, null=True, blank=True)
    after_comma = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='currencies_created_by')
    created_on = models.DateTimeField()
    modified_by = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='currencies_modified_by', null=True)
    modified_on = models.DateTimeField(null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'Currencies'
        ordering = ['name']




