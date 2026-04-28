import uuid
import threading
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from django.db import models, transaction
from typing import Iterable
from django.db.models import QuerySet
from apps.products.views import compute_store_has_tl

# --- Mevcut Uygulama İçe Aktarımları ---
from apps.stores.models import Stores
from apps.customers.models import Customers
from apps.products.models import Products
from apps.process.models import Process, Payment
from apps.accounts.models import Users
from apps.suppliers.models import Suppliers  # YENİ: Tedarikçi Modeli eklendi

_invoice_number_ctx = threading.local()


# =========================
# ENUMS & HELPERS
# =========================

class MoneyCurrency(models.TextChoices):
    TRY = 'TRY', 'Türk Lirası'
    USD = 'USD', 'Amerikan Doları'
    EUR = 'EUR', 'Euro'
    HS = 'HS', 'Has Altın'


def q2(x: Decimal) -> Decimal:
    return (x or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def q3(x: Decimal) -> Decimal:
    return (x or Decimal('0')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


class InvoiceSequence(models.Model):
    """
    Mağaza + Yıl + Belge Tipi bazlı numaratör.
    Örn: 2026 yılında Satış Faturaları 1'den başlar, Gider Pusulaları ayrı seriden gidebilir.
    """

    class SequenceType(models.TextChoices):
        INVOICE = 'INV', 'Fatura Serisi'
        EXPENSE = 'EXP', 'Gider Pusulası Serisi'
        PROFORMA = 'PRF', 'Proforma Serisi'

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='invoice_sequences')
    year = models.PositiveIntegerField()
    seq_type = models.CharField(max_length=3, choices=SequenceType.choices, default=SequenceType.INVOICE)
    last_no = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = (('store', 'year', 'seq_type'),)
        indexes = [models.Index(fields=['store', 'year', 'seq_type'])]

    def __str__(self):
        return f"{self.store} · {self.year} · {self.seq_type} · {self.last_no}"


# =========================
# MODEL: Invoice (Genişletilmiş)
# =========================

class Invoice(models.Model):
    # İşlem Yönü (Alış / Satış)
    class Type(models.TextChoices):
        SALE = 'SALE', 'Satış'  # Müşteriye Satış
        PURCHASE = 'PURCHASE', 'Alış'  # Tedarikçiden Alış veya Müşteriden Alış (Gider Pusulası)
        RETURN = 'RETURN', 'İade'  # Satış İadesi
        SERVICE = 'SERVICE', 'Hizmet'

    # Belge Sınıfı (Proforma mı, E-Belge mi?)
    class DocumentClass(models.TextChoices):
        PROFORMA = 'PROFORMA', 'Proforma / Ön Fatura'
        E_INVOICE = 'E_INVOICE', 'E-Fatura'
        E_ARCHIVE = 'E_ARCHIVE', 'E-Arşiv Fatura'
        EXPENSE_VOUCHER = 'EXPENSE', 'Gider Pusulası'
        PAPER = 'PAPER', 'Kağıt Fatura'

    # GİB Senaryosu (Entegrasyon için)
    class Scenario(models.TextChoices):
        BASIC = 'TEMELFATURA', 'Temel Fatura'
        COMMERCIAL = 'TICARIFATURA', 'Ticari Fatura'
        EARSIV = 'EARSIVFATURA', 'E-Arşiv Fatura'  # Normal E-Arşiv
        EARSIV_INTERNET = 'EARSIVINTERNET', 'E-Arşiv (İnternet Satışı)'

    # Durum
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Taslak / Proforma'
        QUEUED = 'QUEUED', 'Gönderim Sırasında'
        ISSUED = 'ISSUED', 'Kesildi (Resmileşti)'
        SENT = 'SENT', 'GİB’e Gönderildi'
        APPROVED = 'APPROVED', 'Onaylandı (GİB/Müşteri)'
        REJECTED = 'REJECTED', 'Reddedildi'
        CANCELED = 'CANCELED', 'İptal Edildi'
        ERROR = 'ERROR', 'Hata Aldı'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- İlişkiler ---
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='invoices')

    # Fatura kime kesiliyor veya kimden alınıyor? (Sadece biri dolu olmalı)
    customer = models.ForeignKey(Customers, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    supplier = models.ForeignKey(Suppliers, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')

    # Hızlı işlemle birebir bağ (Opsiyonel)
    process = models.ForeignKey(Process, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')

    # --- Fatura Kimlik Bilgileri ---
    invoice_no = models.CharField(max_length=32, db_index=True, help_text="ERP içindeki takip no (Örn: 20260001)")
    document_number = models.CharField(max_length=16, blank=True, null=True, db_index=True,
                                       help_text="Resmi GİB Numarası (Örn: GIB202600000001)")
    ettn = models.UUIDField(null=True, blank=True, help_text="GİB Evrensel Tekil Tanımlama Numarası")

    sequence_no = models.PositiveIntegerField()
    issue_date = models.DateTimeField(default=timezone.now)
    due_date = models.DateField(null=True, blank=True, verbose_name="Vade Tarihi")

    # --- Tür ve Sınıflandırma ---
    invoice_type = models.CharField(max_length=12, choices=Type.choices, default=Type.SALE)
    doc_class = models.CharField(max_length=20, choices=DocumentClass.choices, default=DocumentClass.PROFORMA)
    scenario = models.CharField(max_length=20, choices=Scenario.choices, default=Scenario.BASIC)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)

    # --- Para Birimi ---
    currency = models.CharField(max_length=8, choices=MoneyCurrency.choices, default=MoneyCurrency.TRY)
    exrate_to_try = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal('1.000000'))
    hs_to_try = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True,
                                    help_text="İşlem anındaki Has Altın kuru")

    # --- Toplamlar ---
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    discount_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    tax_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    # E-Fatura KDV Ayrımı (Örn: { "0": 1000.00, "20": 200.00 })
    tax_breakdown = models.JSONField(default=dict, blank=True, null=True)

    paid_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    # --- Entegrasyon ve Dosyalar ---
    is_einvoice = models.BooleanField(default=False)
    gib_uuid = models.CharField(max_length=36, blank=True, default="")  # Pavo veya GIB'den dönen ID
    gib_status_code = models.CharField(max_length=10, blank=True, default="",
                                       help_text="GİB Durum Kodu (1000, 1200, 1300)")
    gib_status_desc = models.CharField(max_length=255, blank=True, default="")
    gib_error = models.TextField(blank=True, default="")

    xml_file = models.FileField(upload_to='Invoices/xml/', null=True, blank=True)
    pdf_file = models.FileField(upload_to='Invoices/pdf/', null=True, blank=True)
    html_content = models.TextField(blank=True, null=True, help_text="Önizleme için HTML")

    # --- Pavo POS Entegrasyonu ---
    pavo_sale_number = models.CharField(max_length=64, blank=True, default="")
    pavo_invoice_no = models.CharField(max_length=64, blank=True, default="")
    pavo_sale_data = models.JSONField(null=True, blank=True)

    # --- Diğer ---
    notes = models.TextField(blank=True, default="")
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Invoices'
        ordering = ['-issue_date']
        indexes = [
            models.Index(fields=['store', 'issue_date']),
            models.Index(fields=['store', 'status']),
            models.Index(fields=['customer']),
            models.Index(fields=['supplier']),
            models.Index(fields=['invoice_no']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['store', 'invoice_no', 'invoice_type'],
                                    name='uniq_invoice_no_per_store_type'),
        ]

    def __str__(self):
        return f"{self.invoice_no} ({self.get_doc_class_display()})"

    @property
    def balance(self) -> Decimal:
        return q2((self.grand_total or Decimal('0')) - (self.paid_total or Decimal('0')))

    @property
    def is_paid(self) -> bool:
        # Küçük küsurat farklarını tolere et
        return self.balance <= Decimal('0.05')

    def recompute_totals(self, save: bool = True):
        subtotal = discount = tax = total = Decimal('0.00')
        breakdown = {}

        for it in self.items.all():
            subtotal += q2(it.total_excl_vat)
            discount += q2(it.discount_amount)
            vat_val = q2(it.vat_amount)
            tax += vat_val
            total += q2(it.total_incl_vat)

            # KDV Dağılımı Hesapla
            rate_key = str(int(it.vat_rate))
            if rate_key not in breakdown:
                breakdown[rate_key] = {'base': Decimal('0'), 'tax': Decimal('0')}
            breakdown[rate_key]['base'] += q2(it.total_excl_vat)
            breakdown[rate_key]['tax'] += vat_val

        # Decimal objelerini string'e veya float'a çevirmek gerekebilir JSON için
        # Ancak Django JSONField Decimal'i destekler, değilse serialize ederken dikkat.
        # Burada basitlik adına Decimal olarak bırakıyoruz, serializer halleder.
        final_breakdown = {}
        for k, v in breakdown.items():
            final_breakdown[k] = {
                'matrah': float(v['base']),
                'kdv_tutari': float(v['tax'])
            }

        self.subtotal = q2(subtotal)
        self.discount_total = q2(discount)
        self.tax_total = q2(tax)
        self.grand_total = q2(total)
        self.tax_breakdown = final_breakdown

        if save:
            self.save(
                update_fields=['subtotal', 'discount_total', 'tax_total', 'grand_total', 'tax_breakdown', 'updated_at'])

    @classmethod
    def set_number_override(cls, number):
        v = str(number or "").strip()
        _invoice_number_ctx.value = v if v else None

    @classmethod
    def clear_number_override(cls):
        if hasattr(_invoice_number_ctx, "value"):
            delattr(_invoice_number_ctx, "value")

    @classmethod
    def next_number_for(cls, store=None, invoice_date=None, doc_class=None, **kwargs):
        """
        Belge tipine göre sıradaki numarayı üretir.
        doc_class: 'EXPENSE_VOUCHER' ise Gider Pusulası serisi, yoksa Fatura serisi.
        """
        override = getattr(_invoice_number_ctx, "value", None)
        if override:
            return str(override), None

        if store is None:
            raise ValueError("store zorunludur")

        d = invoice_date or timezone.now().date()
        year = int(getattr(d, "year", timezone.now().year))

        # Seri tipini belirle
        seq_type = InvoiceSequence.SequenceType.INVOICE
        if doc_class == cls.DocumentClass.EXPENSE_VOUCHER:
            seq_type = InvoiceSequence.SequenceType.EXPENSE
        elif doc_class == cls.DocumentClass.PROFORMA:
            seq_type = InvoiceSequence.SequenceType.PROFORMA

        # Ön ek belirle (Opsiyonel)
        prefix = ""
        if seq_type == InvoiceSequence.SequenceType.EXPENSE:
            prefix = "GDR"
        elif seq_type == InvoiceSequence.SequenceType.PROFORMA:
            prefix = "PRF"

        try:
            with transaction.atomic():
                seq_obj, created = InvoiceSequence.objects.select_for_update().get_or_create(
                    store=store,
                    year=year,
                    seq_type=seq_type,
                    defaults={"last_no": 0},
                )

                seq_obj.last_no = int(seq_obj.last_no or 0) + 1
                seq_obj.save(update_fields=["last_no"])

                sequence_no = seq_obj.last_no

                # Numara Formatı: YIL + 0000X (Örn: 202600001 veya PRF202600001)
                invoice_no = f"{prefix}{year}{sequence_no:05d}"

            return invoice_no, sequence_no

        except Exception:
            # Fallback
            ts = timezone.now().strftime("%Y%m%d%H%M%S")
            return f"TMP{ts}", None


