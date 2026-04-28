from django.db import models
from apps.accounts.models import *
from apps.products.models import *
from apps.suppliers.models import Suppliers
from django.utils import timezone


class Bracelets(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Products, on_delete=models.CASCADE, null=True, blank=True, related_name='bracelets_products')
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='bracelets', null=True, blank=True)
    created_on = models.DateTimeField(auto_now_add=True)
    is_status = models.BooleanField(default=True, null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)

    class Meta:
        db_table = 'Bracelets'
