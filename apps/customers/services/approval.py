"""ApprovalService — Eşik bazlı onay kontrolü (FAZ 14 — Mağaza P&L Hook).

Cari/Emanet Refactor:
  - DISCOUNT, FX_GAIN, FX_LOSS, WRITEOFF, REVERSAL, CORRECTION, OVERPAYMENT
    tipleri eşik üstü olduğunda onay zinciri gerektirir.
  - Eşik aşıldığında entry `requires_approval=True` ile yazılır
    ve `is_approved=False` durumda bekler — bakiyeye dahil edilmez.
  - Onay verme: `approve_entry()` aktör + audit context ile çağrılır.

FAZ 14 Eklentileri:
  - approve_entry() artık post-approval hook ile IncomeExpenseLedger'a
    Mağaza P&L kaydı yazar (DISCOUNT/FX_GAIN/FX_LOSS/WRITEOFF için).
  - write_income_expense_for_ledger_entry(): hem onay anında hem de
    CollectionService'in otomatik onaylı yazımlarında çağrılan ortak
    helper. CustomerLedger satırını IncomeExpenseLedger.entry_type'a
    haritalar.

FAZ 30 Eklentileri (Hızlı Onay Mimarisi):
  - OVERPAYMENT (Kasa Fazlası) tipi → IncomeExpenseLedger.OTHER_INCOME
    olarak haritalanır.
  - `_actor_has_permission` artık rol adı ('admin' string karşılaştırması)
    yerine doğrudan `can_approve_ledger_adjustment` permission'ını kontrol
    eder. Geriye uyum için "Admin"/"Mağaza Admin" rol adı bypass'i de
    korunmuştur.
  - `evaluate_self_approval_capability()` — Hızlı Onay Modalı için backend
    kararı: "Bu aktör kendi kendine onaylayabilir mi?" sorusunu cevaplar.

Yetki seviyeleri (apps.roles.Permission `code` üzerinden):
  - `customer_adjust_minor`         → STAFF üstü; küçük fark (≤ %2)
  - `customer_adjust_major`         → MANAGER; eşik üstü kur farkı/iskonto
  - `customer_writeoff`             → SENIOR_MANAGER; şüpheli alacak silme
  - `can_approve_ledger_adjustment` → şemsiye onay yetkisi (Admin)

is_superuser daima tüm yetkilere sahiptir (acil durum override).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from django.utils import timezone

from apps.customers.services.exceptions import (
    InsufficientApprovalError,
    InvalidLedgerStateError,
)


# ── Eşikler (TL bazlı) ─────────────────────────────────────────
MINOR_TL_THRESHOLD = Decimal('500.00')
MAJOR_TL_THRESHOLD = Decimal('5000.00')
MINOR_PCT_THRESHOLD = Decimal('2.0')   # %
MAJOR_PCT_THRESHOLD = Decimal('10.0')  # %

# FAZ 30 — Hızlı Onay Modalı %10 kuralı:
# Kullanıcı talebi: oluşan fark, kapatılan toplam borcun %10'undan az
# veya eşitse personel kendi kendine onaylayabilir; %10 üstündeyse
# Mağaza Admin onayı zorunludur. MAJOR_PCT_THRESHOLD ile aynı eşik.
SELF_APPROVE_PCT_LIMIT = MAJOR_PCT_THRESHOLD


@dataclass(frozen=True)
class ApprovalLevel:
    code: str          # 'STAFF' | 'MANAGER' | 'SENIOR'
    permission: str    # gerekli permission code

    @classmethod
    def staff(cls):
        return cls(code='STAFF', permission='customer_adjust_minor')

    @classmethod
    def manager(cls):
        return cls(code='MANAGER', permission='customer_adjust_major')

    @classmethod
    def senior(cls):
        return cls(code='SENIOR', permission='customer_writeoff')


# ════════════════════════════════════════════════════════════════════════════
# YETKİ KONTROLÜ
# ════════════════════════════════════════════════════════════════════════════

# Şemsiye onay izni: bu izne sahip kullanıcı tüm cari fark seviyelerini
# (minor/major/senior) onaylayabilir. Mağaza Admin/Patron'a verilir.
_UMBRELLA_APPROVE_PERM = 'can_approve_ledger_adjustment'


def _role_has_permission_code(role, perm_code: str) -> bool:
    """Bir rolün verilen permission'a sahip olup olmadığını kontrol eder.

    Roles → RoleDetail → Permission ilişkisini kullanır; alternatif
    "permissions" M2M ilişkisi de denenir (model çeşitlemesi için
    savunmacı kod).
    """
    if role is None:
        return False
    try:
        return role.role_details.filter(permission__code=perm_code).exists()
    except Exception:
        try:
            return role.permissions.filter(code=perm_code).exists()
        except Exception:
            return False


def _actor_has_permission(actor, perm_code: str) -> bool:
    """Aktörün verilen onay seviyesi permission'ına sahip olup olmadığını dön."""
    if actor is None:
        return False
    if getattr(actor, 'is_superuser', False):
        return True
    if getattr(actor, 'is_staff', False):
        return True

    role = getattr(actor, 'role', None)
    if role is None:
        return False

    # FAZ 30 — Birincil yol: şemsiye permission ('can_approve_ledger_adjustment')
    # tüm onay seviyelerini bypass eder. Permission tabanlı tek doğruluk
    # kaynağı; rol adı string'ine bağımlı değildir, bu yüzden "Mağaza Admin",
    # "Store Admin", "Admin" gibi rol adı çeşitlemeleri sorun değildir.
    if _role_has_permission_code(role, _UMBRELLA_APPROVE_PERM):
        return True

    # FAZ 22.10 → FAZ 30 geriye uyum: rol adı 'admin' içeren kayıtlarda
    # şemsiye permission henüz seed edilmemiş olabilir (mevcut kurulumlar).
    # `python manage.py seed_cari_permissions` koşulduktan sonra bu
    # branch'a düşmeden permission yolundan geçer.
    role_name = (getattr(role, 'name', '') or '').strip().lower()
    if role_name in ('admin', 'mağaza admin', 'magaza admin', 'store admin'):
        return True

    # Spesifik seviye permission'ı
    return _role_has_permission_code(role, perm_code)


