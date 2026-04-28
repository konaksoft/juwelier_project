import uuid
from django.db import models
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce
from apps.stores.models import Stores
from apps.definitions.locations.models import City, District, TaxOffice
from decimal import Decimal


class Customers(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ManyToManyField(Stores, related_name='customers', blank=True)
    first_name = models.CharField(max_length=100, blank=False, null=False)
    last_name = models.CharField(max_length=100, blank=False, null=False)
    identification_number = models.CharField(max_length=11, null=True, blank=True)
    customer_number = models.CharField(max_length=25, null=True, blank=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    gender = models.CharField(max_length=15, blank=True, null=True)
    email = models.EmailField(max_length=255, blank=True, null=True)

    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İl")
    district = models.ForeignKey(District, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İlçe")
    tax_office = models.ForeignKey(TaxOffice, on_delete=models.SET_NULL, null=True, blank=True,
                                   verbose_name="Vergi Dairesi")

    tax_office_code = models.CharField(max_length=10, null=True, blank=True, verbose_name="Vergi Dairesi Kodu")
    address = models.TextField(blank=True, null=True)

    identification_front_image = models.ImageField(upload_to='customers/identity/', null=True, blank=True)
    identification_back_image = models.ImageField(upload_to='customers/identity/', null=True, blank=True)

    receivable_hs = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    payable_hs = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))

    is_email_verified = models.BooleanField(default=False, null=True, blank=True)
    is_phone_verified = models.BooleanField(default=False, null=True, blank=True)

    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    def _ledger_aggregate(self):
        agg = self.ledger_entries.filter(is_active=True).aggregate(
            total_debt=Coalesce(
                Sum('amount_hs', filter=Q(transaction_type='DEBT')),
                Decimal('0'),
            ),
            total_credit=Coalesce(
                Sum('amount_hs', filter=Q(transaction_type='CREDIT')),
                Decimal('0'),
            ),
        )
        return agg['total_debt'] or Decimal('0'), agg['total_credit'] or Decimal('0')

    @property
    def balance_hs(self):
        debt, credit = self._ledger_aggregate()
        return debt - credit

    @property
    def receivable_hs_computed(self):
        b = self.balance_hs
        return b if b > 0 else Decimal('0')

    @property
    def payable_hs_computed(self):
        b = self.balance_hs
        return -b if b < 0 else Decimal('0')

    class Meta:
        db_table = 'Customers'
        indexes = [
            models.Index(fields=['phone'], name='customers_phone_idx'),
            models.Index(fields=['identification_number'], name='customers_identification_idx'),
            models.Index(fields=['customer_number'], name='customers_customer_number_idx'),
            models.Index(fields=['is_deleted', 'is_active'], name='customers_deleted_active_idx'),
        ]


class CustomerLedger(models.Model):
    """Müşteri carisi (audit-trail) — R-Faz 4.

    `Customers.payable_hs` / `receivable_hs` artık doğrudan mutate edilmez;
    her bakiye hareketi bir CustomerLedger satırı olarak yazılır. İptal
    yolunda `is_active=False` ile pasifleştirilir; SupplierLedger FAZ 10
    deseniyle hayalet bakiye riski elenir.
    """

    TRANSACTION_TYPES = [
        ('DEBT', 'Borç (müşteri mağazaya borçlandı)'),
        ('CREDIT', 'Alacak (mağaza müşteriye borçlandı)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        Customers, on_delete=models.PROTECT, related_name='ledger_entries',
    )
    store = models.ForeignKey(
        Stores, on_delete=models.PROTECT, related_name='customer_ledger_entries',
        null=True, blank=True,
    )
    process_no = models.CharField(max_length=100, blank=True, default='')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    amount_hs = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    exchange_rate_tl = models.DecimalField(max_digits=18, decimal_places=6, default=Decimal('0.000000'))
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'CustomerLedger'
        indexes = [
            models.Index(fields=['customer', 'is_active'], name='cust_ledger_cust_active_idx'),
            models.Index(fields=['process_no'], name='cust_ledger_process_no_idx'),
            models.Index(fields=['transaction_type', 'is_active'], name='cust_ledger_type_active_idx'),
        ]
        ordering = ['-created_on']

    def __str__(self):
        return f"{self.customer} {self.transaction_type} {self.amount_hs} HS"
