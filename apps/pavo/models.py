# FILE: apps/pavo/models.py
from __future__ import annotations
import uuid
from decimal import Decimal
from django.db import models
from apps.stores.models import Company, Stores
from apps.invoices.models import Invoice


class PavoAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="pavo_accounts")
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="pavo_accounts", null=True, blank=True)

    title = models.CharField(max_length=120, default="Varsayılan")
    provider = models.CharField(max_length=32, default="PAVO", choices=[("PAVO", "Pavo")])

    base_url = models.CharField(max_length=300, blank=True, null=True)
    api_key = models.CharField(max_length=200, blank=True, null=True)
    api_secret = models.CharField(max_length=200, blank=True, null=True)

    webhook_secret = models.CharField(max_length=200, blank=True, null=True)

    pairing_source_fingerprint = models.CharField(max_length=255, blank=True, null=True)
    pairing_target_serial_no = models.CharField(max_length=120, blank=True, null=True)
    pairing_application_name = models.CharField(max_length=120, blank=True, null=True)
    pairing_source_reference = models.CharField(max_length=120, blank=True, null=True)

    test_mode = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "PavoAccounts"
        unique_together = [("company", "store", "title")]
        indexes = [models.Index(fields=["company", "store", "is_active"])]

    def __str__(self):
        who = self.store or self.company
        return f"{who} · {self.title}"


class PavoTerminal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="pavo_terminals")
    title = models.CharField(max_length=120, default="Terminal")
    ip = models.CharField(max_length=64)
    port = models.IntegerField(blank=True, null=True)
    secure = models.BooleanField(default=True)
    serial_number = models.CharField(max_length=120)
    fingerprint = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    last_paired_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "PavoTerminals"
        unique_together = [("store", "serial_number")]
        indexes = [models.Index(fields=["store", "is_active"])]

    def __str__(self):
        return f"{self.store} · {self.title} · {self.serial_number}"


class PavoPayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(PavoAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="pavo_payments")
    external_id = models.CharField(max_length=100, db_index=True)
    pavo_id = models.CharField(max_length=120, unique=True, null=True, blank=True)
    payment_url = models.TextField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=6, default="TRY")
    status = models.CharField(max_length=32, default="CREATED")
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    raw_request = models.JSONField(default=dict, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "PavoPayments"
        indexes = [models.Index(fields=["external_id", "status"])]

    def __str__(self):
        return f"{self.external_id} · {self.status}"


class PavoLocalSale(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey("PavoTerminal", on_delete=models.SET_NULL, null=True, related_name="local_sales")
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="pavo_local_sales")
    request_payload = models.JSONField(default=dict)
    response_payload = models.JSONField(default=dict)
    status = models.CharField(max_length=32, default="SENT")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=6, default="TRY")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "PavoLocalSales"
        indexes = [models.Index(fields=["status", "created_at"])]


class PavoWebhookEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    headers = models.JSONField(default=dict)
    body = models.JSONField(default=dict)
    signature = models.CharField(max_length=255, blank=True, null=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True)
    pavo_payment = models.ForeignKey(PavoPayment, on_delete=models.SET_NULL, null=True, blank=True)
    processed = models.BooleanField(default=False)
    result = models.CharField(max_length=32, default="RECEIVED")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "PavoWebhookEvents"
        indexes = [models.Index(fields=["created_at", "processed"])]