def determine_approval_requirement(
    transaction_type: str,
    amount_eur: Decimal,
    open_balance_eur: Decimal,
) -> ApprovalLevel | None:
    """İşlem tipi ve tutara göre gereken onay seviyesini döner.

    None dönerse: onay gerekmez (otomatik onaylı yazılır).
    """
    # WRITEOFF her tutarda en üst onay
    if transaction_type == 'WRITEOFF':
        return ApprovalLevel.senior()

    # CORRECTION sistem aktarımı — sadece SENIOR
    if transaction_type == 'CORRECTION':
        return ApprovalLevel.senior()

    # REVERSAL: küçükse manager, büyükse senior
    if transaction_type == 'REVERSAL':
        if amount_eur >= MAJOR_TL_THRESHOLD:
            return ApprovalLevel.senior()
        return ApprovalLevel.manager()

    # FAZ 30 — OVERPAYMENT (Kasa Fazlası) DISCOUNT ile aynı eşik mantığına
    # tabidir. Mağaza lehine "gelir farkı" oluşturduğu için yüzde + TL
    # eşikleri uygulanır.
    if transaction_type in ('FX_GAIN', 'FX_LOSS', 'DISCOUNT', 'OVERPAYMENT'):
        amount_eur = abs(amount_eur or Decimal('0'))
        open_balance_eur = abs(open_balance_eur or Decimal('0'))

        pct = Decimal('0')
        if open_balance_eur > 0:
            pct = (amount_eur / open_balance_eur) * Decimal('100')

        if amount_eur >= MAJOR_TL_THRESHOLD or pct >= MAJOR_PCT_THRESHOLD:
            return ApprovalLevel.senior()
        if amount_eur >= MINOR_TL_THRESHOLD or pct >= MINOR_PCT_THRESHOLD:
            return ApprovalLevel.manager()
        return ApprovalLevel.staff()

    return None


