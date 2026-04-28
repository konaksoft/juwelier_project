import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from django.db.models import Q
from apps.accounts.models import Users
from apps.stores.models import Stores


class Lead(models.Model):
    CHANNELS = (
        ('field', 'Saha'),
        ('instagram', 'instagram'),
        ('facebook', 'facebook'),
        ('tiktok', 'tiktok'),
        ('whatsapp', 'whatsapp'),
        ('website', 'website'),
        ('referral', 'referans'),
        ('fast_track', 'Hızlı Kayıt (Demo)'),
        ('other', 'diğer'),
    )
    STATUSES = (
        ('new', 'Yeni'),
        ('contacted', 'İletişime Geçildi'),
        ('qualified', 'Randevu Alındı'),
        ('proposal', 'Teklif'),
        ('negotiation', 'Görüşme Aşamasında'),
        ('won', 'Kazanıldı'),
        ('lost', 'Kaybedildi'),
        ('spam', 'Spam'),
        ('dnc', 'İletişime Geçilmeyecek'),
    )

    CATEGORY_CHOICES = (
        ('store', 'Kuyumcu Mağazası'),
        ('workshop', 'Kuyumcu Atölyesi'),
        ('diamond', 'Pırlantacı'),
        ('silver', 'Gümüşçü'),
        ('other', 'Diğer'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='leads', null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name='leads_created')
    # assigned_to alanını siliyoruz, yerine bunu ekliyoruz:
    assigned_users = models.ManyToManyField(Users, related_name='assigned_leads', blank=True,
                                            verbose_name="Atanan Kullanıcılar")

    full_name = models.CharField(max_length=150, blank=True, null=True)
    # YENİ ALAN:
    business_name = models.CharField(max_length=150, blank=True, null=True, verbose_name="Mağaza/Firma Adı")

    phone = models.CharField(max_length=32, blank=True, null=True)
    email = models.CharField(max_length=150, blank=True, null=True)

    channel = models.CharField(max_length=20, choices=CHANNELS, default='instagram')
    channel_handle = models.CharField(max_length=120, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='new')
    score = models.PositiveSmallIntegerField(default=0)

    last_activity_at = models.DateTimeField(blank=True, null=True)
    created_on = models.DateTimeField(default=timezone.now)
    updated_on = models.DateTimeField(auto_now=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, blank=True, null=True)
    city = models.CharField(max_length=64, blank=True, null=True)
    district = models.CharField(max_length=64, blank=True, null=True)

    class Meta:
        db_table = 'CRM_Leads'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['store', 'status']),
            models.Index(fields=['store', 'channel']),
            models.Index(fields=['store', 'created_on']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['store', 'phone'],
                name='uq_lead_phone_per_store',
                condition=Q(store__isnull=False, phone__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['store', 'email'],
                name='uq_lead_email_per_store',
                condition=Q(store__isnull=False, email__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['phone'],
                name='uq_lead_phone_global',
                condition=Q(store__isnull=True, phone__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['email'],
                name='uq_lead_email_global',
                condition=Q(store__isnull=True, email__isnull=False),
            ),
        ]

    def __str__(self):
        return f'{self.full_name or ""} ({self.channel})'.strip()


class LeadTag(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='lead_tags')
    name = models.CharField(max_length=40)

    class Meta:
        db_table = 'CRM_LeadTags'
        unique_together = ('store', 'name')
        ordering = ['name']

    def __str__(self):
        return self.name


class LeadTagMap(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='tag_maps')
    tag = models.ForeignKey(LeadTag, on_delete=models.CASCADE, related_name='tag_maps')
    added_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)
    added_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_LeadTagMap'
        unique_together = ('lead', 'tag')


class LeadStageHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='stage_history')
    from_status = models.CharField(
        max_length=20, choices=Lead.STATUSES, blank=True, null=True
    )
    to_status = models.CharField(
        max_length=20, choices=Lead.STATUSES
    )
    changed_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)
    changed_on = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=300, blank=True, null=True)

    class Meta:
        db_table = 'CRM_LeadStageHistory'
        ordering = ['-changed_on']


class LeadNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)
    body = models.TextField()
    created_on = models.DateTimeField(default=timezone.now)
    is_private = models.BooleanField(default=False)

    class Meta:
        db_table = 'CRM_LeadNotes'
        ordering = ['-created_on']


