"""LedgerService — Append-Only CustomerLedger yazımı.

Tüm cari hareket yazımları bu servis üzerinden geçmelidir. Doğrudan
`CustomerLedger.objects.create(...)` çağırmak yasaktır; çünkü:
  - Çift birim (HS+TL+exchange_rate) hesaplaması burada yapılır.
  - Onay zinciri (requires_approval / is_approved) burada belirlenir.
  - Audit context (created_by, ip_address, user_agent) burada yazılır.
  - REVERSAL karşı girişi `reversal_target_type` ile burada bağlanır.

NOT: Bu servis transaction.atomic() açmaz — caller (CollectionService,
CustodyOffsetService veya view) atomik bloğu kendi yönetir. Birden
fazla satır eşzamanlı yazılacaksa caller atomik blok kurar.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.db import models
from django.utils import timezone

from apps.customers.models import CustomerLedger
from apps.banking.exchange_rate_service import (
    get_current_has_rate, get_current_fx_rate,
)
from apps.customers.services.approval import (
    determine_approval_requirement, assert_actor_can,
)
from apps.customers.services.exceptions import InvalidLedgerStateError


class LedgerService:

    # ── DEBT (Satış borcu) ──────────────────────────────────────
    @staticmethod
    def write_debt(
        *, customer, store, amount_hs: Decimal,
        process_no: str, audit: dict,
        description: str = '',
        amount_eur: Optional[Decimal] = None,
        exchange_rate_eur: Optional[Decimal] = None,
        related_payment=None,
    ) -> CustomerLedger:
        """Satış sırasında müşterinin borcunu artıran kayıt.

        FAZ 38 — `amount_eur` opsiyonel:
            Verilirse o değer aynen yazılır (TL modu için satış anındaki
            TL küsuratını kaybetmemek lazım — örn. 1.677 HS satışta
            11.383,00 TL fiyatlandı, gram × kur yuvarlaması ile
            11.385,49 yerine 11.383,00 saklanır).
            Verilmezse `amount_hs × current_rate` ile hesaplanır
            (eski davranış, HS modu).

        FAZ 38 — `exchange_rate_eur` opsiyonel:
            Verilirse o kur saklanır (satış kuru = SATIŞ kuru); yoksa
            anlık piyasa kuru çekilir.

        FAZ 51 — `related_payment` opsiyonel:
            "Müşteri modu ödeme" akışında (CashboxLedger.EXPENSE üreten
            DEBT yazımı) Payment ile satırı bağlar. Bu bağlantı sayesinde
            REVERSAL anında `propagate_reversal_side_effects` kasa karşı
            hareketini doğrudan bulur (R-01 kasa simetri onarımı).
            Satış akışı (Process) için verilmesine gerek yok.
        """
        if exchange_rate_eur is not None:
            rate = Decimal(exchange_rate_eur)
        else:
            rate = get_current_has_rate(store) or Decimal('0')

        if amount_eur is not None:
            tl = Decimal(amount_eur).quantize(Decimal('0.01'))
        else:
            tl = (Decimal(amount_hs) * rate).quantize(Decimal('0.01'))

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=CustomerLedger.DEBT,
            amount_hs=Decimal(amount_hs),
            amount_eur=tl,
            exchange_rate_eur=rate,
            currency=CustomerLedger.CURRENCY_HS,
            process_no=process_no,
            description=description,
            requires_approval=False,
            is_approved=True,
            related_payment=related_payment,
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── CREDIT (Müşteri alacağı) ────────────────────────────────
    @staticmethod
    def write_credit(
        *, customer, store, amount_hs: Decimal,
        process_no: str, audit: dict,
        description: str = '',
        amount_eur: Optional[Decimal] = None,
        exchange_rate_eur: Optional[Decimal] = None,
        related_payment=None,
    ) -> CustomerLedger:
        """İade veya fazla ödeme sonrası müşterinin alacağı.

        FAZ 38 — `amount_eur` ve `exchange_rate_eur` opsiyonel parametreler
        `write_debt` ile aynı semantik (TL modu için stored TL korunur).

        FAZ 51 — `related_payment`: write_debt ile aynı amaçla — REVERSAL
        akışında kasa eşlemesini garanti eder.
        """
        if exchange_rate_eur is not None:
            rate = Decimal(exchange_rate_eur)
        else:
            rate = get_current_has_rate(store) or Decimal('0')

        if amount_eur is not None:
            tl = Decimal(amount_eur).quantize(Decimal('0.01'))
        else:
            tl = (Decimal(amount_hs) * rate).quantize(Decimal('0.01'))

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=CustomerLedger.CREDIT,
            amount_hs=Decimal(amount_hs),
            amount_eur=tl,
            exchange_rate_eur=rate,
            currency=CustomerLedger.CURRENCY_HS,
            process_no=process_no,
            description=description,
            is_approved=True,
            related_payment=related_payment,
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── COLLECTION (Tahsilat) ───────────────────────────────────
    @staticmethod
    def write_collection(
        *, customer, store, transaction_type: str,
        amount_hs: Decimal, amount_eur: Decimal,
        exchange_rate_eur: Decimal,
        process_no: str, audit: dict,
        currency: str = 'TRY',
        amount_fx: Decimal = Decimal('0.00'),
        fx_to_eur_rate: Decimal = Decimal('0.000000'),
        related_payment=None,
        description: str = '',
    ) -> CustomerLedger:
        """Tahsilat (COLLECTION_TL/FX/HS) yazımı.

        Tahsilat satırları onay GEREKTİRMEZ — kasaya gerçek para
        girişiyle eşleşir. Manipülasyon noktası "fark" satırlarıdır
        (FX_GAIN, DISCOUNT) ve onlar ayrı yazılır.
        """
        if transaction_type not in CustomerLedger.COLLECTION_TYPES:
            raise InvalidLedgerStateError(
                f'Geçersiz tahsilat tipi: {transaction_type}',
            )

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=transaction_type,
            amount_hs=Decimal(amount_hs),
            amount_eur=Decimal(amount_eur),
            amount_fx=Decimal(amount_fx),
            exchange_rate_eur=Decimal(exchange_rate_eur),
            fx_to_eur_rate=Decimal(fx_to_eur_rate),
            currency=currency,
            process_no=process_no,
            related_payment=related_payment,
            description=description,
            requires_approval=False,
            is_approved=True,
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── ADJUSTMENT (Kur farkı / İskonto / WriteOff) ─────────────
    @staticmethod
    def write_adjustment(
        *, customer, store, transaction_type: str,
        amount_hs: Decimal, amount_eur: Decimal,
        exchange_rate_eur: Decimal,
        process_no: str, audit: dict,
        parent: Optional[CustomerLedger] = None,
        description: str = '',
        open_balance_eur: Decimal = Decimal('0'),
    ) -> CustomerLedger:
        """Kur farkı / iskonto / yazım kayıt fişi.

        Otomatik olarak eşik bazlı onay seviyesini belirler. Aktör
        yetersizse:
          - is_approved=False olarak yazar (PENDING durumu)
          - requires_approval=True olarak işaretler
        Aktör yeterliyse:
          - is_approved=True + approved_by=actor olarak yazar.
        """
        if transaction_type not in (
            CustomerLedger.FX_GAIN, CustomerLedger.FX_LOSS,
            CustomerLedger.DISCOUNT, CustomerLedger.WRITEOFF,
        ):
            raise InvalidLedgerStateError(
                f'Geçersiz adjustment tipi: {transaction_type}',
            )

        if not description:
            raise InvalidLedgerStateError(
                f'{transaction_type} için açıklama zorunludur.',
            )

        actor = audit.get('actor')
        level = determine_approval_requirement(
            transaction_type=transaction_type,
            amount_eur=Decimal(amount_eur),
            open_balance_eur=Decimal(open_balance_eur),
        )

        # Aktör yeterli mi kontrolü; yetersizse kayıt is_approved=False
        # olarak yazılır (üst onay beklemede). assert_actor_can yerine
        # sessiz kontrol yapıyoruz.
        from apps.customers.services.approval import _actor_has_permission
        is_approved = (
            level is None
            or _actor_has_permission(actor, level.permission)
        )

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=transaction_type,
            amount_hs=Decimal(amount_hs),
            amount_eur=Decimal(amount_eur),
            exchange_rate_eur=Decimal(exchange_rate_eur),
            currency=CustomerLedger.CURRENCY_TRY,
            process_no=process_no,
            parent=parent,
            description=description,
            requires_approval=(level is not None),
            is_approved=is_approved,
            approved_by=actor if is_approved else None,
            approved_at=timezone.now() if is_approved else None,
            created_by=actor,
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── OVERPAYMENT (Kasa Fazlası — FAZ 30) ─────────────────────
    @staticmethod
    def write_overpayment(
        *, customer, store, amount_hs: Decimal, amount_eur: Decimal,
        exchange_rate_eur: Decimal,
        process_no: str, audit: dict,
        parent: Optional[CustomerLedger] = None,
        related_payment=None,
        description: str = '',
        closed_debt_eur: Decimal = Decimal('0'),
    ) -> CustomerLedger:
        """Müşteri borçtan fazla ödediğinde oluşan "kasa fazlası" kaydı.

        Bu satır müşteri bakiyesini ETKİLEMEZ (debt-neutral); sadece
        sistemin "X kadar fazla geldi, mağaza bunu Diğer Gelir olarak
        aldı" bilgisini denetlenebilir biçimde tutar.

        Onay akışı DISCOUNT/FX_GAIN ile aynıdır:
          - Fark, kapatılan borcun ≤ %2 ise STAFF; ≤ %10 ise MANAGER;
            > %10 ise SENIOR onayı gerekir.
          - Aktör yeterliyse `is_approved=True` olarak yazılır ve hemen
            IncomeExpenseLedger.OTHER_INCOME satırı yazılır
            (CollectionService tarafında).
          - Yetersizse `requires_approval=True, is_approved=False` —
            ApprovalService.approve_entry akışı bekler.

        Args:
            customer, store: zorunlu.
            amount_hs, amount_eur: Fazla tahsilatın HS/TL karşılığı (pozitif).
            exchange_rate_eur: Yazım anındaki has kuru.
            process_no: Tahsilat process_no'su (genelde aynısı).
            audit: extract_audit_context çıktısı.
            parent: İlgili COLLECTION_TL satırı (referans).
            related_payment: Tahsilat Payment kaydı.
            description: Açıklama.
            closed_debt_eur: Kapatılan borcun TL'si — onay seviyesi
                           hesabı için tabandır (yüzde oranı).

        Returns:
            Yazılmış CustomerLedger (transaction_type=OVERPAYMENT).
        """
        if Decimal(amount_eur) <= 0:
            raise InvalidLedgerStateError(
                'OVERPAYMENT tutarı pozitif olmalıdır.',
            )

        actor = audit.get('actor')
        from apps.customers.services.approval import _actor_has_permission
        level = determine_approval_requirement(
            transaction_type=CustomerLedger.OVERPAYMENT,
            amount_eur=Decimal(amount_eur),
            open_balance_eur=Decimal(closed_debt_eur),
        )
        is_approved = (
            level is None
            or _actor_has_permission(actor, level.permission)
        )

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=CustomerLedger.OVERPAYMENT,
            amount_hs=Decimal(amount_hs),
            amount_eur=Decimal(amount_eur),
            exchange_rate_eur=Decimal(exchange_rate_eur),
            currency=CustomerLedger.CURRENCY_TRY,
            process_no=process_no,
            parent=parent,
            related_payment=related_payment,
            description=description or 'Kasa Fazlası (Fazla Tahsilat)',
            requires_approval=(level is not None),
            is_approved=is_approved,
            approved_by=actor if is_approved else None,
            approved_at=timezone.now() if is_approved else None,
            created_by=actor,
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── CUSTODY_OFFSET (Emanet → Cari mahsuplaşma) ──────────────
    @staticmethod
    def write_custody_offset(
        *, customer, store, amount_hs: Decimal,
        process_no: str, audit: dict,
        related_custody=None,
        description: str = '',
    ) -> CustomerLedger:
        """Emanet altın cariden düşüldüğünde yazılan satır.

        Birim dönüşümü (HS→HS) olduğundan kur farkı yok.
        """
        rate = get_current_has_rate(store) or Decimal('0')
        amount_eur = (Decimal(amount_hs) * rate).quantize(Decimal('0.01'))

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=CustomerLedger.CUSTODY_OFFSET,
            amount_hs=Decimal(amount_hs),
            amount_eur=amount_eur,
            exchange_rate_eur=rate,
            currency=CustomerLedger.CURRENCY_HS,
            process_no=process_no,
            related_custody=related_custody,
            description=description or 'Emanet mahsuplaşması',
            requires_approval=False,
            is_approved=True,
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── REVERSAL (Karşı Giriş) ──────────────────────────────────
    @staticmethod
    def reverse_entry(
        *, original: CustomerLedger, audit: dict,
        reason: str,
    ) -> CustomerLedger:
        """Bir CustomerLedger satırını APPEND-ONLY mantığıyla iptal eder.

        Orijinal kayda dokunulmaz; karşı giriş (REVERSAL) yazılır.
        Onay zinciri tutara göre belirlenir (yetersizse PENDING kalır).

        FAZ 35 — REVERSAL Side-Effect Propagation:
            • `related_payment` orijinalden kopyalanır → tahsilat-iptal
              audit zinciri korunur (Kasa-Cari mutabakat raporu için).
            • is_approved=True ise yan etkiler (CashboxLedger.REVERSAL +
              IncomeExpenseLedger.is_reversed) burada yazılır.
            • PENDING (onay bekliyor) ise yan etkiler `approve_entry`
              tarafından tetiklenir.
        """
        if original.transaction_type == CustomerLedger.REVERSAL:
            raise InvalidLedgerStateError(
                'Bir REVERSAL kaydı tekrar REVERSAL yapılamaz.',
            )

        # Aynı orijinal için zaten REVERSAL var mı? (çift iptal koruması)
        already = CustomerLedger.objects.filter(
            transaction_type=CustomerLedger.REVERSAL,
            parent=original,
        ).exists()
        if already:
            raise InvalidLedgerStateError(
                'Bu kayıt zaten iptal edilmiş.',
            )

        if not reason:
            raise InvalidLedgerStateError(
                'REVERSAL için iptal nedeni zorunludur.',
            )

        actor = audit.get('actor')
        level = determine_approval_requirement(
            transaction_type=CustomerLedger.REVERSAL,
            amount_eur=original.amount_eur,
            open_balance_eur=original.amount_eur,
        )
        from apps.customers.services.approval import _actor_has_permission
        is_approved = (
            level is None
            or _actor_has_permission(actor, level.permission)
        )

        reversal = CustomerLedger.objects.create(
            customer=original.customer,
            store=original.store,
            transaction_type=CustomerLedger.REVERSAL,
            amount_hs=original.amount_hs,
            amount_eur=original.amount_eur,
            exchange_rate_eur=original.exchange_rate_eur,
            currency=original.currency,
            process_no=original.process_no,
            parent=original,
            reversal_target_type=original.transaction_type,
            related_payment=original.related_payment,
            related_custody=original.related_custody,
            description=f'İPTAL: {reason}',
            requires_approval=(level is not None),
            is_approved=is_approved,
            approved_by=actor if is_approved else None,
            approved_at=timezone.now() if is_approved else None,
            created_by=actor,
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

        # FAZ 35 — Onaylı REVERSAL ise yan etkileri hemen yaz.
        # PENDING ise approve_entry tarafından tetiklenir.
        if is_approved:
            from apps.customers.services.approval import (
                propagate_reversal_side_effects,
            )
            propagate_reversal_side_effects(
                reversal=reversal,
                audit=audit,
            )

        return reversal

    # ── CORRECTION (Sistem aktarım düzeltmesi) ──────────────────
    @staticmethod
    def write_correction(
        *, customer, store, amount_hs_signed: Decimal,
        audit: dict, description: str,
        process_no: str = '',
        rate: Optional[Decimal] = None,
    ) -> CustomerLedger:
        """Sistem aktarım düzeltmesi (eski ERP'den göç vb.).

        Yön (+/-) `amount_hs_signed` ile gelir. Daima SENIOR onayı
        gerektirir.

        FAZ 35 — Kur Parametresi:
            `rate` verilirse o kurla TL hesaplanır (ör: bir DEBT'in
            kuruyla simetrik düzeltme yapmak için). Verilmezse anlık
            piyasa ALIŞ kuru kullanılır (eski davranış).
        """
        if not description:
            raise InvalidLedgerStateError(
                'CORRECTION için açıklama zorunludur.',
            )

        amount_hs_signed = Decimal(amount_hs_signed)
        if rate is None:
            rate = get_current_has_rate(store) or Decimal('0')
        else:
            rate = Decimal(rate)
        amount_eur = (abs(amount_hs_signed) * rate).quantize(Decimal('0.01'))

        actor = audit.get('actor')
        level = determine_approval_requirement(
            transaction_type=CustomerLedger.CORRECTION,
            amount_eur=amount_eur,
            open_balance_eur=Decimal('0'),
        )
        from apps.customers.services.approval import _actor_has_permission
        is_approved = _actor_has_permission(actor, level.permission)

        return CustomerLedger.objects.create(
            customer=customer,
            store=store,
            transaction_type=CustomerLedger.CORRECTION,
            amount_hs=abs(amount_hs_signed),
            amount_hs_signed=amount_hs_signed,
            amount_eur=amount_eur,
            exchange_rate_eur=rate,
            currency=CustomerLedger.CURRENCY_HS,
            process_no=process_no,
            description=description,
            requires_approval=True,
            is_approved=is_approved,
            approved_by=actor if is_approved else None,
            approved_at=timezone.now() if is_approved else None,
            created_by=actor,
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

    # ── Bakiye Sorgulama Yardımcısı ─────────────────────────────
    @staticmethod
    def get_open_balance_hs(customer) -> Decimal:
        """Onaylanmış aktif bakiye (Has)."""
        # Customers.balance_hs property'sini doğrudan kullan
        return customer.balance_hs

    @staticmethod
    def get_open_balance_eur_at_rate(customer, rate: Decimal) -> Decimal:
        """Mevcut açık borcun verilen kurla TL karşılığı."""
        balance_hs = LedgerService.get_open_balance_hs(customer)
        return (balance_hs * rate).quantize(Decimal('0.01'))

    # ── FAZ 33.3 — Stored TL Bakiye Sorgulama ───────────────────
    @staticmethod
    def get_open_balance_eur(customer) -> Decimal:
        """Onaylanmış aktif bakiye (TL — stored, borç yazıldığı andaki kur).

        Σ signed amount_eur. Anlık piyasa kuru kullanılmaz; her satır
        kendi tarihi `exchange_rate_eur`'siyle yazılmış TL'sini saklar.
        Kur dalgalanması cari ekrana yansımaz → "satıştaki TL = cari
        TL = tahsilattaki TL" garantisi.
        """
        return customer.balance_eur

    @staticmethod
    def get_effective_rate_tl(customer) -> Decimal | None:
        """Açık bakiyenin efektif TL/HS kuru.

        balance_eur / balance_hs → tahsilat ekranında kullanıcının
        girdiği TL'i HS'ye çevirmek için doğru kuru döner. Bakiye
        sıfır veya işaret tutarsız ise None.
        """
        return customer.effective_rate_tl