def assert_actor_can(actor, level: ApprovalLevel | None) -> None:
    """Aktör yetersizse InsufficientApprovalError fırlatır."""
    if level is None:
        return
    if _actor_has_permission(actor, level.permission):
        return
    raise InsufficientApprovalError(
        required_level=level.code,
        actor_level='STAFF' if actor else 'ANONYMOUS',
        required_permission=level.permission,
    )


# ════════════════════════════════════════════════════════════════════════════
# FAZ 30 — HIZLI ONAY MODALI: SELF-APPROVAL DEĞERLENDİRMESİ
# ════════════════════════════════════════════════════════════════════════════

def evaluate_self_approval_capability(
    *,
    actor,
    transaction_type: str,
    amount_eur: Decimal,
    closed_debt_eur: Decimal,
) -> dict:
    """Hızlı onay modalı için backend kararı.

    Tahsilat sırasında oluşan bir fark/kasa fazlası kaydı için:
      - Farkın kapatılan borca oranı (yüzde) hesaplanır.
      - %10 ve altı + aktör en az `customer_adjust_minor` yetkisine sahipse
        kullanıcı kendi kendine onaylayabilir → modal "Onayla" butonu aktif.
      - %10 üstü ise yalnızca şemsiye yetkili (Admin) onaylayabilir; aktör
        admin değilse modal "Yetkili onayı gerekiyor" bilgi state'ine düşer.

    Args:
        actor: onay verecek (veya talep eden) kullanıcı.
        transaction_type: CustomerLedger.transaction_type değeri.
        amount_eur: Fark tutarının TL karşılığı (pozitif).
        closed_debt_eur: Kapatılan toplam borcun TL karşılığı.

    Returns:
        {
          'pct_of_debt': Decimal,             # 0..100, ondalıklı yüzde
          'within_self_approve_band': bool,   # %10 ve altı mı?
          'actor_can_self_approve': bool,     # ekrandaki "Onayla" aktif mi?
          'required_level': str | None,       # 'STAFF' | 'MANAGER' | 'SENIOR'
          'required_permission': str | None,  # gereken permission code
        }
    """
    amount_eur = abs(Decimal(amount_eur or 0))
    closed_debt_eur = abs(Decimal(closed_debt_eur or 0))

    pct = Decimal('0')
    if closed_debt_eur > 0:
        pct = (amount_eur / closed_debt_eur) * Decimal('100')
    pct = pct.quantize(Decimal('0.01'))

    within_band = pct <= SELF_APPROVE_PCT_LIMIT

    level = determine_approval_requirement(
        transaction_type=transaction_type,
        amount_eur=amount_eur,
        open_balance_eur=closed_debt_eur,
    )

    if level is None:
        # Onay gerekmez — kayıt zaten otomatik onaylı yazılır.
        return {
            'pct_of_debt': str(pct),
            'within_self_approve_band': True,
            'actor_can_self_approve': True,
            'required_level': None,
            'required_permission': None,
        }

    # Yetki kontrolü:
    #   - Aktör şemsiye admin yetkisine sahipse → her durumda onaylar.
    #   - Değilse: %10 bandı içindeyse customer_adjust_minor yetiyor.
    can = False
    if _actor_has_permission(actor, _UMBRELLA_APPROVE_PERM):
        can = True
    elif within_band and _actor_has_permission(actor, 'customer_adjust_minor'):
        can = True
    elif _actor_has_permission(actor, level.permission):
        can = True

    return {
        'pct_of_debt': str(pct),
        'within_self_approve_band': bool(within_band),
        'actor_can_self_approve': bool(can),
        'required_level': level.code,
        'required_permission': level.permission,
    }


# ════════════════════════════════════════════════════════════════════════════
# FAZ 14 — Mağaza P&L Hook (IncomeExpenseLedger Yazımı)
# ════════════════════════════════════════════════════════════════════════════

