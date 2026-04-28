import uuid
from django.db import models
from django.utils import timezone
from apps.accounts.models import Users
from apps.stores.models import Stores
from apps.crm.leads.models import Lead

class Interaction(models.Model):
    MEDIUMS = (
        ('dm', 'dm'),
        ('whatsapp', 'whatsapp'),
        ('call', 'call'),
        ('sms', 'sms'),
        ('email', 'email'),
        ('visit', 'visit'),
        ('note', 'note'),
    )
    DIRECTIONS = (('in', 'in'), ('out', 'out'))
    OUTCOMES = (
        ('no_answer', 'no_answer'),
        ('follow_up', 'follow_up'),
        ('positive', 'positive'),
        ('negative', 'negative'),
        ('scheduled', 'scheduled'),
        ('converted', 'converted'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='interactions')
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='interactions')

    medium = models.CharField(max_length=16, choices=MEDIUMS)
    direction = models.CharField(max_length=4, choices=DIRECTIONS, default='out')
    subject = models.CharField(max_length=200, blank=True, null=True)
    content = models.TextField(blank=True, null=True)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, blank=True, null=True)

    scheduled_for = models.DateTimeField(blank=True, null=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name='interactions_created')
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_Interactions'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['store', 'lead']),
            models.Index(fields=['store', 'medium']),
        ]


class Task(models.Model):
    PRIORITIES = (('low', 'low'), ('normal', 'normal'), ('high', 'high'))
    STATUSES = (('open', 'open'), ('done', 'done'), ('cancelled', 'cancelled'))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='crm_tasks')
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='tasks')

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    due_at = models.DateTimeField(blank=True, null=True)
    assigned_to = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name='crm_tasks_assigned')
    priority = models.CharField(max_length=8, choices=PRIORITIES, default='normal')
    status = models.CharField(max_length=10, choices=STATUSES, default='open')

    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name='crm_tasks_created')
    created_on = models.DateTimeField(default=timezone.now)
    completed_on = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'CRM_Tasks'
        ordering = ['status', 'due_at']
        indexes = [
            models.Index(fields=['store', 'status']),
            models.Index(fields=['store', 'assigned_to']),
        ]


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='attachments')
    uploaded_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)
    file = models.FileField(upload_to='crm/')
    note = models.CharField(max_length=200, blank=True, null=True)
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_Attachments'
        ordering = ['-created_on']