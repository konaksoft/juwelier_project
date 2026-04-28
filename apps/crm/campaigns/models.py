import uuid
from django.db import models
from django.utils import timezone
from apps.accounts.models import Users
from apps.stores.models import Stores
from apps.crm.leads.models import Lead

class Campaign(models.Model):
    CHANNELS = (
        ('instagram', 'instagram'),
        ('facebook', 'facebook'),
        ('tiktok', 'tiktok'),
        ('google', 'google'),
        ('other', 'other'),
    )
    STATUSES = (('draft', 'draft'), ('active', 'active'), ('paused', 'paused'), ('ended', 'ended'))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='crm_campaigns')
    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=16, choices=CHANNELS)
    status = models.CharField(max_length=10, choices=STATUSES, default='draft')
    utm_source = models.CharField(max_length=60, blank=True, null=True)
    utm_medium = models.CharField(max_length=60, blank=True, null=True)
    utm_campaign = models.CharField(max_length=120, blank=True, null=True)
    budget = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    start_on = models.DateField(blank=True, null=True)
    end_on = models.DateField(blank=True, null=True)

    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_Campaigns'
        ordering = ['-created_on']
        unique_together = ('store', 'name')


class CampaignTouch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='touches')
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='campaign_touches')
    first_touch = models.BooleanField(default=True)
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_CampaignTouches'
        unique_together = ('campaign', 'lead', 'first_touch')