# CustomerLedger.transaction_type → IncomeExpenseLedger.entry_type haritası.
# Sadece P&L'i etkileyen tipler haritaya dahildir; COLLECTION_*, DEBT, CREDIT,
# CUSTODY_OFFSET, REVERSAL, CORRECTION P&L'e doğrudan yansımaz.
_CUSTOMER_LEDGER_TO_PL_ENTRY_TYPE = {
    # FX_GAIN: Müşteri lehine kur farkı → mağaza zararı (Senaryo X)
    # NOT: İsim "FX_GAIN" müşteri perspektifindendir; mağaza için kayıptır.
    'FX_GAIN':  'FX_LOSS_EXPENSE',
    # FX_LOSS: Müşteri aleyhine kur farkı → mağaza karı
    'FX_LOSS':  'FX_GAIN_INCOME',
    # DISCOUNT: Mağaza müşteriye iskonto → gider
    'DISCOUNT': 'DISCOUNT_EXPENSE',
    # WRITEOFF: Şüpheli alacak silme → gider
    'WRITEOFF': 'WRITEOFF_EXPENSE',
    # FAZ 30 — OVERPAYMENT: Müşteri borçtan fazla ödedi → kasa fazlası,
    # mağaza geliri.
    'OVERPAYMENT': 'OTHER_INCOME',
}


# ════════════════════════════════════════════════════════════════════════════
# FAZ 35 — REVERSAL Side-Effect Propagation
# ════════════════════════════════════════════════════════════════════════════
# Bir CustomerLedger.REVERSAL satırı APPROVED durumuna geçtiğinde (yazılırken
# auto-approve veya sonradan approve_entry ile) iki yan etki gerekir:
#
#   1) CashboxLedger karşı hareketi: orijinal COLLECTION_* için yazılmış
#      CashboxLedger.INCOME/EXPENSE satırı varsa, append-only REVERSAL
#      satırı yazılır (parent + related_payment ile bağlı).
#   2) IncomeExpenseLedger.is_reversed: orijinal kayıt P&L'e yansıdıysa
#      (DISCOUNT/FX_GAIN/WRITEOFF/OVERPAYMENT) ilgili satır is_reversed=True
#      bayrağı ile işaretlenir; raporlar bu bayrağı filtreleyebilir.
#
# Bu fonksiyon idempotent'tir: aynı REVERSAL için iki kez çağrılırsa ikinci
# çağrı no-op döner (mevcut CashboxLedger REVERSAL satırı kontrolü ile).
# ════════════════════════════════════════════════════════════════════════════