class InvoiceItem(models.Model):
    class Unit(models.TextChoices):
        GRAM = 'GR', 'Gram'
        PIECE = 'AD', 'Adet'
        CM = 'CM', 'Santim'
        KG = 'KG', 'Kilogram'

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Products, on_delete=models.SET_NULL, null=True, blank=True)

    # Ürün anlık fotoğrafı (Snapshot)
    product_name = models.CharField(max_length=255)
    barcode = models.CharField(max_length=50, blank=True, default="")
    jewelry_type = models.CharField(max_length=100, blank=True, default="")
    is_gram_bullion = models.BooleanField(default=True)

    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    unit = models.CharField(max_length=4, choices=Unit.choices, default=Unit.GRAM)

    # Birim fiyat (Fatura para birimi cinsinden)
    unit_price = models.DecimalField(max_digits=15, decimal_places=3, default=Decimal('0.000'))

    # Kuyumculuk özel alanlar
    price_hs = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Birim Has Fiyatı")
    hs_to_try = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)

    discount_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))  # %
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    # KDV
    vat_rate = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('0.00'))  # 0, 1, 10, 20
    vat_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    # Tevkifat (Opsiyonel - Hurda alımlarında gerekebilir)
    withholding_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'),
                                           help_text="Tevkifat Oranı (Varsa)")
    withholding_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    total_excl_vat = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    total_incl_vat = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))

    # KP-06: İstisna gerekçesi — GİB ISTISNA tipi faturalarda zorunlu alan
    exemption_reason = models.CharField(
        max_length=255, blank=True, default='',
        help_text="İstisna gerekçesi (Örn: KDVK Madde 17/4-g — Külçe altın teslimi)"
    )

    notes = models.CharField(max_length=255, blank=True, default="", help_text="Satır açıklaması (Örn: KDVK 17/4-g)")

    class Meta:
        db_table = 'InvoiceItems'
        indexes = [models.Index(fields=['invoice'])]

    def __str__(self):
        return f"{self.product_name} × {self.quantity}"

    def recompute(self, save: bool = True):
        """
        Satır toplamlarını hesaplar.
        """
        raw_total = q2(q3(self.unit_price) * q3(self.quantity))  # Brüt Tutar

        # İskonto
        disc = q2((raw_total * (self.discount_rate or Decimal('0.00'))) / Decimal('100'))

        # Matrah
        base = q2(raw_total - disc)

        # KDV
        vat = q2((base * (self.vat_rate or Decimal('0.00'))) / Decimal('100'))

        # Tevkifat (Varsa KDV'den düşülür ama genelde fatura toplamını etkileyişi senaryoya göre değişir)
        # Şimdilik basit tutuyoruz, tevkifat raporlama içindir.

        ttl = q2(base + vat)

        self.discount_amount = disc
        self.total_excl_vat = base
        self.vat_amount = vat
        self.total_incl_vat = ttl

        if save:
            self.save(update_fields=[
                'discount_amount', 'total_excl_vat', 'vat_amount', 'total_incl_vat', 'unit_price'
            ])


