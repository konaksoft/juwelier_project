# apps/whatsapp/models.py
from __future__ import annotations

from datetime import date
from django.db import models
from django.utils import timezone

from apps.stores.models import Stores
from apps.accounts.models import Users


# ------------------------------------------------------------
# Yardımcılar
# ------------------------------------------------------------
def _safe_localdate():
    """USE_TZ kapalı/yanlış konfigürasyonlarda da güvenle bugün tarihini üretir."""
    try:
        return timezone.localdate()
    except Exception:
        return date.today()


# ------------------------------------------------------------
# Sabitler / Tipler
# ------------------------------------------------------------
class MessageType(models.TextChoices):
    GENERIC = "GENERIC", "Genel"
    TWO_FA = "2FA", "İki Aşamalı Doğrulama"
    REPAIR = "REPAIR", "Tamir İşlem Özeti"
    OP_SUM = "OP_SUM", "Perakende İşlem Özeti"


# ------------------------------------------------------------
# Mağaza ayarları + kota/sayaç
# ------------------------------------------------------------
class StoreWhatsAppSettings(models.Model):
    store = models.OneToOneField(Stores, on_delete=models.CASCADE, related_name="wa_settings")

    enabled = models.BooleanField(default=True)

    allow_generic = models.BooleanField(default=True)
    allow_two_fa = models.BooleanField(default=True)
    allow_repair = models.BooleanField(default=True)
    allow_op_sum = models.BooleanField(default=True)

    allowed_templates = models.JSONField(default=list, blank=True)

    # --- ESKİ limitler (kalsın ama artık "kontör" varken görmezden geleceğiz) ---
    daily_limit = models.PositiveIntegerField(null=True, blank=True)
    monthly_limit = models.PositiveIntegerField(null=True, blank=True)

    # --- Sayaçlar (kullandığımız kullanım istatistiği için) ---
    daily_count = models.PositiveIntegerField(default=0)
    daily_reset = models.DateField(null=True, blank=True)
    monthly_count = models.PositiveIntegerField(default=0)
    monthly_reset = models.DateField(null=True, blank=True)

    # --- YENİ: Kontör bakiyesi ---
    credit_balance = models.PositiveIntegerField(default=250, null=True, blank=True)  # None → sınırsız
    low_balance_threshold = models.PositiveIntegerField(null=True, blank=True)
    last_topup_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ...

    def can_send_now(self) -> tuple[bool, str | None]:
        """
        (ok, reason) → reason ∈ {'DISABLED','CREDIT_EMPTY','DAILY_LIMIT','MONTHLY_LIMIT', None}
        KONTÖR TANIMLIYSA ÖNCE ONU DENETLER.
        """
        if not self.enabled:
            return False, "DISABLED"

        # Kontör varsa önce onu kontrol et
        if self.credit_balance is not None and self.credit_balance <= 0:
            return False, "CREDIT_EMPTY"

        # (Geri uyumluluk) Kontör yoksa eski limit mantığını kullan
        if self.daily_limit is not None and self.daily_count >= self.daily_limit:
            return False, "DAILY_LIMIT"
        if self.monthly_limit is not None and self.monthly_count >= self.monthly_limit:
            return False, "MONTHLY_LIMIT"
        return True, None

    def consume(self, n: int = 1):
        today = _safe_localdate()

        if self.daily_reset != today:
            self.daily_count = 0
            self.daily_reset = today

        if self.monthly_reset is None or self.monthly_reset.month != today.month or self.monthly_reset.year != today.year:
            self.monthly_count = 0
            self.monthly_reset = today

        self.daily_count += n
        self.monthly_count += n

        if self.credit_balance is not None:
            if self.credit_balance >= n:
                self.credit_balance -= n
            else:
                self.credit_balance = 0

        self.save(update_fields=[
            "daily_count", "monthly_count", "daily_reset", "monthly_reset",
            "credit_balance", "updated_at"
        ])

    def topup(self, n: int):
        if n and n > 0:
            self.credit_balance = (self.credit_balance or 0) + n
            self.last_topup_at = timezone.now()
            self.save(update_fields=["credit_balance", "last_topup_at", "updated_at"])

    def is_template_allowed(self, tpl_name: str, msg_type: str | None = None) -> bool:
        """
        Şablon izni kontrolü.
        1) Mesaj türü bayraklarını denetler (GENERIC/2FA/REPAIR/OP_SUM).
        2) allowed_templates listesi doluysa whitelist uygular (yalnız listedekilere izin).
        Not: self.enabled kontrolü servis tarafında ayrıca yapılıyor.
        """
        # --- 1) Tür bazlı politika ---
        mt = (msg_type or "").upper()
        if mt == MessageType.TWO_FA:
            if not self.allow_two_fa:
                return False
        elif mt == MessageType.REPAIR:
            if not self.allow_repair:
                return False
        elif mt == MessageType.OP_SUM:
            if not self.allow_op_sum:
                return False
        else:
            # GENERIC veya tanınmayan tipler
            if not self.allow_generic:
                return False

        # --- 2) Allowlist (whitelist) ---
        allowlist = self.allowed_templates or []
        if len(allowlist) > 0:
            return tpl_name in allowlist

        # --- 3) Varsayılan davranış ---
        # Geriye uyumluluk için allowlist boşsa "tür bayraklarına" göre izin ver.
        # Eğer “superuser onaylamadan kullanılamasın” istiyorsanız True yerine False döndürün.
        return True
        # return False  # (daha sıkı politika için bu satırı kullanın)


