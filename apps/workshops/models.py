import uuid
import secrets
from django.db import models
from apps.accounts.models import *
from apps.stores.models import *

def _gen_public_token():
    return secrets.token_urlsafe(16)

class Workshops(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company_name = models.CharField(max_length=100, blank=False, null=False)
    person_name = models.CharField(max_length=100, blank=False, null=False)
    person_surname = models.CharField(max_length=100, blank=False, null=False)
    email = models.EmailField(max_length=100, blank=True, null=True)
    phone = models.CharField(max_length=100, blank=True, null=True)
    company_address = models.CharField(max_length=100, blank=True, null=True)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, null=True, blank=True)

    public_token = models.CharField(max_length=64, default=_gen_public_token, editable=False)

    is_active = models.BooleanField(default=True, null=True, blank=True)
    is_deleted = models.BooleanField(default=False, null=True, blank=True)


    class Meta:
        db_table = 'Workshops'
        ordering = ['company_name']