class InvoicePaymentAllocation(models.Model):
    """
    Hangi ödemenin hangi faturaya ait olduğunu takip eder.
    """
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='allocations')
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='invoice_allocations')
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = 'InvoicePaymentAllocations'
        indexes = [models.Index(fields=['invoice']), models.Index(fields=['payment'])]

    def __str__(self):
        return f"{self.invoice.invoice_no} ↔ {self.payment} · {self.amount}"


# =========================
# HELPER: Fatura Oluşturma (Proforma Dahil)
# =========================

def create_invoice_from_processes(
        processes: Iterable[Process] | QuerySet,
        *,
        currency: str = MoneyCurrency.TRY,
        exrate_to_try: Decimal = Decimal('1'),
        hs_to_try: Decimal | None = None,
        vat_rate_metal: Decimal = Decimal('0.00'),
        vat_rate_labor: Decimal = Decimal('20.00'),
        discount_rate: Decimal = Decimal('0.00'),
        as_status: str = Invoice.Status.DRAFT,  # Varsayılan TASLAK/PROFORMA
        note: str | None = None,
) -> Invoice:
    processes = list(processes)
    if not processes:
        raise ValueError("İşlem listesi boş")

    first_p = processes[0]
    store = first_p.store

    # Müşteri veya Tedarikçi ayrımı
    customer = None
    supplier = None

    # İşlem tipine göre belge sınıfı belirle
    is_purchase = first_p.transaction_type in ['PURCHASE', 'RETURN', 'STOCK_IN']

    if is_purchase:
        invoice_type = Invoice.Type.PURCHASE
        doc_class = Invoice.DocumentClass.EXPENSE_VOUCHER  # Varsayılan Gider Pusulası
        # Eğer supplier tanımlıysa bu bir Alış Faturasıdır (Incoming Invoice)
        if first_p.supplier:
            supplier = first_p.supplier
            doc_class = Invoice.DocumentClass.PAPER  # Veya E-FATURA (Gelen)
        elif first_p.customer:
            customer = first_p.customer  # Müşteriden altın bozumu
    else:
        invoice_type = Invoice.Type.SALE
        doc_class = Invoice.DocumentClass.PROFORMA  # Varsayılan Proforma
        customer = first_p.customer

    with transaction.atomic():
        # Belge sınıfına uygun numarayı al (Proforma ise PRF serisi, Gider ise GDR serisi)
        number, seq = Invoice.next_number_for(store, doc_class=doc_class)

        inv = Invoice.objects.create(
            store=store,
            customer=customer,
            supplier=supplier,
            process=None,  # Çoklu seçim olduğu için process'i boş geçiyoruz, allocations ile bağlanabilir
            invoice_no=number,
            sequence_no=seq,
            issue_date=timezone.now(),
            invoice_type=invoice_type,
            doc_class=doc_class,
            status=as_status,
            currency=currency,
            exrate_to_try=exrate_to_try or Decimal('1'),
            hs_to_try=hs_to_try,
            notes=(note or "")
        )

        for p in processes:
            prod = p.product
            # Gramlı ürün mü Adetli mi?
            # Process modelindeki piece ve gram alanlarına göre karar ver
            is_gram = False
            qty = Decimal('0')

            p_gram = Decimal(str(p.gram or 0))
            p_piece = Decimal(str(p.piece or 0))

            if p_gram > 0:
                is_gram = True
                qty = q3(p_gram)
            else:
                is_gram = False
                qty = q3(p_piece)

            unit = InvoiceItem.Unit.GRAM if is_gram else InvoiceItem.Unit.PIECE

            # Birim Fiyat
            # Process amount KDV dahil toplamdır. Bunu birim fiyata çevirmeliyiz.
            # Ancak Process modelinde unit_price var, onu kullanalım.
            p_unit_price = Decimal(str(p.unit_price or 0))

            # Kalem 1: Metal / Ürün Bedeli
            item = InvoiceItem.objects.create(
                invoice=inv,
                product=prod,
                product_name=(getattr(prod, 'name', '') or 'Ürün'),
                barcode=getattr(prod, 'barcode', '') or '',
                jewelry_type=getattr(prod, 'jewelry_type', '') or '',
                is_gram_bullion=bool(getattr(prod, 'is_gram_bullion', True)),
                quantity=qty,
                unit=unit,
                unit_price=q3(p_unit_price),
                discount_rate=discount_rate,
                vat_rate=vat_rate_metal,  # Genelde Altın %0
                notes="KDVK 17/4-g – külçe altın" if not is_purchase else "Gider Pusulası Kalemi"
            )
            item.recompute(save=True)

            # Kalem 2: İşçilik (Varsa ve Satış ise)
            labor = getattr(p, 'labor_amount', None)
            try:
                labor = Decimal(str(labor or 0))
            except Exception:
                labor = Decimal('0')

            # Process modelindeki labor_amount NET işçilik mi BRÜT mü kontrol edilmeli.
            # Genelde Kuyum Plus mantığında labor_amount özel matrahtır.
            if labor > 0 and not is_purchase:
                it2 = InvoiceItem.objects.create(
                    invoice=inv,
                    product=None,
                    product_name=f"{getattr(prod, 'name', 'Ürün')} – İşçilik Hizmeti",
                    quantity=Decimal('1.000'),
                    unit=InvoiceItem.Unit.PIECE,
                    unit_price=q3(labor),
                    discount_rate=Decimal('0.00'),
                    vat_rate=vat_rate_labor,  # Genelde %20
                    notes="KDVK 23/e – özel matrah (işçilik)"
                )
                it2.recompute(save=True)

        inv.recompute_totals(save=True)
        return inv