class LeadStateHistory(models.Model):
    STATES = (
        ('new', 'Yeni'),
        ('contacted', 'İletişime Geçildi'),
        ('qualified', 'Uygun Bulundu'),
        ('proposal', 'Teklif'),
        ('negotiation', 'Görüşme Aşamasında'),
        ('won', 'Kazanıldı'),
        ('lost', 'Kaybedildi'),
        ('spam', 'Spam'),
        ('dnc', 'İletişime Geçilmeyecek'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='state_history')
    state = models.CharField(max_length=32, choices=STATES)
    note = models.CharField(max_length=300, blank=True, null=True)
    changed_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)
    changed_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'CRM_LeadStateHistory'
        ordering = ['-changed_on']


class LeadActivity(models.Model):
    ACTIVITY_TYPES = (
        ('call', 'Telefon Görüşmesi'),
        ('meeting', 'Yüzyüze/Ziyaret'),
        ('presentation', 'Sunum'),
        ('demo', 'Demo Gösterimi'),
        ('whatsapp', 'WhatsApp/Mesaj'),
        ('email', 'E-Posta'),
    )

    OUTCOMES = (
        ('positive', 'Olumlu / İlgilendi'),
        ('negative', 'Olumsuz / İlgilenmedi'),
        ('offer_requested', 'Teklif İstedi'),
        ('offer_decision_pending', 'Teklif Kararı Bekleniyor'),
        ('demo_requested', 'Demo İstedi'),
        ('decision_pending', 'Karar Bekleniyor'),
        ('reached_voicemail', 'Ulaşılamadı'),
        ('scheduled', 'İleri Tarihe Randevu'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='activities')
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True)

    activity_type = models.CharField(max_length=20, choices=ACTIVITY_TYPES, default='call')
    outcome = models.CharField(max_length=30, choices=OUTCOMES, blank=True, null=True, verbose_name="Sonuç")

    summary = models.TextField(verbose_name="Görüşme Notları / Talepler")

    activity_date = models.DateTimeField(default=timezone.now, verbose_name="Gerçekleşme Zamanı")
    next_action_date = models.DateTimeField(blank=True, null=True, verbose_name="Sonraki Hatırlatma")

    class Meta:
        db_table = 'CRM_LeadActivities'
        ordering = ['-activity_date']


class PackageApplication(models.Model):
    APPLICATION_STATUSES = (
        ('pending', 'Beklemede'),
        ('contacted', 'İletişime Geçildi'),
        ('proposal_created', 'Teklif Oluşturuldu'),
        ('rejected', 'Reddedildi'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application_no = models.CharField(max_length=30, unique=True, editable=False, null=True, blank=True)

    first_name = models.CharField(max_length=80, verbose_name="Ad")
    last_name = models.CharField(max_length=80, verbose_name="Soyad")
    phone = models.CharField(max_length=32, verbose_name="Telefon")
    email = models.CharField(max_length=150, blank=True, null=True, verbose_name="E-Posta")
    business_name = models.CharField(max_length=200, verbose_name="Firma / Mağaza Adı")
    city = models.CharField(max_length=64, blank=True, null=True, verbose_name="İl")

    selected_modules = models.ManyToManyField(
        'packages.SaaSModule', blank=True,
        related_name='applications',
        verbose_name="Seçilen Modüller"
    )

    monthly_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name="Hesaplanan Aylık Toplam (₺)"
    )
    yearly_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        verbose_name="Hesaplanan Yıllık Toplam (₺)"
    )

    status = models.CharField(max_length=20, choices=APPLICATION_STATUSES, default='pending')

    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='package_applications',
                             verbose_name="Bağlı Lead Kaydı")
    proposal = models.ForeignKey('proposals.Proposals', on_delete=models.SET_NULL,
                                 null=True, blank=True,
                                 related_name='source_applications',
                                 verbose_name="Oluşturulan Teklif")

    utm_source = models.CharField(max_length=100, blank=True, null=True, verbose_name="UTM Kaynak",
                                  help_text="Hangi kanaldan geldi? Örn: whatsapp, instagram, google")
    notes = models.TextField(blank=True, null=True, verbose_name="Notlar / Ek Bilgi")

    created_on = models.DateTimeField(default=timezone.now)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'CRM_PackageApplications'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['phone']),
            models.Index(fields=['created_on']),
        ]

    def __str__(self):
        return f"{self.application_no} — {self.first_name} {self.last_name} ({self.business_name})"

    def save(self, *args, **kwargs):
        if not self.application_no:
            date_str = timezone.now().strftime('%Y%m%d')
            uid_chunk = str(uuid.uuid4())[:4].upper()
            self.application_no = f"B-{date_str}-{uid_chunk}"
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
