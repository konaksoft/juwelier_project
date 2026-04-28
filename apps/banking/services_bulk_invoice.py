# ============================================================================
# DOSYA: apps/banking/services_bulk_invoice.py
# KONUM: Kuyum Plus (jewelery_project)
# AÇIKLAMA: Açık Bankacılık → Toplu E-Fatura Otomasyonu
#
# Bu servis, banka hesabına düşen havale tutarlarını mağazanın
# varsayılan fatura ayarlarına göre "Metal (KDV %0)" + "İşçilik
# (KDV %20, Özel Matrah)" kalemlerine matematiksel olarak ayrıştırır,
# OZELMATRAH tipi bir Invoice oluşturur ve opsiyonel olarak e-Süreç
# üzerinden GİB'e gönderim akışını tetikler.
#
# ALGORİTMA (İŞÇİLİK = PERCENT):
#   labor_incl_vat = bank_amount × (labor_value / 100)
#   labor_excl_vat = labor_incl_vat / 1.20
#   labor_vat      = labor_incl_vat - labor_excl_vat
#   metal_amount   = bank_amount - labor_incl_vat   (KDV %0 metal)
#
# ALGORİTMA (İŞÇİLİK = AMOUNT — KDV DAHİL):
#   labor_incl_vat = labor_value
#   labor_excl_vat = labor_incl_vat / 1.20
#   labor_vat      = labor_incl_vat - labor_excl_vat
#   metal_amount   = bank_amount - labor_incl_vat
#
# MİLYEM HESABI:
#   gram = metal_amount / (has_sale_tl × milyem_katsayisi)
#   Milyem — 24: 1.000, 22: 0.916, 18: 0.750, 14: 0.585, 8: 0.333
#
# KURALLAR:
#   - Tüm ara hesaplamalar Decimal ile yapılır (float YASAK).
#   - select_for_update() ile row-level lock, çift faturalandırma
#     engellenir.
#   - Yalnızca DEBIT (gelen) hareketler faturalandırılır.
#   - Hareketin zaten invoice_id'si varsa atlanır (idempotency).
#   - Invoice.status = DRAFT ile başlar; send_to_gib=True ise Celery
#     görevi transaction.on_commit() ile tetiklenir.
# ============================================================================

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.banking.models import BankTransaction
from apps.invoices.models import (
    Invoice,
    InvoiceItem,
    StoreEInvoiceSettings,
)

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────
# SABİTLER
# ────────────────────────────────────────────────────────────────

LABOR_VAT_RATE = Decimal('20.00')   # İşçilik kalemi KDV oranı (%)
METAL_VAT_RATE = Decimal('0.00')    # Metal kalemi KDV oranı (%)
VAT_MULTIPLIER = Decimal('1.20')    # 1 + (20/100)

CENT = Decimal('0.01')
GRAM_PRECISION = Decimal('0.001')


def _q2(x: Decimal) -> Decimal:
    """2 ondalık basamak (tutar) — ROUND_HALF_UP."""
    return (x or Decimal('0')).quantize(CENT, rounding=ROUND_HALF_UP)


def _q3(x: Decimal) -> Decimal:
    """3 ondalık basamak (miktar/gram) — ROUND_HALF_UP."""
    return (x or Decimal('0')).quantize(GRAM_PRECISION, rounding=ROUND_HALF_UP)


def _to_decimal(value, default='0') -> Decimal:
    """Güvenli Decimal dönüşümü — hata durumunda default."""
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


# ────────────────────────────────────────────────────────────────
# AYRIŞTIRMA SONUCU (DTO)
# ────────────────────────────────────────────────────────────────

