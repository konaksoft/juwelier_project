"""CollectionService — Tahsilat / Kapatma akışı (FAZ 14 — Kasa-Cari Entegrasyonu).

Cari/Emanet Refactor — Senaryo örneği:
  Müşteri 1.58 HS borçlu (dünkü kurla 10.000 TL).
  Bugün kur değişti, 1.58 HS = 10.099 TL oldu.
  Müşteri 10.000 TL ödemek istiyor; kuyumcu kabul ediyor.

  Atomik blok (FAZ 14 sonrası):
    1) Payment kaydı  (10.000 TL kasaya, bank_account zorunlu)
    2) CashboxLedger.INCOME (10.000 TL, kasa=bank_account, related_payment=1)
    3) CustomerLedger.COLLECTION_TL  (1.5645 HS, 10.000 TL, related_payment=1)
    4) CustomerLedger.FX_GAIN        (0.0155 HS, 99 TL, parent=Fiş 3)
                                     description="Müşteri insiyatifi kur farkı"
                                     → IncomeExpenseLedger.FX_LOSS_EXPENSE
                                       onay sonrası ApprovalService tarafından yazılır.

  Sonuç: müşteri bakiyesi 0; kasa +10.000 TL; muhasebe 99 TL kur zararı.

FAZ 30 — Hızlı Onay Mimarisi (Fazla Tahsilat / Kasa Fazlası):
  Müşterinin borcu 67.286 TL, kuyumcu 67.300 TL tahsil ediyor.
  `allow_overpayment=True` ile servis çağrılır:
    1) Payment.amount = 67.300 TL (tam giriş)
    2) CashboxLedger.INCOME = 67.300 TL (tam giriş)
    3) CustomerLedger.COLLECTION_TL = 67.286 TL (sadece borç kadar — borç kapanır)
    4) CustomerLedger.OVERPAYMENT = 14 TL (debt-neutral; parent=COLLECTION)
       → onaylandığında IncomeExpenseLedger.OTHER_INCOME satırı yazılır.
  Toplam doğrulama: collection_tl + overpayment_tl == payment.amount.

Kritik kurallar:
  - bank_account ZORUNLU. None geçilirse InvalidLedgerStateError.
  - bank_account.account_type ile payment_currency arasında uyum kontrolü.
  - bank_account.currency ile payment_currency arasında uyum kontrolü.
  - Tahsilat HS karşılığı + Adjustment HS = Kapatılan borç HS.
    Bu eşitlik sağlanmazsa BalanceMismatchError fırlatır.
  - allow_overpayment=False (varsayılan) iken borçtan büyük tahsilat
    InvalidLedgerStateError ile reddedilir (FAZ 14 davranışı korunur).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction

from apps.customers.models import Customers, CustomerLedger
from apps.banking.exchange_rate_service import (
    get_current_has_rate, get_current_fx_rate,
)
from apps.customers.services.ledger import LedgerService
from apps.customers.services.approval import (
    evaluate_self_approval_capability,
)
from apps.customers.services.exceptions import (
    BalanceMismatchError,
    InvalidLedgerStateError,
)


_HS_QUANT = Decimal('0.001')
_TL_QUANT = Decimal('0.01')


def _q_hs(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_HS_QUANT, rounding=ROUND_HALF_UP)


def _q_tl(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_TL_QUANT, rounding=ROUND_HALF_UP)


# ════════════════════════════════════════════════════════════════════════════
# TİP UYUM HARİTALARI (FAZ 14 — Adım 6)
# ════════════════════════════════════════════════════════════════════════════
#
# bank_account.account_type → Payment.payment_type otomatik türemesi.
# Kullanıcı arayüzünde "Kasa Seçimi" tek başına yeterlidir; payment_type
# bu seçimden çıkarsanır. Bu, çapraz tip hatasını (örn. CASH ödeme
# POS hesabına gönderilmesi) yapısal olarak engeller.
#
ACCOUNT_TYPE_TO_PAYMENT_TYPE = {
    'CASH': 'CASH',
    'POS':  'CREDIT_CARD',
    'BANK': 'TRANSFER',
}

# Para birimi tabanlı kasa uyum kuralı:
# - TRY tahsilatı → currency='TRY' kasaya
# - USD/EUR/GBP tahsilatı → ilgili döviz kasasına
# - HS tahsilatı → TRY kasaya (HS altın için ayrı altın kasası FAZ kapsamı dışı)
def _expected_cashbox_currency(payment_currency: str) -> str:
    if payment_currency in ('USD', 'EUR', 'GBP'):
        return payment_currency
    return 'TRY'


# ════════════════════════════════════════════════════════════════════════════
# SONUÇ DTO
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CollectionResult:
    payment = None  # process.Payment instance
    cashbox_entry = None  # banking.CashboxLedger instance
    collection_entry: CustomerLedger = None
    adjustment_entry: Optional[CustomerLedger] = None
    overpayment_entry: Optional[CustomerLedger] = None  # FAZ 30
    closed_amount_hs: Decimal = Decimal('0')
    closed_amount_eur: Decimal = Decimal('0')
    new_balance_hs: Decimal = Decimal('0')
    overpayment_tl: Decimal = Decimal('0')                # FAZ 30
    overpayment_hs: Decimal = Decimal('0')                # FAZ 30
    self_approval_info: dict = field(default_factory=dict)  # FAZ 30

    def to_dict(self):
        # Hangi satır onay bekliyorsa frontend'e ona göre sinyal gönderiyoruz.
        # Birden fazla pending satır olamaz (mimari kısıt: ya adjustment ya
        # da overpayment, aynı tahsilatta ikisi birden olmaz).
        pending_entry = None
        if self.adjustment_entry and not self.adjustment_entry.is_approved:
            pending_entry = self.adjustment_entry
        elif self.overpayment_entry and not self.overpayment_entry.is_approved:
            pending_entry = self.overpayment_entry

        pending_id = str(pending_entry.id) if pending_entry else None
        pending_type = pending_entry.transaction_type if pending_entry else None

        return {
            'payment_id': str(self.payment.id) if self.payment else None,
            'cashbox_entry_id': str(self.cashbox_entry.id) if self.cashbox_entry else None,
            'collection_entry_id': str(self.collection_entry.id) if self.collection_entry else None,
            'adjustment_entry_id': str(self.adjustment_entry.id) if self.adjustment_entry else None,
            'overpayment_entry_id': str(self.overpayment_entry.id) if self.overpayment_entry else None,
            'closed_amount_hs': str(self.closed_amount_hs),
            'closed_amount_eur': str(self.closed_amount_eur),
            'new_balance_hs': str(self.new_balance_hs),
            # FAZ 30 — Hızlı Onay Modalı için yeni alanlar
            'overpayment_tl': str(self.overpayment_tl),
            'overpayment_hs': str(self.overpayment_hs),
            'pending_approval': bool(pending_entry),
            'pending_entry_id': pending_id,
            'pending_entry_type': pending_type,
            # `self_approval_info` evaluate_self_approval_capability çıktısı:
            #   pct_of_debt, within_self_approve_band, actor_can_self_approve,
            #   required_level, required_permission
            'self_approval': self.self_approval_info or {},
        }


# ════════════════════════════════════════════════════════════════════════════
# COLLECTION SERVICE
# ════════════════════════════════════════════════════════════════════════════

class CollectionService:

    # ────────────────────────────────────────────────────────────────────
    # YARDIMCI: Kasa-Ödeme Tip Uyum Kontrolü (FAZ 14 — Adım 6)
    # ────────────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_cashbox_compatibility(bank_account, payment_currency: str) -> str:
        """bank_account ile payment_currency uyumunu kontrol eder.

        Returns:
            Türemiş Payment.payment_type (CASH/CREDIT_CARD/TRANSFER).

        Raises:
            InvalidLedgerStateError: bank_account None ise veya tip/para
                                     birimi uyumsuzluğu varsa.
        """
        if bank_account is None:
            raise InvalidLedgerStateError(
                'Tahsilat için kasa seçimi zorunludur (bank_account boş geçilemez).',
            )

        # 1) account_type → payment_type türemesi
        account_type = getattr(bank_account, 'account_type', None)
        payment_type = ACCOUNT_TYPE_TO_PAYMENT_TYPE.get(account_type)
        if not payment_type:
            raise InvalidLedgerStateError(
                f'Geçersiz kasa tipi: {account_type!r}. '
                f'Beklenen: CASH / POS / BANK.',
            )

        # 2) Para birimi uyumu
        expected_currency = _expected_cashbox_currency(payment_currency)
        cashbox_currency = (getattr(bank_account, 'currency', None) or 'TRY').upper()
        if cashbox_currency != expected_currency:
            raise InvalidLedgerStateError(
                f'Kasa para birimi uyuşmuyor: '
                f'tahsilat {payment_currency} için {expected_currency} kasası '
                f'gerekli; seçilen kasa {cashbox_currency}.',
            )

        return payment_type

    # ────────────────────────────────────────────────────────────────────
    # YARDIMCI: CashboxLedger Yazımı (FAZ 14 — Adım 3)
    # ────────────────────────────────────────────────────────────────────
    @staticmethod
    def _write_cashbox_inflow(
        *,
        bank_account,
        store,
        amount,
        currency: str,
        amount_eur_equivalent: Decimal,
        exchange_rate: Optional[Decimal],
        related_payment,
        process_no: str,
        audit: dict,
        description: str,
    ):
        """CashboxLedger.INCOME satırı yazar (atomic blok içinde çağrılmalı).

        balance_snapshot: O an için kasa kümülatif bakiyesini hesaplayıp
                          yeni satıra yazar (audit + sorgu hızı).
        """
        from apps.banking.models import CashboxLedger

        # Yazım anındaki bakiye snapshot'u (TL bazında)
        prior_balance = bank_account.get_balance(currency=currency)
        new_balance = (prior_balance + amount_eur_equivalent).quantize(_TL_QUANT)

        return CashboxLedger.objects.create(
            cashbox=bank_account,
            store=store,
            movement_type=CashboxLedger.MovementType.INCOME,
            amount=Decimal(amount).quantize(_TL_QUANT),
            currency=currency,
            amount_eur_equivalent=amount_eur_equivalent,
            exchange_rate=exchange_rate,
            balance_snapshot=new_balance,
            related_payment=related_payment,
            process_no=process_no,
            description=description,
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ────────────────────────────────────────────────────────────────────
    # ANA AKIŞ: Tahsilat + Kapatma
    # ────────────────────────────────────────────────────────────────────
    @staticmethod
    @transaction.atomic
    def collect_and_close(
        *,
        customer,
        store,
        bank_account,
        payment_amount: Decimal,
        payment_currency: str = 'TRY',
        target_close_hs: Optional[Decimal] = None,
        adjustment_type: Optional[str] = None,
        adjustment_reason: str = '',
        audit: dict,
        process_no: Optional[str] = None,
        allow_overpayment: bool = False,
    ) -> CollectionResult:
        """Tahsilat + Kapatma atomik akışı (FAZ 14 + FAZ 30).

        Atomik blok içinde 4-5 satır yazılır:
          1) Payment (TL tutar, bank_account zorunlu)
          2) CashboxLedger.INCOME (kasa hareketi)
          3) CustomerLedger.COLLECTION_TL/FX/HS (cari tahsilat)
          4) CustomerLedger.DISCOUNT/FX_GAIN/WRITEOFF (varsa, fark fişi)
          5) FAZ 30: CustomerLedger.OVERPAYMENT (varsa, kasa fazlası)

        Args:
            customer: Customers
            store: Stores
            bank_account: banking.BankAccount (ZORUNLU — None geçilemez)
            payment_amount: Müşterinin verdiği tutar (currency cinsinden)
            payment_currency: 'TRY' | 'USD' | 'EUR' | 'GBP' | 'HS'
            target_close_hs: Kapatılmak istenen toplam Has (None ise
                            tahsilatın HS karşılığı kapatılır — ek fiş yok)
            adjustment_type: Fark fiş tipi (FX_GAIN | DISCOUNT | WRITEOFF)
                            None → fark fişi yazılmaz
            adjustment_reason: Fark fişi açıklaması (zorunlu adjustment'da)
            audit: extract_audit_context(request) çıktısı
            process_no: İşlem no (otomatik üretilir)
            allow_overpayment: True ise borçtan fazla tahsilat kabul edilir;
                               fark `OVERPAYMENT` satırı olarak yazılır ve
                               IncomeExpenseLedger.OTHER_INCOME'a yansır.

        Returns:
            CollectionResult
        """
        from django.utils import timezone as dj_tz

        # ── 0) Kasa-Ödeme Uyum Kontrolü (FAZ 14 — Adım 6) ────────
        payment_type = CollectionService._validate_cashbox_compatibility(
            bank_account=bank_account,
            payment_currency=payment_currency,
        )

        # ── 0.5) MÜŞTERİ ROW LOCK (FAZ 16 — P1-01 düzeltmesi) ────
        # Eşzamanlı tahsilat isteklerinin aynı müşterinin bakiyesini
        # iki kez okuyup iki kez tahsilat yazmasını engellemek için
        # Customers satırını SELECT FOR UPDATE ile kilitle. Atomic
        # blok bitiminde (commit/rollback) PostgreSQL kilidi otomatik
        # serbest bırakır. Bakiyeyi azaltıcı/artırıcı tüm akışlar
        # bu noktadan geçtikleri sürece serileşmeye zorlanırlar.
        Customers.objects.select_for_update().filter(pk=customer.pk).first()

        # ── 1) Sistem kurunu çek ──────────────────────────────────
        # FAZ 33.3 — Açık borcun efektif kuru (stored amount_eur /
        # amount_hs) varsa onu kullan. Bu kur, borçların yazıldığı
        # andaki ortalama oranı temsil eder; tahsilat anında
        # kullanıcının girdiği TL'yi HS'ye çevirirken stored ledger
        # ile bire bir kapama sağlar (sahte overpayment yok).
        # Açık borç yoksa anlık piyasa kuruna düşülür (yeni müşteri,
        # avans tahsilatı vb.).
        effective_rate = customer.effective_rate_tl
        if effective_rate is not None and effective_rate > 0:
            has_rate = effective_rate
        else:
            has_rate = get_current_has_rate(store)
        if has_rate is None or has_rate <= 0:
            raise InvalidLedgerStateError(
                'Has altın kuru servisi yanıt vermedi. Tahsilat alınamaz.',
            )

        # ── 2) Tahsilatın HS karşılığını hesapla ──────────────────
        payment_amount = Decimal(payment_amount)
        if payment_amount <= 0:
            raise InvalidLedgerStateError(
                'Tahsilat tutarı pozitif olmalıdır.',
            )

        if payment_currency == 'TRY':
            collection_tl = _q_tl(payment_amount)
            collection_hs = _q_hs(collection_tl / has_rate)
            collection_type = CustomerLedger.COLLECTION_TL
            fx_to_tl = Decimal('1.000000')
            amount_fx = Decimal('0.00')
        elif payment_currency == 'HS':
            collection_hs = _q_hs(payment_amount)
            collection_tl = _q_tl(collection_hs * has_rate)
            collection_type = CustomerLedger.COLLECTION_HS
            fx_to_tl = Decimal('1.000000')
            amount_fx = Decimal('0.00')
        elif payment_currency in ('USD', 'EUR', 'GBP'):
            fx_to_tl = get_current_fx_rate(payment_currency, store)
            if fx_to_tl is None or fx_to_tl <= 0:
                raise InvalidLedgerStateError(
                    f'{payment_currency} kuru servisi yanıt vermedi.',
                )
            amount_fx = _q_tl(payment_amount)
            collection_tl = _q_tl(amount_fx * fx_to_tl)
            collection_hs = _q_hs(collection_tl / has_rate)
            collection_type = CustomerLedger.COLLECTION_FX
        else:
            raise InvalidLedgerStateError(
                f'Desteklenmeyen para birimi: {payment_currency}',
            )

        # Tahsilatın CASHBOX'a giren ham TL/FX tutarlarını sakla;
        # overpayment durumunda Payment ve CashboxLedger bunları kullanır.
        cashbox_payment_amount_currency = payment_amount
        cashbox_payment_amount_eur = collection_tl
        cashbox_amount_fx = amount_fx

        # ── 3) Açık borç ve hedef kapama ──────────────────────────
        open_balance_hs = LedgerService.get_open_balance_hs(customer)
        if open_balance_hs <= 0 and target_close_hs and target_close_hs > 0:
            raise InvalidLedgerStateError(
                'Müşterinin açık borcu yok; tahsilat kapatma yapılamaz.',
            )

        if target_close_hs is None:
            target_close_hs = collection_hs
        target_close_hs = _q_hs(target_close_hs)

        # ── 3.5) FAZ 30 — Fazla Tahsilat Tespiti ─────────────────
        # İki tetikleyici:
        #   (a) Kullanıcı kapatma hedefi olarak açık borcun tamamını
        #       seçti ve tahsilat HS, açık borçtan büyük → kasa fazlası.
        #   (b) target_close_hs > open_balance_hs (kullanıcı bilinçli
        #       fazla yazdı) — `allow_overpayment=False` iken yine
        #       reddediyoruz; True ise hedef otomatik open_balance'a
        #       sıkıştırılır ve aşan kısım OVERPAYMENT olur.
        overpayment_hs = Decimal('0.000')
        overpayment_tl = Decimal('0.00')

        if allow_overpayment and open_balance_hs > 0:
            # target ya da collection açık borcu aşıyorsa:
            #   target'ı open_balance'a kıs, aşan tahsilatı overpayment olarak ayır.
            if collection_hs > open_balance_hs:
                overpayment_hs = _q_hs(collection_hs - open_balance_hs)
                # Tahsilat satırı tam borç kadar yazılır:
                effective_collection_hs = open_balance_hs
                effective_collection_tl = _q_tl(open_balance_hs * has_rate)
                # OVERPAYMENT TL: payment'tan kalan
                overpayment_tl = _q_tl(collection_tl - effective_collection_tl)
                # Adjustment hedefi de borca eşitlenir
                target_close_hs = open_balance_hs
                # Aşağıdaki bölümler için collection_* değerlerini güncelle
                collection_hs = effective_collection_hs
                collection_tl = effective_collection_tl
            elif target_close_hs > open_balance_hs:
                # collection_hs ≤ open_balance ama target > open_balance
                # Bu çelişkili — target'ı borca sıkıştır (tutarlılık için).
                target_close_hs = open_balance_hs
        else:
            # Eski davranış (FAZ 14): kapama miktarı açık borçtan büyük olamaz.
            if target_close_hs > open_balance_hs and open_balance_hs > 0:
                raise InvalidLedgerStateError(
                    f'Kapama miktarı açık borçtan büyük: {target_close_hs} HS > '
                    f'{open_balance_hs} HS. Fazla tahsilat için '
                    f'"Fazla Ödemeyi Kabul Et" seçeneğini işaretleyin.',
                )

        # ── 4) Fark hesabı (adjustment) ───────────────────────────
        adjustment_hs = _q_hs(target_close_hs - collection_hs)
        adjustment_tl = _q_tl(adjustment_hs * has_rate)

        if adjustment_hs != Decimal('0.000'):
            if adjustment_type is None:
                # FAZ 30: Negatif adjustment durumu artık `allow_overpayment`
                # ile yukarıda yakalanıp OVERPAYMENT'a yönlendirildi. Buraya
                # düşen negatif fark, allow_overpayment=False iken kullanıcı
                # hedefi yanlış girmiş demektir.
                if adjustment_hs < 0:
                    raise InvalidLedgerStateError(
                        'Negatif fark fişlenemez. Fazla tahsilat için '
                        '`allow_overpayment=True` ile çağırın.',
                    )
                raise InvalidLedgerStateError(
                    f'Tahsilat ile borç arasında {adjustment_hs} HS fark var. '
                    f'Lütfen fark tipini seçin (FX_GAIN/DISCOUNT/WRITEOFF).',
                )
            # FAZ 22.10 — UAT geri bildirimi: "Fark Nedeni" alanı kaldırıldı.
            if not adjustment_reason:
                adjustment_reason = {
                    CustomerLedger.FX_GAIN: 'Kur Farkı',
                    CustomerLedger.DISCOUNT: 'İskonto',
                    CustomerLedger.WRITEOFF: 'Şüpheli Alacak Silme',
                }.get(adjustment_type, 'Fark fişi')
            if adjustment_type not in (
                CustomerLedger.FX_GAIN,
                CustomerLedger.DISCOUNT,
                CustomerLedger.WRITEOFF,
            ):
                raise InvalidLedgerStateError(
                    f'Geçersiz fark tipi: {adjustment_type}',
                )
            if adjustment_hs < 0:
                raise InvalidLedgerStateError(
                    'Negatif fark fişlenemez. Fazla tahsilat için '
                    '`allow_overpayment=True` ile çağırın.',
                )

        # ── 5) İşlem no ───────────────────────────────────────────
        if not process_no:
            process_no = f'TAH-{dj_tz.now().strftime("%Y%m%d%H%M%S")}'

        # ── 6) Payment kaydı ─────────────────────────────────────
        # Payment.amount = kasaya GİREN tam tutarın TL eşdeğeri (overpayment
        # dahil). Müşteri 67.300 verdi → Payment 67.300 TL'dir.
        from apps.process.models import Payment

        is_fx = payment_currency in ('USD', 'EUR', 'GBP')
        is_hs = payment_currency == 'HS'

        if payment_type == 'CASH':
            recon_status = Payment.ReconciliationStatus.NOT_REQUIRED
        else:
            recon_status = Payment.ReconciliationStatus.PENDING

        payment_kwargs = dict(
            process_no=process_no,
            payment_type=payment_type,
            amount=cashbox_payment_amount_eur,  # her zaman TL — tam giriş
            date=dj_tz.now(),
            is_output=False,  # tahsilat = kasaya giriş
            reference=process_no,
            bank_account=bank_account,
            reconciliation_status=recon_status,
            is_cancelled=False,
            is_approved=True,
            performed_by=audit.get('actor'),
            notes=f'Cari tahsilat — {customer}',
        )
        if is_fx:
            payment_kwargs['currency_amount'] = cashbox_amount_fx
            payment_kwargs['exchange_rate'] = fx_to_tl
        elif is_hs:
            payment_kwargs['currency_amount'] = (
                _q_hs(cashbox_payment_amount_currency)
            )
            payment_kwargs['exchange_rate'] = has_rate

        payment = Payment.objects.create(**payment_kwargs)

        # ── 7) CashboxLedger.INCOME (FAZ 14 — Adım 3) ────────────
        # Kasa para birimine göre yazım: döviz kasalarında orijinal döviz
        # tutarı, TRY kasalarında TL tutarı yazılır. Overpayment dahil
        # tam tutar yazılır.
        if is_fx:
            cb_amount = cashbox_amount_fx
            cb_currency = payment_currency
            cb_rate = fx_to_tl
        elif is_hs:
            cb_amount = cashbox_payment_amount_eur
            cb_currency = 'TRY'
            cb_rate = None
        else:
            cb_amount = cashbox_payment_amount_eur
            cb_currency = 'TRY'
            cb_rate = None

        cashbox_entry = CollectionService._write_cashbox_inflow(
            bank_account=bank_account,
            store=store,
            amount=cb_amount,
            currency=cb_currency,
            amount_eur_equivalent=cashbox_payment_amount_eur,
            exchange_rate=cb_rate,
            related_payment=payment,
            process_no=process_no,
            audit=audit,
            description=f'Cari tahsilat ({payment_currency}) — {customer}',
        )

        # ── 8) Tahsilat fişi (CustomerLedger) ─────────────────────
        # FAZ 30: collection_hs/collection_tl, overpayment çıkarıldıktan
        # sonraki "borç kadar" tutarı ifade eder. Müşteri bakiyesi yalnızca
        # bu kadar düşürülür; aşan tutar OVERPAYMENT olarak yazılır.
        collection_entry = LedgerService.write_collection(
            customer=customer,
            store=store,
            transaction_type=collection_type,
            amount_hs=collection_hs,
            amount_eur=collection_tl,
            exchange_rate_eur=has_rate,
            currency=payment_currency,
            amount_fx=amount_fx,
            fx_to_eur_rate=fx_to_tl,
            process_no=process_no,
            related_payment=payment,
            audit=audit,
            description=f'{collection_type} — {payment_currency} {payment_amount}',
        )

        # ── 9) Fark fişi (varsa) ──────────────────────────────────
        adjustment_entry = None
        if adjustment_hs > 0 and adjustment_type:
            adjustment_entry = LedgerService.write_adjustment(
                customer=customer,
                store=store,
                transaction_type=adjustment_type,
                amount_hs=adjustment_hs,
                amount_eur=adjustment_tl,
                exchange_rate_eur=has_rate,
                process_no=process_no,
                parent=collection_entry,
                audit=audit,
                description=adjustment_reason,
                open_balance_eur=_q_tl(open_balance_hs * has_rate),
            )

            if adjustment_entry.is_approved:
                from apps.customers.services.approval import (
                    write_income_expense_for_ledger_entry,
                )
                write_income_expense_for_ledger_entry(
                    entry=adjustment_entry,
                    audit=audit,
                )

        # ── 9.5) FAZ 30 — Fazla Tahsilat (OVERPAYMENT) Fişi ──────
        overpayment_entry = None
        if overpayment_tl > 0:
            closed_debt_eur = _q_tl(target_close_hs * has_rate)
            overpayment_entry = LedgerService.write_overpayment(
                customer=customer,
                store=store,
                amount_hs=overpayment_hs,
                amount_eur=overpayment_tl,
                exchange_rate_eur=has_rate,
                process_no=process_no,
                audit=audit,
                parent=collection_entry,
                related_payment=payment,
                description=f'Kasa Fazlası — {customer}',
                closed_debt_eur=closed_debt_eur,
            )

            if overpayment_entry.is_approved:
                from apps.customers.services.approval import (
                    write_income_expense_for_ledger_entry,
                )
                write_income_expense_for_ledger_entry(
                    entry=overpayment_entry,
                    audit=audit,
                )

        # ── 10) Tutar dengesi doğrulaması ─────────────────────────
        effective_adj = (
            adjustment_hs if adjustment_entry and adjustment_entry.is_approved
            else Decimal('0.000')
        )
        total_closed = _q_hs(collection_hs + effective_adj)
        if adjustment_entry and not adjustment_entry.is_approved:
            # Onay beklemede — kapama eksik tamamlanır
            total_closed = collection_hs

        if total_closed > target_close_hs + _HS_QUANT:
            raise BalanceMismatchError(
                expected=target_close_hs,
                actual=total_closed,
                unit='HS',
            )

        # ── 11) FAZ 30 — Hızlı Onay Modalı kararı ─────────────────
        # Bekleyen entry varsa (adjustment veya overpayment), backend
        # frontend'e "kullanıcı kendi kendine onaylayabilir mi?" cevabını
        # üretir. Frontend tek tıkla onay modalını ona göre açar.
        self_approval_info = {}
        pending_entry = None
        if adjustment_entry and not adjustment_entry.is_approved:
            pending_entry = adjustment_entry
        elif overpayment_entry and not overpayment_entry.is_approved:
            pending_entry = overpayment_entry

        if pending_entry is not None:
            closed_debt_eur_for_eval = _q_tl(target_close_hs * has_rate)
            self_approval_info = evaluate_self_approval_capability(
                actor=audit.get('actor'),
                transaction_type=pending_entry.transaction_type,
                amount_eur=pending_entry.amount_eur,
                closed_debt_eur=closed_debt_eur_for_eval,
            )

        # ── 12) Sonuç ─────────────────────────────────────────────
        result = CollectionResult()
        result.payment = payment
        result.cashbox_entry = cashbox_entry
        result.collection_entry = collection_entry
        result.adjustment_entry = adjustment_entry
        result.overpayment_entry = overpayment_entry
        result.closed_amount_hs = total_closed
        result.closed_amount_eur = _q_tl(total_closed * has_rate)
        result.new_balance_hs = LedgerService.get_open_balance_hs(customer)
        result.overpayment_tl = overpayment_tl
        result.overpayment_hs = overpayment_hs
        result.self_approval_info = self_approval_info
        return result

    # ─────────────────────────────────────────────────────────────
    # FAZLA ÖDEME (CREDIT) AKIŞI
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    @transaction.atomic
    def collect_overpayment(
        *,
        customer, store, bank_account,
        payment_amount: Decimal,
        payment_currency: str = 'TRY',
        audit: dict,
        process_no: Optional[str] = None,
    ):
        """Müşteri borçtan fazla ödeme yapıyor → CREDIT olarak yazılır.

        FAZ 30 sonrası bu metod kullanım dışıdır; `collect_and_close`
        içine `allow_overpayment=True` parametresi entegre edildi. Geriye
        dönük uyumluluk için iskelet korunuyor.
        """
        raise InvalidLedgerStateError(
            'Bu metod artık kullanılmıyor. `collect_and_close` içine '
            '`allow_overpayment=True` ile çağırın (FAZ 30).',
        )