def propagate_reversal_side_effects(*, reversal, audit: dict):
    """REVERSAL onaylandığında kasa ve P&L tarafını senkronize eder.

    FAZ 51 — R-01/R-08 Sertleştirmesi:
        (a) Eski kod sadece COLLECTION_* tiplerinin INCOME tarafını
            reverse ediyordu. Müşteri modu ÖDEME (DEBT, EXPENSE üreten)
            REVERSAL'larında kasa karşı hareketi yazılmıyordu — kasa-cari
            mutabakatı yarım kalıyordu. Yeni kod COLLECTION_* + DEBT
            (related_payment_id'si olan) için INCOME ↔ EXPENSE simetrisini
            kurar.
        (b) `related_payment_id` yoksa process_no ile fallback arama yapılır
            (eski yetim kayıtlar için). Bulunan tek aday tek bir cashbox
            satırına eşleşmiyorsa (birden çoksa veya eski kayıtsa) sessiz
            atlanır — hatalı çift reverse yapmaz.
        (c) Reverse edilen Payment kayıtları `is_cancelled=True` ve
            `cancelled_at` set edilir → banka mutabakatı "yetim Payment"
            görmez (R-08 onarımı).

    Args:
        reversal: CustomerLedger instance (transaction_type=REVERSAL,
                  is_approved=True olmalı).
        audit: actor + ip + user_agent dict'i.
    """
    from apps.customers.models import CustomerLedger
    from apps.banking.models import CashboxLedger, IncomeExpenseLedger
    from apps.process.models import Payment
    from django.utils import timezone as _tz
    from decimal import Decimal

    if reversal.transaction_type != CustomerLedger.REVERSAL:
        return None
    if not reversal.is_approved:
        return None

    original = reversal.parent
    if original is None:
        return None

    # ── 1) CashboxLedger karşı hareketi ─────────────────────────────
    # COLLECTION_* (INCOME üretti)  → REVERSAL outflow  (bakiye düşer)
    # DEBT — payment_to_customer akışı (EXPENSE üretti) → REVERSAL inflow
    # (bakiye geri artar). Her iki yön için de kasa simetrisi sağlanır.
    is_collection = original.transaction_type in CustomerLedger.COLLECTION_TYPES
    is_debt_with_payment = (
        original.transaction_type == CustomerLedger.DEBT
        and original.related_payment_id is not None
    )

    if is_collection or is_debt_with_payment:
        # Orijinal CashboxLedger satırını related_payment_id ile bul.
        original_cb = None
        if original.related_payment_id is not None:
            cb_qs = CashboxLedger.objects.filter(
                related_payment_id=original.related_payment_id,
            ).exclude(movement_type=CashboxLedger.MovementType.REVERSAL)
            original_cb = cb_qs.order_by('created_on').first()

        # Fallback: related_payment yoksa process_no üzerinden tek aday
        # bulunabiliyorsa onu kullan (eski yetim kayıtlar için).
        if original_cb is None and original.process_no:
            cb_fallback_qs = (
                CashboxLedger.objects
                .filter(process_no=original.process_no)
                .exclude(movement_type=CashboxLedger.MovementType.REVERSAL)
                .filter(parent__isnull=True)  # Yalnız orijinal satırlar
            )
            # Tek aday varsa kullan; çoğul ise belirsizlik nedeniyle atla.
            if cb_fallback_qs.count() == 1:
                original_cb = cb_fallback_qs.first()

        if original_cb is not None:
            # Idempotency — aynı orijinal için zaten REVERSAL var mı?
            existing_rev = CashboxLedger.objects.filter(
                parent=original_cb,
                movement_type=CashboxLedger.MovementType.REVERSAL,
            ).exists()
            if not existing_rev:
                # Yön: orijinal INCOME idiyse REVERSAL bakiyeyi düşürür;
                # EXPENSE idiyse bakiye geri artar.
                was_income = (
                    original_cb.movement_type
                    == CashboxLedger.MovementType.INCOME
                )
                try:
                    prior_balance = original_cb.cashbox.get_balance(
                        currency=original_cb.currency,
                    )
                except Exception:
                    prior_balance = Decimal('0')
                try:
                    delta = original_cb.amount_eur_equivalent
                    if was_income:
                        new_balance = (prior_balance - delta).quantize(Decimal('0.01'))
                    else:
                        new_balance = (prior_balance + delta).quantize(Decimal('0.01'))
                except Exception:
                    new_balance = prior_balance

                CashboxLedger.objects.create(
                    cashbox=original_cb.cashbox,
                    store=original_cb.store,
                    movement_type=CashboxLedger.MovementType.REVERSAL,
                    amount=original_cb.amount,
                    currency=original_cb.currency,
                    amount_eur_equivalent=original_cb.amount_eur_equivalent,
                    exchange_rate=original_cb.exchange_rate,
                    balance_snapshot=new_balance,
                    related_payment=original_cb.related_payment,
                    parent=original_cb,
                    process_no=original_cb.process_no,
                    description=(
                        f'İPTAL: CustomerLedger REVERSAL #{reversal.pk} — '
                        f'{(reversal.description or "")[:120]}'
                    )[:255],
                    created_by=audit.get('actor'),
                    ip_address=audit.get('ip_address'),
                    user_agent=audit.get('user_agent') or '',
                )

        # ── 1.b) Payment.is_cancelled=True (R-08) ──────────────────
        # Reverse edilen Payment'ı banka mutabakatı için iptal işaretle.
        # cancel_row FAZ 41 yolu zaten kendisi yapıyor; burası bağımsız
        # kanallardan (cari_reverse, vb.) gelen REVERSAL'ları yakalar.
        if original.related_payment_id:
            try:
                Payment.objects.filter(
                    id=original.related_payment_id,
                    is_cancelled=False,
                ).update(
                    is_cancelled=True,
                    cancelled_at=_tz.now(),
                )
            except Exception:
                # Payment modelinde cancelled_at yoksa veya başka bir
                # alan varsa sessiz geç — kritik akışı bozmasın.
                try:
                    Payment.objects.filter(
                        id=original.related_payment_id,
                        is_cancelled=False,
                    ).update(is_cancelled=True)
                except Exception:
                    pass

    # ── 2) IncomeExpenseLedger.is_reversed bayrağı ──────────────────
    # Orijinal CustomerLedger için P&L kaydı varsa is_reversed=True işaretle.
    # Bu DISCOUNT/FX_GAIN/FX_LOSS/WRITEOFF/OVERPAYMENT için geçerli.
    pl_original = IncomeExpenseLedger.objects.filter(
        related_customer_ledger=original,
        is_reversed=False,
    ).first()
    if pl_original is not None:
        pl_original.is_reversed = True
        pl_original.save(update_fields=['is_reversed'])

    return None