@dataclass
class SplitResult:
    """Tek bir banka hareketinin ayrıştırma sonucu."""
    bank_amount: Decimal        # Havale toplam tutarı (TL)
    metal_amount: Decimal       # Metal kaleminin brüt bedeli (KDV %0)
    labor_incl_vat: Decimal     # İşçilik KDV DAHİL tutar
    labor_excl_vat: Decimal     # İşçilik KDV HARİÇ tutar (matrah)
    labor_vat: Decimal          # İşçilik KDV tutarı
    has_sale_tl: Decimal        # Mağazanın anlık has satış kuru
    milyem: Decimal             # Ayarın milyem katsayısı
    gram: Decimal               # Metal kaleminin gram miktarı
    karat: int                  # 24 / 22 / 18 / 14 / 8
    product_name: str           # Metal kaleminin ürün adı

    def to_dict(self) -> dict:
        return {
            'bank_amount':    str(self.bank_amount),
            'metal_amount':   str(self.metal_amount),
            'labor_incl_vat': str(self.labor_incl_vat),
            'labor_excl_vat': str(self.labor_excl_vat),
            'labor_vat':      str(self.labor_vat),
            'has_sale_tl':    str(self.has_sale_tl),
            'milyem':         str(self.milyem),
            'gram':           str(self.gram),
            'karat':          self.karat,
            'product_name':   self.product_name,
        }


@dataclass
class BulkInvoiceResult:
    """Toplu işlemin nihai sonucu."""
    created: list = field(default_factory=list)   # [{invoice_id, invoice_no, bank_txn_id, amount}]
    skipped: list = field(default_factory=list)   # [{bank_txn_id, reason}]
    failed: list = field(default_factory=list)    # [{bank_txn_id, error}]
    gib_queued: int = 0                           # GİB'e kuyruğa alınan fatura sayısı


# ────────────────────────────────────────────────────────────────
# ANA SERVİS SINIFI
# ────────────────────────────────────────────────────────────────

