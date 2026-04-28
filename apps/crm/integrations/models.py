import uuid
from django.db import models
from django.utils import timezone
from apps.stores.models import Stores


class SocialChannel(models.Model):
    PROVIDERS = (
        ('instagram', 'instagram'),
        ('facebook', 'facebook'),
        ('tiktok', 'tiktok'),
        ('whatsapp', 'whatsapp'),
        ('website', 'website'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='crm_channels')
    provider = models.CharField(max_length=16, choices=PROVIDERS)
    handle = models.CharField(max_length=120, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    settings = models.JSONField(default=dict, blank=True)  # token/headers/webhook ayarları gibi
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_SocialChannels'
        unique_together = ('store', 'provider', 'handle')
        ordering = ['provider', 'handle']


class ImportQueue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(SocialChannel, on_delete=models.CASCADE, related_name='imports')
    payload = models.JSONField()  # ham sosyal medya/form datası
    mapped_lead_id = models.UUIDField(blank=True, null=True)
    status = models.CharField(max_length=16, default='pending')  # pending|processed|failed
    error = models.TextField(blank=True, null=True)
    created_on = models.DateTimeField(default=timezone.now)
    processed_on = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'CRM_ImportQueue'
        ordering = ['-created_on']


class WebhookLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(SocialChannel, on_delete=models.SET_NULL, null=True, related_name='webhook_logs')
    event = models.CharField(max_length=80)
    headers = models.JSONField(default=dict, blank=True)
    body = models.JSONField(default=dict, blank=True)
    received_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_WebhookLogs'
        ordering = ['-received_on']
