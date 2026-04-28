import secrets

from apps.accounts.models import *
from apps.customers.models import *
from apps.products.models import *
from apps.stores.models import *
from apps.workshops.models import Workshops


def _gen_public_token():
    return secrets.token_urlsafe(16)


class Repairs(models.Model):
    LOCATION_CHOICES = [
        ('store', 'Mağazada Beklemede'),
        ('workshop', 'Atölyede Tamirde'),
        ('ready_for_pickup', 'Teslimata Hazır'),
        ('delivered', 'Müşteriye Teslim Edildi'),
    ]

    JEWELRY_TYPE_CHOICES = [
        ('ring', 'Yüzük'),
        ('necklace', 'Kolye'),
        ('bracelet', 'Bileklik'),
        ('earring', 'Küpe'),
        ('watch', 'Saat'),
        ('other', 'Diğer'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tracking_code = models.CharField(max_length=200, null=True, blank=True)
    customer = models.ForeignKey(Customers, on_delete=models.CASCADE, related_name='repairs')
    workshop = models.ForeignKey(Workshops, on_delete=models.CASCADE, null=True, blank=True)
    product_type = models.CharField(max_length=200, null=True, blank=True, choices=JEWELRY_TYPE_CHOICES)
    product_description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    gram = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), blank=True, null=True)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)
    received_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='repairs_received')
    delivered_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='repairs_delivered')
    moved_to_workshop_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='repairs_moved_to_workshop_by'
    )
    ready_for_pickup_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='repairs_ready_for_pickup_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    moved_to_workshop_at = models.DateTimeField(null=True, blank=True)
    ready_for_pickup_at = models.DateTimeField(null=True, blank=True)
    image = models.ImageField(default="default/default.png", upload_to='Products/RepairProducts/', null=True,
                              blank=True)
    status = models.CharField(max_length=20, choices=LOCATION_CHOICES, default='store')
    public_token = models.CharField(max_length=64, unique=True, default=_gen_public_token, editable=False)
    public_token_created_at = models.DateTimeField(auto_now_add=True)
    is_completed = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)

    def __str__(self):
        return f"{self.customer} ({self.tracking_code})"

    class Meta:
        db_table = 'Repairs'
        indexes = [
            models.Index(fields=['store', 'status'], name='repairs_store_status_idx'),
            models.Index(fields=['store', 'is_deleted'], name='repairs_store_deleted_idx'),
            models.Index(fields=['customer'], name='repairs_customer_idx'),
            models.Index(fields=['status'], name='repairs_status_idx'),
            models.Index(fields=['-created_at'], name='repairs_created_at_desc_idx'),
        ]
