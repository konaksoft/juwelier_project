# Dosya: apps/orders/models.py
from __future__ import annotations
import uuid
import random
from decimal import Decimal, ROUND_HALF_UP
from django.db import models, transaction
from django.utils import timezone
from apps.accounts.models import Users
from apps.stores.models import Stores
from apps.whatsapp.models import WhatsAppCreditRequest
from apps.crm.packages.models import Packages
# Proposal ve Device modellerini lazy reference (string) ile kullanıyoruz
from apps.crm.proposals.models import Proposals
from apps.crm.devices.models import Device


class MoneyCurrency(models.TextChoices):
    TRY = 'TRY', 'TRY'
    USD = 'USD', 'USD'
    EUR = 'EUR', 'EUR'
    HS = 'HS', 'HS'


def q2(x: Decimal) -> Decimal:
    return (x or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def generate_unique_order_no(prefix: str) -> str:
    """
    Belirtilen ön ek (prefix) ile 2 Harf + 5 Rakam (Örn: WP12345) formatında
    benzersiz bir sipariş numarası üretir.
    """
    while True:
        # 10000 ile 99999 arasında rastgele sayı
        rand_num = random.randint(10000, 99999)
        candidate = f"{prefix}{rand_num}"

        # Benzersizlik kontrolü
        if not Order.objects.filter(order_no=candidate).exists():
            return candidate


class Order(models.Model):
    class Type(models.TextChoices):
        WA_CREDIT = 'WA_CREDIT', 'WhatsApp Kontör'
        PACKAGE = 'PACKAGE', 'Paket Satın Alma'
        PROPOSAL = 'PROPOSAL', 'Teklif Onayı'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Taslak'
        PENDING = 'PENDING', 'Beklemede'
        APPROVED = 'APPROVED', 'Onaylandı'
        REJECTED = 'REJECTED', 'Reddedildi'
        CANCELED = 'CANCELED', 'İptal'
        FULFILLED = 'FULFILLED', 'Tamamlandı'

    class PaymentStatus(models.TextChoices):
        UNPAID = 'UNPAID', 'Ödenmedi'
        PARTIAL = 'PARTIAL', 'Kısmi'
        PAID = 'PAID', 'Ödendi'
        REFUNDED = 'REFUNDED', 'İade Edildi'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_no = models.CharField(max_length=40, unique=True, db_index=True)
    # sequence_no artık kullanılmıyor, DB hatası vermemesi için default 0 bırakıyoruz
    sequence_no = models.PositiveIntegerField(default=0)

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='orders')

    # Teklif İlişkisi
    proposal = models.OneToOneField('proposals.Proposals', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='order')

    requester = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name='orders_requested')
    approver = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='orders_approved')
    order_type = models.CharField(max_length=24, choices=Type.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    payment_status = models.CharField(max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    currency = models.CharField(max_length=8, choices=MoneyCurrency.choices, default=MoneyCurrency.TRY)
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    discount_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    tax_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    paid_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    notes = models.TextField(blank=True, default='')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Orders'
        indexes = [
            models.Index(fields=['store', 'created_at']),
            models.Index(fields=['store', 'status']),
            models.Index(fields=['order_type']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return self.order_no

    def recompute_totals(self, save: bool = True):
        # Teklif siparişlerinde tutarlar tekliften geldiği için otomatik hesaplamayı eziyoruz
        if self.order_type == self.Type.PROPOSAL:
            return

        sub = Decimal('0.00')
        for it in self.items.all():
            sub += q2(it.total)
        self.subtotal = q2(sub)
        self.discount_total = q2(Decimal('0'))
        self.tax_total = q2(Decimal('0'))
        self.grand_total = q2(self.subtotal)
        if save:
            self.save(update_fields=['subtotal', 'discount_total', 'tax_total', 'grand_total', 'updated_at'])

    def approve(self, by: Users | None = None, note: str = ''):
        self.status = Order.Status.APPROVED
        self.approver = by
        self.decided_at = timezone.now()
        self.decision_note = note or ''
        self.save(update_fields=['status', 'approver', 'decided_at', 'decision_note', 'updated_at'])

    def reject(self, by: Users | None = None, note: str = ''):
        self.status = Order.Status.REJECTED
        self.approver = by
        self.decided_at = timezone.now()
        self.decision_note = note or ''
        self.save(update_fields=['status', 'approver', 'decided_at', 'decision_note', 'updated_at'])

    def mark_paid(self, amount: Decimal | None = None):
        amt = amount if amount is not None else self.grand_total
        self.paid_total = q2((self.paid_total or Decimal('0')) + (amt or Decimal('0')))
        if self.paid_total >= self.grand_total:
            self.payment_status = Order.PaymentStatus.PAID
        elif self.paid_total > 0:
            self.payment_status = Order.PaymentStatus.PARTIAL
        self.save(update_fields=['paid_total', 'payment_status', 'updated_at'])

    def fulfill(self):
        # Sadece Onaylı ve Ödenmiş siparişler tamamlanır
        if self.status != Order.Status.APPROVED or self.payment_status != Order.PaymentStatus.PAID:
            return

        for it in self.items.all():
            # 1. WA Kontör
            if it.line_type == Order.Type.WA_CREDIT and it.wa_credit_request:
                settings = getattr(self.store, 'wa_settings', None)
                if settings:
                    settings.topup(int(it.quantity))
                it.wa_credit_request.status = WhatsAppCreditRequest.Status.APPROVED
                it.wa_credit_request.decided_by = self.approver
                it.wa_credit_request.decided_at = timezone.now()
                it.wa_credit_request.decision_note = self.decision_note or ''
                it.wa_credit_request.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])

            # 2. E-Fatura Kontör — DEVRE DIŞI (Nisan 2026)
            # Fatura kontör sistemi kaldırıldı. Mevcut sipariş kayıtları korunur
            # ancak yeni topup/onay işlemi yapılmaz.
            # if it.line_type == Order.Type.EINVOICE_CREDIT:
            #     st, _ = StoreEInvoiceSettings.objects.get_or_create(store=self.store)
            #     st.topup(int(it.quantity))
            #     if it.einv_credit_request:
            #         r = it.einv_credit_request
            #         r.status = EInvoiceCreditRequest.Status.APPROVED
            #         r.decided_by = self.approver
            #         r.decided_at = timezone.now()
            #         r.decision_note = self.decision_note or ''
            #         r.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])

            # 3. Paket (Tekliften veya direkt paketten)
            if it.package:
                self.store.package = it.package
                if not self.store.subscription_start:
                    self.store.subscription_start = timezone.now().date()
                if not self.store.is_active:
                    self.store.is_active = True
                self.store.save(update_fields=['package', 'subscription_start', 'is_active'])

        self.status = Order.Status.FULFILLED
        self.save(update_fields=['status', 'updated_at'])


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    line_type = models.CharField(max_length=24, choices=Order.Type.choices)

    package = models.ForeignKey(Packages, null=True, blank=True, on_delete=models.SET_NULL, related_name='order_items')

    # Cihaz İlişkisi
    device = models.ForeignKey('devices.Device', null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='order_items')

    wa_credit_request = models.OneToOneField(WhatsAppCreditRequest, null=True, blank=True, on_delete=models.SET_NULL,
                                             related_name='order_item')
    quantity = models.PositiveIntegerField(default=1)
    unit_label = models.CharField(max_length=20, default='adet')
    unit_price = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=8, choices=MoneyCurrency.choices, default=MoneyCurrency.TRY)
    total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'OrderItems'
        indexes = [
            models.Index(fields=['order']),
            models.Index(fields=['line_type']),
        ]

    def __str__(self):
        return f'{self.order.order_no} · {self.line_type}'

    def recompute(self, save: bool = True):
        self.total = q2((self.unit_price or Decimal('0')) * Decimal(self.quantity or 0))
        if save:
            self.save(update_fields=['total'])

    def save(self, *args, **kwargs):
        if not self.total or self.total == Decimal('0.00'):
            self.total = q2((self.unit_price or Decimal('0')) * Decimal(self.quantity or 0))
        super().save(*args, **kwargs)
        if self.order.order_type != Order.Type.PROPOSAL:
            self.order.recompute_totals(save=True)


def create_order_from_wa_credit_request(req: WhatsAppCreditRequest, unit_price: Decimal,
                                        currency: str = MoneyCurrency.TRY, note: str = '') -> Order:
    with transaction.atomic():
        number = generate_unique_order_no("WP")  # WhatsApp
        od = Order.objects.create(
            order_no=number,
            sequence_no=0,
            store=req.store,
            requester=req.requester,
            order_type=Order.Type.WA_CREDIT,
            status=Order.Status.PENDING,
            payment_status=Order.PaymentStatus.UNPAID,
            currency=currency,
            notes=note or req.note or ''
        )
        it = OrderItem.objects.create(
            order=od,
            line_type=Order.Type.WA_CREDIT,
            wa_credit_request=req,
            quantity=int(req.requested_amount or 0),
            unit_label='kontör',
            unit_price=q2(unit_price),
            currency=currency
        )
        it.recompute(save=True)
        od.recompute_totals(save=True)
        return od


def create_order_from_proposal(proposal, requester: Users | None = None) -> Order:
    """
    Proposals modelinden Order oluşturur.
    Requester parametresi eklendi.
    """
    with transaction.atomic():
        if hasattr(proposal, 'order') and proposal.order:
            return proposal.order

        if not proposal.company:
            raise ValueError("Teklifin bağlı olduğu bir şirket (Company) yok.")

        store = proposal.company.stores.first()
        if not store:
            raise ValueError("Şirkete bağlı bir mağaza (Store) bulunamadı.")

        number = generate_unique_order_no("TK")  # Teklif

        od = Order.objects.create(
            order_no=number,
            sequence_no=0,
            store=store,
            proposal=proposal,
            requester=requester or proposal.created_by,
            order_type=Order.Type.PROPOSAL,
            status=Order.Status.PENDING,
            payment_status=Order.PaymentStatus.UNPAID,
            currency=proposal.currency,
            subtotal=proposal.subtotal,
            discount_total=proposal.discount_amount,
            tax_total=proposal.tax_amount,
            grand_total=proposal.grand_total,
            notes=proposal.notes or ''
        )

        for p_item in proposal.items.all():
            l_type = Order.Type.PACKAGE if p_item.package else Order.Type.PROPOSAL

            OrderItem.objects.create(
                order=od,
                line_type=l_type,
                package=p_item.package,
                device=p_item.device,
                quantity=p_item.quantity,
                unit_label='adet',
                unit_price=q2(p_item.unit_price),
                currency=proposal.currency,
                total=q2(p_item.total_price),
                meta={'description': p_item.description}
            )

        return od