# ------------------------------------------------------------
# Şablon kataloğu (global)
# ------------------------------------------------------------
class WhatsAppTemplateCatalog(models.Model):
    """
    Meta'da var olan ve sistemin kullanacağı şablonların global kataloğu.
    Mağaza bazlı izin/filtreleme StoreWhatsAppSettings.allowed_templates ile yapılır.
    """
    name = models.CharField(max_length=120, unique=True)  # Cloud API template.name
    title = models.CharField(max_length=150, blank=True, default="")
    category = models.CharField(max_length=32, blank=True, default="")  # TRANSACTIONAL / AUTHENTICATION / MARKETING
    default_language = models.CharField(max_length=16, default="tr_TR")

    header_placeholders = models.PositiveIntegerField(default=0)
    body_placeholders = models.PositiveIntegerField(default=0)
    button_placeholders = models.PositiveIntegerField(default=0)

    languages = models.JSONField(default=list, blank=True)  # ör: ["tr_TR","en_US"]
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)

    last_synced = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'WhatsAppTemplateCatalog'
        verbose_name = "WA Şablon (Katalog)"
        verbose_name_plural = "WA Şablon Kataloğu"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ------------------------------------------------------------
# Sohbet (conversation) ve mesajları
# ------------------------------------------------------------
class WhatsAppConversation(models.Model):
    """Mağaza ile bir müşteri numarası arasındaki tekil sohbet."""
    DIRECTION = (("CUSTOMER", "Müşteri Başlattı"), ("STORE", "Mağaza Başlattı"))
    STATUS = (("OPEN", "Açık"), ("CLOSED", "Kapalı"))

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="wa_conversations")
    phone_number = models.CharField(max_length=32, db_index=True)  # müşteri numarası (E.164)
    started_by = models.CharField(max_length=16, choices=DIRECTION, default="CUSTOMER")
    status = models.CharField(max_length=16, choices=STATUS, default="OPEN")
    subject = models.CharField(max_length=160, blank=True, default="")

    unread_count = models.PositiveIntegerField(default=0)  # mağaza tarafından okunmamış inbound sayısı
    last_message_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'WhatsAppConversation'
        ordering = ["-last_message_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "phone_number"], name="uq_conv_store_phone"),
        ]
        ordering = ["-last_message_at"]

    def __str__(self) -> str:
        store_code = getattr(self.store, "store_id", self.store_id if hasattr(self, "store_id") else self.store.pk)
        return f"{store_code} · {self.phone_number}"