def write_income_expense_for_ledger_entry(*, entry, audit: dict):
    """Bir CustomerLedger satırı için Mağaza P&L kaydı yazar.

    Bu fonksiyon iki yerden çağrılır:
      1) CollectionService.collect_and_close → adjustment otomatik
         onaylandıysa hemen burada yazılır.
      2) approve_entry → onay bekleyen bir kayıt onaylandığında.

    Aynı CustomerLedger için çift IncomeExpenseLedger yazımı yapılmaz
    (idempotency: related_customer_ledger üzerinde unique kontrol).

    Args:
        entry: CustomerLedger instance (is_approved=True olmalı).
        audit: extract_audit_context çıktısı.

    Returns:
        IncomeExpenseLedger instance veya None (haritaya dahil değilse).
    """
    pl_type = _CUSTOMER_LEDGER_TO_PL_ENTRY_TYPE.get(entry.transaction_type)
    if pl_type is None:
        return None

    if not entry.is_approved:
        # Güvenlik kontrolü: onaylanmamış kayıt için P&L yazılmamalı.
        raise InvalidLedgerStateError(
            'Onaylanmamış CustomerLedger satırı için Mağaza P&L kaydı '
            'yazılamaz.',
        )

    from apps.banking.models import IncomeExpenseLedger

    # Idempotency: aynı entry için zaten yazılmış mı?
    existing = IncomeExpenseLedger.objects.filter(
        related_customer_ledger=entry,
    ).first()
    if existing:
        return existing

    return IncomeExpenseLedger.objects.create(
        store=entry.store,
        entry_type=pl_type,
        amount_eur=entry.amount_eur,
        amount_hs=entry.amount_hs,
        exchange_rate_eur=entry.exchange_rate_eur,
        related_customer_ledger=entry,
        description=(
            f'{entry.get_transaction_type_display()} '
            f'(Müşteri: {entry.customer}) — {entry.description or ""}'
        )[:255],
        created_by=audit.get('actor') or entry.approved_by or entry.created_by,
        ip_address=audit.get('ip_address'),
        user_agent=audit.get('user_agent') or '',
    )


# ════════════════════════════════════════════════════════════════════════════
# ONAYLAMA AKIŞI
# ════════════════════════════════════════════════════════════════════════════

