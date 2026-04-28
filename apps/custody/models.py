import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from apps.customers.models import *
from apps.accounts.models import *
from apps.stores.models import *
from apps.products.models import *


# apps/custody/models.py
class CustomerCustodyLedger(models.Model):
    CUSTODY_IN = 'IN'  # ürün bize bırakıldı
    CUSTODY_OUT = 'OUT'  # ürün teslim edildi

    custody_type = models.CharField(
        max_length=3,
        choices=[(CUSTODY_IN, 'Emanet Girişi'),
                 (CUSTODY_OUT, 'Emanet Çıkışı')],
        default=CUSTODY_IN
    )

    customer = models.ForeignKey(Customers, on_delete=models.CASCADE)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE)
    product = models.ForeignKey(Products, null=True, blank=True,
                                on_delete=models.SET_NULL)

    quantity_piece = models.PositiveIntegerField(default=0)
    quantity_gram = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    amount_hs = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    process_no = models.CharField(max_length=20, db_index=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    is_returned = models.BooleanField(default=False)

    created_on = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL,
                                   null=True, related_name='+')

    received_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    delivered_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'CustomerCustodyLedger'
        ordering = ['-created_on']