class WhatsAppChatMessage(models.Model):
    """Sohbet içindeki tekil mesaj (free-text, media veya template özet kaydı)."""
    DIRECTION = (("IN", "Inbound"), ("OUT", "Outbound"))
    KIND = (("TEXT", "Text"), ("MEDIA", "Media"), ("TEMPLATE", "Template"))

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="wa_chat_messages")
    conversation = models.ForeignKey(WhatsAppConversation, on_delete=models.CASCADE, related_name="messages")
    user = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL)  # OUT ise genelde dolu
    customer = models.ForeignKey("customers.Customers", null=True, blank=True, on_delete=models.SET_NULL)

    direction = models.CharField(max_length=3, choices=DIRECTION)
    kind = models.CharField(max_length=16, choices=KIND, default="TEXT")

    wa_message_id = models.CharField(max_length=100, blank=True, default="")  # Meta message id
    from_number = models.CharField(max_length=32, blank=True, default="")
    to_number = models.CharField(max_length=32, blank=True, default="")

    text = models.TextField(blank=True, default="")
    media = models.JSONField(default=dict, blank=True)  # {type, mime, url, sha256, caption?}
    template_name = models.CharField(max_length=120, blank=True, default="")  # kind=TEMPLATE ise isim
    template_params = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=24, default="DELIVERED", blank=True)
    error = models.TextField(blank=True, default="")

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'WhatsAppChatMessage'
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["store", "conversation", "timestamp"])]

    def __str__(self) -> str:
        base = self.text or self.template_name or ""
        return f"{self.direction} · {base[:30]}"


# ------------------------------------------------------------
# Gönderim logları + aktiviteler
# ------------------------------------------------------------
class WhatsAppMessageLog(models.Model):
    class Status(models.TextChoices):
        SENT = "SENT", "Gönderildi"
        FAILED = "FAILED", "Hata"
        BLOCKED_QUOTA = "BLOCKED_QUOTA", "Kota Engeli"
        BLOCKED_POLICY = "BLOCKED_POLICY", "Politika Engeli"

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="wa_messages")
    user = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL)
    customer = models.ForeignKey("customers.Customers", null=True, blank=True, on_delete=models.SET_NULL)

    # (opsiyonel) ilgili sohbet
    conversation = models.ForeignKey("WhatsAppConversation", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="logs")

    to = models.CharField(max_length=32)
    template = models.CharField(max_length=120)
    language = models.CharField(max_length=16, default="tr_TR")
    msg_type = models.CharField(max_length=16, choices=MessageType.choices, default=MessageType.GENERIC)

    header_params = models.JSONField(null=True, blank=True)
    body_params = models.JSONField(null=True, blank=True)
    button_params = models.JSONField(null=True, blank=True)

    response_id = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices)
    error_code = models.CharField(max_length=32, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'WhatsAppMessageLog'
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store", "created_at"]),
            models.Index(fields=["store", "status"]),
            models.Index(fields=["template"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.store_id if hasattr(self, 'store_id') else self.store_id} · {self.template} · {self.status}"


class WhatsAppActivity(models.Model):
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="wa_activities")
    message = models.ForeignKey(WhatsAppMessageLog, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="activities")
    actor = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=32, default="SEND")  # SEND / ERROR / BLOCK vs.
    description = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'WhatsAppActivity'
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.kind} · {self.created_at:%Y-%m-%d %H:%M}"


class StoreWhatsAppEndpoint(models.Model):
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="wa_endpoints")
    phone_number_id = models.CharField(max_length=40, unique=True)  # metadata.phone_number_id
    display_phone_number = models.CharField(max_length=32, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'StoreWhatsAppEndpoint'
        verbose_name = "WA Uç Noktası"
        verbose_name_plural = "WA Uç Noktaları"
        indexes = [models.Index(fields=["store", "is_active"])]

    def __str__(self):
        return f"{self.store} · {self.display_phone_number or self.phone_number_id}"


class WhatsAppCreditRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Beklemede"
        APPROVED = "APPROVED", "Onaylandı"
        REJECTED = "REJECTED", "Reddedildi"

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="wa_credit_requests")
    requester = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="wa_credit_requests")
    requested_amount = models.PositiveIntegerField()
    note = models.TextField(blank=True, default="")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="wa_credit_decisions")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'WhatsAppCreditRequest'
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store", "status"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.store_id if hasattr(self, 'store_id') else self.store_id} · {self.requested_amount} · {self.status}"