def create_expense_voucher_from_processes(
        processes: list[Process],
        user,
        note: str = None
) -> Invoice:
    """
    Sadece 'Gider Pusulası' (Alış) işlemleri için özelleştirilmiş fonksiyon.
    Anlık Mağaza Alış Kurunu çeker ve hesaplamalarda kullanır.
    """
    if not processes:
        raise ValueError("İşlem listesi boş, gider pusulası oluşturulamadı.")

    first_p = processes[0]
    store = first_p.store

    # 1. ANLIK KUR BİLGİSİNİ ÇEK
    # Toptan işlem sırasında kur girilmemiş olsa bile mağazanın o anki alış kurunu alıyoruz.
    try:
        current_buy_rate, current_sell_rate = compute_store_has_tl(store)
        current_buy_rate = Decimal(str(current_buy_rate or 0))
    except Exception as e:
        # KP-11/BUL-13: print() → log.warning(), str(e) → type(e).__name__
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning(f"Kur çekme hatası: {type(e).__name__}")
        current_buy_rate = Decimal('0')

    # 2. TEMEL KONTROLLER VE HESAPLAMALAR
    total_process_tl = Decimal('0')
    total_process_hs = Decimal('0')

    supplier = first_p.supplier
    customer = first_p.customer

    # Tüm işlemlerin toplamlarını alalım
    for p in processes:
        total_process_tl += Decimal(str(p.amount or 0))  # Girilen Toplam TL
        total_process_hs += Decimal(str(p.price_hs or 0))  # Girilen Toplam Has

    # 3. FATURA KURU BELİRLEME (KRİTİK ADIM)
    # Senaryo A: Kullanıcı toptan ekranında TL tutarı girdiyse, "Anlaşılan Kur" geçerlidir.
    # Senaryo B: Kullanıcı TL girmediyse (Sadece gram girdiyse), "Anlık Mağaza Kuru" geçerlidir.

    calculated_deal_rate = Decimal('0')
    if total_process_hs > 0:
        if total_process_tl > 0:
            # Kullanıcı bir tutar belirlemiş, o zaman işlem kuru budur (Deal Rate)
            calculated_deal_rate = total_process_tl / total_process_hs
        else:
            # Kullanıcı tutar girmemiş (0 TL), o zaman anlık mağaza kurunu baz alacağız
            calculated_deal_rate = current_buy_rate

    # Eğer hiçbiri yoksa (Has 0 ise) kur 0 kalır.

    # Güvenlik: Eğer hesaplanan kur 0 ise ve anlık kur varsa, anlık kuru kullan (görünüm için)
    final_rate_to_use = calculated_deal_rate if calculated_deal_rate > 0 else current_buy_rate

    with transaction.atomic():
        # 4. FATURA BAŞLIĞINI OLUŞTUR
        number, seq = Invoice.next_number_for(store, doc_class=Invoice.DocumentClass.EXPENSE_VOUCHER)

        invoice = Invoice.objects.create(
            store=store,
            invoice_no=number,
            sequence_no=seq,
            issue_date=timezone.now(),

            invoice_type=Invoice.Type.PURCHASE,
            doc_class=Invoice.DocumentClass.EXPENSE_VOUCHER,
            status=Invoice.Status.DRAFT,

            supplier=supplier,
            customer=customer,

            currency='TRY',
            # BURASI ÖNEMLİ: Hesapladığımız kuru basıyoruz.
            hs_to_try=final_rate_to_use,
            exrate_to_try=Decimal('1'),

            notes=note or f"Toptan Alış İşlemi"
        )

        # 5. KALEMLERİ OLUŞTUR
        for p in processes:
            p_gram = Decimal(str(p.gram or 0))
            p_piece = Decimal(str(p.piece or 0))

            if p_gram > 0:
                qty = p_gram
                unit = InvoiceItem.Unit.GRAM
            else:
                qty = p_piece
                unit = InvoiceItem.Unit.PIECE
                if qty <= 0: qty = Decimal('1')

            # Process üzerindeki tutarlar
            p_amount_tl = Decimal(str(p.amount or 0))
            p_amount_hs = Decimal(str(p.price_hs or 0))

            # Birim Fiyat Hesaplama
            unit_price_tl = Decimal('0')
            unit_price_hs = Decimal('0')

            if qty > 0:
                # Eğer Process'te TL tutarı varsa onu kullan, yoksa Kur * Has üzerinden git
                if p_amount_tl > 0:
                    unit_price_tl = p_amount_tl / qty
                else:
                    # TL tutarı 0 gelmişse, Anlık Kur üzerinden TL değerini oluştur
                    # Formül: (Miktar * Birim Has * Kur) / Miktar -> Basitçe: Birim Has * Kur
                    # Ancak burada p.price_hs (Toplam Has) var.
                    # Birim Has Fiyatı = Toplam Has / Miktar
                    row_unit_hs_cost = p_amount_hs / qty
                    unit_price_tl = row_unit_hs_cost * final_rate_to_use

                unit_price_hs = p_amount_hs / qty

            process_mileage = p.process_mileage or p.product.product_mileage

            item_note = ""
            if process_mileage and str(process_mileage) != '0':
                item_note = f"Milyem: {process_mileage}"

            InvoiceItem.objects.create(
                invoice=invoice,
                product=p.product,
                product_name=p.product.name or "Hurda Altın",

                quantity=qty,
                unit=unit,

                # Fiyatlar
                unit_price=unit_price_tl,
                price_hs=unit_price_hs,
                hs_to_try=final_rate_to_use,  # Her satıra da kur bilgisini işliyoruz

                vat_rate=Decimal('0'),
                withholding_rate=Decimal('0'),

                notes=item_note
            ).recompute(save=True)

        # 6. TOPLAMLARI GÜNCELLE
        invoice.recompute_totals(save=True)

        return invoice