def approve_entry(entry, actor, audit: dict, note: str = '') -> None:
    """Bekleyen bir CustomerLedger satırını onayla.

    FAZ 14 — Onay sonrasında ilgili IncomeExpenseLedger satırı otomatik
    olarak yazılır (DISCOUNT/FX_GAIN/FX_LOSS/WRITEOFF/OVERPAYMENT için).

    FAZ 16 (P1-02 düzeltmesi):
        Eşzamanlı iki onay çağrısı arasındaki race condition'ı önlemek
        için atomic blok içinde CustomerLedger satırına SELECT FOR UPDATE
        uygulanır ve is_approved kontrolü kilit altında tekrarlanır.
        Böylece ikinci thread bekler, sonra "zaten onaylanmış" hatasını
        deterministic olarak alır.

    FAZ 30 — Onay seviyesi belirlenirken kapatılan borcun TL'si yerine
        kaydın kendi tutarı baz alınır (mevcut davranış korunur). Hızlı
        Onay Modalı'nda %10 kararı UI tarafında `pct_of_debt` üzerinden
        verilir; backend'de aktör yetki kontrolü her durumda çalışır.

    Args:
        entry: CustomerLedger instance (is_approved=False olmalı)
        actor: onaylayan kullanıcı
        audit: extract_audit_context çıktısı
        note: opsiyonel onay notu
    """
    from django.db import transaction as db_transaction
    from apps.customers.models import CustomerLedger

    # ── Hızlı erken-çıkış (kilit almadan) ─────────────────────────
    # Önemli: bu kontrol race condition'ı çözmez (kilit altındaki
    # kontrol gerçek savunma); fakat yetkisiz aktörlerin gereksiz
    # row lock almasını engelleyerek lock contention'ı azaltır.
    if not entry.requires_approval:
        raise InvalidLedgerStateError('Bu kayıt onay gerektirmiyor.')

    # Onay verecek aktör, kaydın eşik seviyesine sahip olmalı.
    # Bu kontrol entry'nin değişmez alanlarına (amount_eur, transaction_type)
    # bağlı olduğu için kilit dışında yapılabilir.
    level = determine_approval_requirement(
        entry.transaction_type,
        entry.amount_eur,
        entry.amount_eur,  # aşamalı yaklaşım: kayıt anındaki tutarı baz alıyoruz
    )
    assert_actor_can(actor, level)

    # ── Atomik blok: kilit + idempotency + onay + Mağaza P&L ─────
    with db_transaction.atomic():
        # FAZ 16 — P1-02 düzeltmesi: satırı kilitle ve TAZE oku
        try:
            locked_entry = (
                CustomerLedger.objects
                .select_for_update()
                .get(pk=entry.pk)
            )
        except CustomerLedger.DoesNotExist:
            raise InvalidLedgerStateError(
                'Onaylanacak ledger satırı bulunamadı.',
            )

        # Kilit altında idempotency kontrolü — gerçek yarış savunması
        if locked_entry.is_approved:
            raise InvalidLedgerStateError('Kayıt zaten onaylanmış.')
        if not locked_entry.requires_approval:
            # Aradaki süreçte kaydın requires_approval'ı düşürülmüş olabilir
            raise InvalidLedgerStateError('Bu kayıt onay gerektirmiyor.')

        # Onayı uygula — caller'ın elindeki `entry` instance'ını da
        # in-place güncelle (geriye dönük uyumluluk: çağıran kod
        # `entry.is_approved` üzerinden state okumaya devam edebilir).
        now_ts = timezone.now()
        locked_entry.is_approved = True
        locked_entry.approved_by = actor
        locked_entry.approved_at = now_ts
        if note:
            locked_entry.approval_note = note
        locked_entry.save(update_fields=[
            'is_approved', 'approved_by', 'approved_at', 'approval_note',
        ])

        # Caller referansını da güncelle (eski davranışı koru)
        entry.is_approved = True
        entry.approved_by = actor
        entry.approved_at = now_ts
        if note:
            entry.approval_note = note

        # FAZ 14 — Mağaza P&L hook (kilit altında, çift yazım imkânsız)
        hook_audit = {
            'actor': actor,
            'ip_address': audit.get('ip_address') if audit else None,
            'user_agent': (audit.get('user_agent') if audit else None) or '',
        }
        write_income_expense_for_ledger_entry(
            entry=locked_entry,
            audit=hook_audit,
        )

        # FAZ 35 — REVERSAL onayı geldiyse kasa ve P&L tarafını senkronize et.
        # reverse_entry write anında auto-approved durumda yan etkileri
        # zaten çalıştırır; bu hook PENDING REVERSAL'ın geç onayını yakalar.
        if locked_entry.transaction_type == CustomerLedger.REVERSAL:
            propagate_reversal_side_effects(
                reversal=locked_entry,
                audit=hook_audit,
            )