class BankBulkInvoiceService:
    """
    Banka hesap hareketlerini, mağazanın varsayılan fatura ayarlarına
    göre OZELMATRAH tipi e-Arşiv/e-Fatura taslağına dönüştürür.

    Kullanım:
        svc = BankBulkInvoiceService(store=request.user.store)
        result = svc.build_bulk(txn_ids=['uuid1','uuid2'], send_to_gib=True)

    Fırlatabileceği hatalar:
        ValueError — Ayar eksikliği veya geçersiz kur durumunda.
    """

    def __init__(self, store, settings: Optional[StoreEInvoiceSettings] = None):
        if store is None:
            raise ValueError("BankBulkInvoiceService: store zorunlu.")
        self.store = store
        self.settings = settings or self._load_settings(store)
        self._has_sale_tl = None  # lazy

    # ────────────────────────────────────────────────────────────
    # AYAR YÜKLEME
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_settings(store) -> StoreEInvoiceSettings:
        """
        Mağazanın e-fatura ayarlarını çeker. Kayıt yoksa otomatik oluşturur
        (default alanlarla — canlı veride patlamaz).
        """
        settings, _created = StoreEInvoiceSettings.objects.get_or_create(store=store)
        return settings

    # ────────────────────────────────────────────────────────────
    # ANLIK KUR ÇEKİMİ
    # ────────────────────────────────────────────────────────────

    @property
    def has_sale_tl(self) -> Decimal:
        """
        Mağazanın anlık "has altın satış" kurunu döner (TL/gram).
        compute_store_has_tl() fonksiyonu tuple (buy, sale) döner;
        biz satış kurunu kullanırız (fatura kesme = satış).
        """
        if self._has_sale_tl is not None:
            return self._has_sale_tl

        try:
            # Lazy import — products.views modülü view'ları import eder
            from apps.products.views import compute_store_has_tl
            buy, sale = compute_store_has_tl(self.store)
            self._has_sale_tl = _to_decimal(sale)
        except Exception as exc:
            log.warning(
                "[BulkInvoice] compute_store_has_tl çağrılamadı — store=%s hata=%s",
                self.store.id, type(exc).__name__,
            )
            self._has_sale_tl = Decimal('0')

        return self._has_sale_tl

    # ────────────────────────────────────────────────────────────
    # MATEMATİKSEL AYRIŞTIRMA (Saf fonksiyon — side effect yok)
    # ────────────────────────────────────────────────────────────

    def split_amount(self, bank_amount) -> SplitResult:
        """
        Banka hareketinin tutarını ayarlara göre metal + işçilik kalemlerine
        ayrıştırır. Saf matematiksel fonksiyondur, DB'ye yazmaz.
        """
        amount = _to_decimal(bank_amount)
        if amount <= 0:
            raise ValueError(
                f"Geçersiz banka tutarı: {bank_amount}. Pozitif olmalı."
            )

        labor_value = _to_decimal(self.settings.default_invoice_labor_value)
        labor_type = self.settings.default_invoice_labor_type or StoreEInvoiceSettings.LaborType.PERCENT

        # ── İŞÇİLİK HESABI ────────────────────────────────────
        if labor_type == StoreEInvoiceSettings.LaborType.AMOUNT:
            # AMOUNT: labor_value sabit KDV DAHİL TL
            labor_incl_vat = labor_value
        else:
            # PERCENT: toplam üzerinden yüzde (KDV dahil işçilik)
            #   örn: 100.000 × (5/100) = 5.000 TL
            if labor_value < 0:
                raise ValueError(
                    f"Geçersiz işçilik yüzdesi: {labor_value}. Negatif olamaz."
                )
            labor_incl_vat = amount * (labor_value / Decimal('100'))

        # Aşırı işçilik koruması: işçilik tutarı toplamı aşarsa
        if labor_incl_vat > amount:
            raise ValueError(
                f"İşçilik tutarı ({_q2(labor_incl_vat)} TL) havale tutarından "
                f"({_q2(amount)} TL) büyük olamaz. İşçilik ayarlarını kontrol edin."
            )

        labor_incl_vat = _q2(labor_incl_vat)
        # KDV hariç baz = KDV dahil / 1.20
        labor_excl_vat = _q2(labor_incl_vat / VAT_MULTIPLIER) if labor_incl_vat > 0 else Decimal('0.00')
        labor_vat = _q2(labor_incl_vat - labor_excl_vat)

        # ── METAL KALEMİ ──────────────────────────────────────
        metal_amount = _q2(amount - labor_incl_vat)

        # ── MİLYEM + GRAM HESABI ──────────────────────────────
        karat = int(self.settings.default_invoice_karat or 24)
        milyem = self.settings.karat_milyem
        kur = self.has_sale_tl

        if kur <= 0 or milyem <= 0 or metal_amount <= 0:
            gram = Decimal('0.000')
        else:
            # gram = metal_amount / (kur × milyem)
            effective_rate = kur * milyem
            gram = _q3(metal_amount / effective_rate) if effective_rate > 0 else Decimal('0.000')

        return SplitResult(
            bank_amount=_q2(amount),
            metal_amount=metal_amount,
            labor_incl_vat=labor_incl_vat,
            labor_excl_vat=labor_excl_vat,
            labor_vat=labor_vat,
            has_sale_tl=_q2(kur),
            milyem=milyem,
            gram=gram,
            karat=karat,
            product_name=(self.settings.default_invoice_product_name or '').strip() or '24 Ayar Has Altın',
        )

    # ────────────────────────────────────────────────────────────
    # TEK BANKA HAREKETİNDEN FATURA OLUŞTURMA
    # ────────────────────────────────────────────────────────────

    def build_invoice(self, bank_txn: BankTransaction) -> Invoice:
        """
        Tek bir BankTransaction'dan OZELMATRAH tipi Invoice oluşturur.

        select_for_update() çağırıcı tarafta yapılmalıdır (bu metot
        zaten kilitli bir bank_txn bekler). transaction.atomic()
        bloğu da çağırıcının sorumluluğundadır.

        Returns:
            Invoice (status=DRAFT, doc_class=E_ARCHIVE, scenario=EARSIVFATURA)

        Notlar:
            - invoice_no üretilirken Invoice.next_number_for() kullanılır.
            - 2 InvoiceItem oluşturulur: metal (KDV %0) + işçilik (KDV %20).
            - bank_txn.invoice FK güncellenir (çağırıcı save eder).
        """
        split = self.split_amount(bank_txn.amount)

        # ── Invoice başlığı ──
        invoice_no, seq_no = Invoice.next_number_for(
            store=self.store,
            invoice_date=bank_txn.doc_date or timezone.now(),
        )

        invoice = Invoice.objects.create(
            store=self.store,
            customer=bank_txn.customer,     # eşleşen cari (None olabilir)
            supplier=None,
            invoice_no=invoice_no,
            sequence_no=seq_no,
            issue_date=timezone.now(),
            invoice_type=Invoice.Type.SALE,
            doc_class=Invoice.DocumentClass.E_ARCHIVE,
            scenario=Invoice.Scenario.EARSIV,
            status=Invoice.Status.DRAFT,
            currency='TRY',
            notes=self._build_invoice_note(bank_txn, split),
        )

        # ── KALEM 1: METAL (KDV %0, Özel Matrah) ──
        #   gram > 0 ise unit=GRAM, değilse AD (gram hesaplanamadığında da kayıt çalışır)
        if split.gram > 0:
            metal_unit = InvoiceItem.Unit.GRAM
            metal_qty = split.gram
            # unit_price = metal_amount / gram (gram başına fiyat)
            metal_unit_price = _q3(split.metal_amount / split.gram) if split.gram > 0 else Decimal('0.000')
        else:
            metal_unit = InvoiceItem.Unit.PIECE
            metal_qty = Decimal('1.000')
            metal_unit_price = _q3(split.metal_amount)

        metal_item = InvoiceItem.objects.create(
            invoice=invoice,
            product=None,
            product_name=split.product_name,
            is_gram_bullion=True,
            quantity=metal_qty,
            unit=metal_unit,
            unit_price=metal_unit_price,
            discount_rate=Decimal('0.00'),
            vat_rate=METAL_VAT_RATE,
            notes=f"KDVK 23/e — Özel Matrah ({split.karat} ayar, milyem {split.milyem})",
            exemption_reason='KDVK Madde 23/e — Kıymetli Maden Teslimleri (Özel Matrah)',
        )
        metal_item.recompute(save=True)

        # ── KALEM 2: İŞÇİLİK (KDV %20, Özel Matrah) ──
        if split.labor_incl_vat > 0:
            labor_item = InvoiceItem.objects.create(
                invoice=invoice,
                product=None,
                product_name=f"{split.product_name} — İşçilik Hizmeti",
                is_gram_bullion=False,
                quantity=Decimal('1.000'),
                unit=InvoiceItem.Unit.PIECE,
                unit_price=split.labor_excl_vat,   # KDV hariç matrah
                discount_rate=Decimal('0.00'),
                vat_rate=LABOR_VAT_RATE,
                notes="KDVK 23/e — Özel Matrah (İşçilik)",
                exemption_reason='KDVK Madde 23/e — Özel Matrah (İşçilik)',
            )
            labor_item.recompute(save=True)

        # ── Toplamları hesapla (Invoice.recompute_totals) ──
        invoice.recompute_totals(save=True)

        return invoice

    # ────────────────────────────────────────────────────────────
    # TOPLU FATURA OLUŞTURMA (Concurrency-safe)
    # ────────────────────────────────────────────────────────────

    def build_bulk(self, txn_ids: list, send_to_gib: bool = False) -> BulkInvoiceResult:
        """
        Birden fazla BankTransaction için toplu fatura oluşturur.

        Her işlem için:
          1. select_for_update() ile row-level lock
          2. Yön kontrolü (DEBIT — gelen para)
          3. Idempotency (invoice_id IS NULL)
          4. build_invoice() çağrısı
          5. bank_txn.invoice + payment_status güncelleme
          6. send_to_gib=True ise transaction.on_commit() ile Celery tetikle

        Hata yönetimi: tek bir transaction'ın hatası diğerlerini etkilemez.
        """
        result = BulkInvoiceResult()

        if not txn_ids:
            return result

        for txn_id in txn_ids:
            try:
                self._process_one(txn_id, send_to_gib, result)
            except Exception as exc:
                log.exception(
                    "[BulkInvoice] Beklenmeyen hata — txn=%s hata=%s",
                    txn_id, type(exc).__name__,
                )
                result.failed.append({
                    'bank_txn_id': str(txn_id),
                    'error': f'{type(exc).__name__}: {str(exc)[:200]}',
                })

        return result

    def _process_one(self, txn_id: str, send_to_gib: bool, result: BulkInvoiceResult):
        """Tek bir hareket için atomik işlem. Hata dışarıda yakalanır."""
        with transaction.atomic():
            # select_for_update — aynı anda başka bir isteğin kilidini bekler
            bank_txn = (
                BankTransaction.objects
                .select_for_update()
                .select_related('customer')
                .filter(id=txn_id, store=self.store)
                .first()
            )

            if bank_txn is None:
                result.skipped.append({
                    'bank_txn_id': str(txn_id),
                    'reason': 'Banka hareketi bulunamadı.',
                })
                return

            # ── Yön kontrolü ──
            if bank_txn.plus_minus != BankTransaction.PlusMinus.DEBIT:
                result.skipped.append({
                    'bank_txn_id': str(txn_id),
                    'reason': 'Giden para (CREDIT) — faturalandırılamaz.',
                })
                return

            # ── Idempotency ──
            if bank_txn.invoice_id:
                result.skipped.append({
                    'bank_txn_id': str(txn_id),
                    'reason': f'Zaten faturalandırılmış (Fatura: {bank_txn.invoice_id}).',
                })
                return

            # ── Tutar kontrolü ──
            if not bank_txn.amount or _to_decimal(bank_txn.amount) <= 0:
                result.skipped.append({
                    'bank_txn_id': str(txn_id),
                    'reason': 'Tutar 0 veya negatif — faturalandırılamaz.',
                })
                return

            # ── Fatura oluştur ──
            invoice = self.build_invoice(bank_txn)

            # ── Banka hareketini fatura ile eşle ──
            bank_txn.invoice = invoice
            bank_txn.payment_status = BankTransaction.PaymentStatus.PAID
            bank_txn.save(update_fields=['invoice', 'payment_status', 'updated_on'])

            result.created.append({
                'bank_txn_id': str(txn_id),
                'invoice_id': str(invoice.id),
                'invoice_no': invoice.invoice_no,
                'amount': str(bank_txn.amount),
                'customer_name': self._customer_label(bank_txn),
            })

            # ── GİB'e gönderim (opsiyonel) ──
            # transaction.on_commit: DB commit sonrası Celery'e gönder.
            # Böylece worker Invoice.DoesNotExist almaz.
            if send_to_gib:
                invoice_id_str = str(invoice.id)
                store_id_str = str(self.store.id)

                def _dispatch_gib():
                    try:
                        from apps.invoices.tasks import send_invoice_to_gib_task
                        send_invoice_to_gib_task.delay(invoice_id_str, store_id_str)
                    except Exception as dispatch_exc:
                        log.error(
                            "[BulkInvoice] Celery dispatch hatası — invoice=%s hata=%s",
                            invoice_id_str, type(dispatch_exc).__name__,
                        )

                transaction.on_commit(_dispatch_gib)
                result.gib_queued += 1

    # ────────────────────────────────────────────────────────────
    # YARDIMCILAR
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_invoice_note(bank_txn: BankTransaction, split: SplitResult) -> str:
        """Fatura notunda havale referansı + ayrıştırma özeti."""
        parts = []
        if bank_txn.doc_no:
            parts.append(f"Havale No: {bank_txn.doc_no}")
        if bank_txn.bank_name:
            parts.append(f"Banka: {bank_txn.bank_name}")
        if bank_txn.other_name:
            parts.append(f"Gönderen: {bank_txn.other_name}")
        parts.append(
            f"Ayrıştırma: Metal {_q2(split.metal_amount)} TL + "
            f"İşçilik {_q2(split.labor_incl_vat)} TL (KDV dahil)"
        )
        return ' | '.join(parts)

    @staticmethod
    def _customer_label(bank_txn: BankTransaction) -> str:
        """Cari adını (varsa) veya karşı taraf bilgisini döner."""
        if bank_txn.customer_id:
            c = bank_txn.customer
            name = f"{c.first_name or ''} {c.last_name or ''}".strip()
            if name:
                return name
        return bank_txn.other_name or '-'