# =========================
# MAĞAZA AYARLARI & KREDİ MODELLERİ (MEVCUT HALİ)
# =========================

class StoreEInvoiceSettings(models.Model):
    # ────────────────────────────────────────────────────────────────
    # VARSAYILAN FATURA AYAR ENUM'LARI (Açık Bankacılık Toplu Fatura)
    # ────────────────────────────────────────────────────────────────
    class LaborType(models.TextChoices):
        AMOUNT = 'AMOUNT', 'Sabit Tutar (TL — KDV Dahil)'
        PERCENT = 'PERCENT', 'Yüzde (%)'

    class Karat(models.IntegerChoices):
        K24 = 24, '24 Ayar'
        K22 = 22, '22 Ayar'
        K18 = 18, '18 Ayar'
        K14 = 14, '14 Ayar'
        K08 = 8,  '8 Ayar'

    store = models.OneToOneField(Stores, on_delete=models.CASCADE, related_name="einvoice_settings")
    enabled = models.BooleanField(default=True)

    # Entegratör Bilgileri (YENİ EKLENEBİLİR)
    integrator_name = models.CharField(max_length=50, blank=True, default="GIB")  # Örn: UYUMSOFT, SOVOS
    api_username = models.CharField(max_length=100, blank=True, null=True)
    api_password = models.CharField(max_length=100, blank=True, null=True)

    credit_balance = models.PositiveIntegerField(null=True, blank=True, default=250)
    last_topup_at = models.DateTimeField(null=True, blank=True)

    # ────────────────────────────────────────────────────────────────
    # AÇIK BANKACILIK — TOPLU FATURA VARSAYILAN AYARLARI (2026-04)
    # Banka hesabına düşen havale tutarı bu ayarlar üzerinden
    # "Metal (KDV %0)" ve "İşçilik (KDV %20, Özel Matrah)" kalemlerine
    # ayrıştırılır. İşçilik Tipi = AMOUNT ise labor_value KDV DAHİL
    # tutar olarak alınır; PERCENT ise toplam üzerinden yüzde uygulanır.
    # ────────────────────────────────────────────────────────────────
    default_invoice_product_name = models.CharField(
        max_length=150, blank=True, default='24 Ayar Has Altın',
        verbose_name='Varsayılan Ürün Adı',
        help_text='Toplu banka-fatura akışında metal kaleminin ürün adı (Örn: "24 Ayar Has Altın", "Muhtelif Takı").',
    )
    default_invoice_karat = models.PositiveSmallIntegerField(
        default=24, choices=Karat.choices,
        verbose_name='Varsayılan Ayar',
        help_text='Milyem hesabı için altın ayarı. 24=1.000, 22=0.916, 18=0.750, 14=0.585, 8=0.333.',
    )
    default_invoice_labor_type = models.CharField(
        max_length=10, choices=LaborType.choices, default=LaborType.PERCENT,
        verbose_name='İşçilik Tipi',
        help_text='AMOUNT → labor_value sabit TL (KDV dahil). PERCENT → labor_value toplam üzerinden yüzde (Örn: 5.00 = %5).',
    )
    default_invoice_labor_value = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('0.0000'),
        verbose_name='İşçilik Değeri',
        help_text='PERCENT modu için yüzde (Örn: 5.0000 = %5). AMOUNT modu için KDV DAHİL TL tutarı.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "E-Fatura Mağaza Ayarları"
        verbose_name_plural = "E-Fatura Mağaza Ayarları"

    def topup(self, n: int):
        if n and n > 0:
            self.credit_balance = (self.credit_balance or 0) + int(n)
            self.last_topup_at = timezone.now()
            self.save(update_fields=["credit_balance", "last_topup_at", "updated_at"])

    def consume(self, n: int = 1):
        if self.credit_balance is not None:
            self.credit_balance = max(0, (self.credit_balance or 0) - int(n))
            self.save(update_fields=["credit_balance", "updated_at"])

    # ────────────────────────────────────────────────────────────────
    # YARDIMCI: Ayar → Milyem katsayısı
    # ────────────────────────────────────────────────────────────────
    MILYEM_MAP = {
        24: Decimal('1.000'),
        22: Decimal('0.916'),
        18: Decimal('0.750'),
        14: Decimal('0.585'),
        8:  Decimal('0.333'),
    }

    @property
    def karat_milyem(self) -> Decimal:
        """Varsayılan ayarın milyem katsayısını Decimal olarak döner."""
        return self.MILYEM_MAP.get(int(self.default_invoice_karat or 24), Decimal('1.000'))


class EInvoiceCreditRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Beklemede"
        APPROVED = "APPROVED", "Onaylandı"
        REJECTED = "REJECTED", "Reddedildi"

    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name="ef_credit_requests")
    requester = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="ef_credit_requests")
    requested_amount = models.PositiveIntegerField()
    note = models.TextField(blank=True, default="")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(Users, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="ef_credit_decisions")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["store", "status"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        sid = getattr(self.store, 'store_id', None) or str(self.store_id)
        return f"{sid} · {self.requested_amount} · {self.status}"


class InvoiceSyncLog(models.Model):
    """
    e-Süreç ile yapılan her gönderim/sorgulama işleminin kaydı.
    Kullanıcıya kuyruğun durumunu göstermek ve hata takibi için.
    """

    class Action(models.TextChoices):
        SEND_TO_ESUREC = 'SEND_TO_ESUREC', 'e-Süreç\'e Gönder'
        SEND_TO_GIB = 'SEND_TO_GIB', 'GİB\'e Gönder'
        CHECK_STATUS = 'CHECK_STATUS', 'Durum Sorgula'
        GET_PDF = 'GET_PDF', 'PDF Al'
        GET_XML = 'GET_XML', 'XML Al'
        CANCEL = 'CANCEL', 'İptal'

    class Status(models.TextChoices):
        QUEUED = 'QUEUED', 'Kuyrukta'
        PROCESSING = 'PROCESSING', 'İşleniyor'
        SUCCESS = 'SUCCESS', 'Başarılı'
        FAILED = 'FAILED', 'Başarısız'
        SKIPPED = 'SKIPPED', 'Atlandı'
        RETRYING = 'RETRYING', 'Tekrar Deneniyor'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE, related_name='sync_logs')
    store = models.ForeignKey('stores.Stores', on_delete=models.CASCADE, null=True, blank=True)

    action = models.CharField(max_length=20, choices=Action.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default='QUEUED')

    # Celery task bilgileri
    task_id = models.CharField(max_length=255, null=True, blank=True, verbose_name='Celery Task ID')
    attempt = models.IntegerField(default=1, verbose_name='Deneme Sayısı')

    # e-Süreç referansı
    esurec_invoice_id = models.CharField(max_length=100, null=True, blank=True)

    # Hata bilgileri
    error_message = models.TextField(null=True, blank=True)
    error_detail = models.TextField(null=True, blank=True)

    # API yanıt
    response_data = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'InvoiceSyncLogs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['invoice', '-created_at']),
            models.Index(fields=['status', 'action']),
            models.Index(fields=['store', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_action_display()} - {self.get_status_display()} ({self.invoice_id})"


class InvoiceActivityLog(models.Model):
    """
    Kullanıcıya gösterilecek hafif aktivite logları.

    InvoiceSyncLog teknik detay (response_data, error_detail) için,
    bu model kullanıcı dostu mesajlar ve Trace ID takibi için kullanılır.

    Admin → trace_id veya log_number ile arama yapar → InvoiceSyncLog'a bağlanır.
    Müşteri → sadece kendi store'una ait logları görür.
    """

    class Level(models.TextChoices):
        INFO = 'INFO', 'Bilgi'
        WARN = 'WARN', 'Uyarı'
        ERROR = 'ERROR', 'Hata'

    class Event(models.TextChoices):
        SEND_ATTEMPT = 'SEND_ATTEMPT', 'Gönderim Denemesi'
        GIB_ERROR = 'GIB_ERROR', 'GİB Hatası'
        GIB_SUCCESS = 'GIB_SUCCESS', 'GİB Başarılı'
        STATUS_CHANGE = 'STATUS_CHANGE', 'Durum Değişikliği'
        DRAFT_RESET = 'DRAFT_RESET', 'Düzenlemeye Alındı'
        VALIDATION_ERROR = 'VALIDATION_ERROR', 'Doğrulama Hatası'
        CANCEL = 'CANCEL', 'İptal'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trace_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="InvoiceSyncLog ile bağlantı kurmak için ortak izleme kimliği"
    )
    log_number = models.CharField(
        max_length=20, unique=True, db_index=True,
        help_text="Kullanıcıya gösterilecek referans numarası (Örn: LOG-20260416-0042)"
    )

    invoice = models.ForeignKey(
        'Invoice', on_delete=models.CASCADE, related_name='activity_logs'
    )
    store = models.ForeignKey(
        'stores.Stores', on_delete=models.CASCADE, null=True, blank=True,
        help_text="Müşteri izolasyonu için — her kullanıcı sadece kendi mağazasını görür"
    )

    level = models.CharField(max_length=5, choices=Level.choices, default=Level.INFO)
    event = models.CharField(max_length=20, choices=Event.choices)
    user_message = models.CharField(
        max_length=500,
        help_text="Kullanıcıya gösterilecek sanitize edilmiş mesaj"
    )
    is_resolved = models.BooleanField(
        default=False,
        help_text="Admin tarafından 'çözüldü' olarak işaretlenebilir"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'InvoiceActivityLogs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['invoice', '-created_at']),
            models.Index(fields=['store', 'level', '-created_at']),
        ]

    def __str__(self):
        return f"{self.log_number} — {self.get_level_display()} — {self.user_message[:60]}"

    def save(self, *args, **kwargs):
        if not self.log_number:
            self.log_number = self._generate_log_number()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_log_number() -> str:
        """
        LOG-YYYYMMDD-NNNN formatında benzersiz log numarası üretir.
        Aynı gün içinde sıralı artar.
        """
        from django.utils import timezone as _tz
        today = _tz.now()
        prefix = f"LOG-{today.strftime('%Y%m%d')}-"

        last = InvoiceActivityLog.objects.filter(
            log_number__startswith=prefix,
        ).order_by('-log_number').values_list('log_number', flat=True).first()

        if last:
            try:
                seq = int(last.split('-')[-1]) + 1
            except (ValueError, IndexError):
                seq = 1
        else:
            seq = 1

        return f"{prefix}{seq:04d}"

