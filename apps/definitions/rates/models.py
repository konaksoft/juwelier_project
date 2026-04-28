import uuid
from apps.accounts.models import Users
from django.db import models

from apps.definitions.currencies.models import Currencies


class Rates(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, blank=False, null=False)
    currency_one = models.ForeignKey(Currencies, related_name='currency_one', on_delete=models.CASCADE, blank=True,
                                     null=True)
    currency_two = models.ForeignKey(Currencies, related_name='currency_two', on_delete=models.CASCADE, blank=True,
                                     null=True)
    buy_price = models.DecimalField(max_digits=10, decimal_places=4, verbose_name="Alış")
    sale_price = models.DecimalField(max_digits=10, decimal_places=4, verbose_name="Satış")
    modified_on = models.DateTimeField(blank=True, null=True)
    market_time = models.DateTimeField(max_length=20, verbose_name="Piyasa Tarihi", blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'Rates'
