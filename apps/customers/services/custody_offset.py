"""CustodyOffsetService — Emanet ↔ Cari Mahsuplaşma.

Senaryo:
  Müşterinin 50 gr emanet altını mağazada; 30 gr Has borcu var.
  Müşteri 20 gr emanet ile borcun bir kısmını kapatmak istiyor.

  Atomik blok:
    1) CustomerCustodyLedger.OFFSET (20 gr çıkış, parent: ilgili emanet)
    2) CustomerLedger.CUSTODY_OFFSET (-20 gr borç azalışı)
       related_custody → 1. adımdaki kayda
    3) (FAZ 22 — A-4) Kur farkı tespiti:
       Emanet bırakıldığı andaki kur ile bugünkü kur farkı varsa
       CustomerLedger'a FX_GAIN/FX_LOSS satırı yazılır. Bu satır,
       has bazlı mahsuplaşmanın TL bazlı raporlama yansımasını
       şeffaflaştırır; kuyumcunun "tutar tutmuyor" sorusunu giderir.
    4) (opsiyonel) Stok sistemine 20 gr giriş — bu adım işin
       envanter tarafıdır ve bu serviste tetiklenmez; çağıran
       view kendi sorumluluğunda kuyumcunun stoğuna ekleme yapar.

  Has → Has dönüşümünde altın bazlı kur farkı yoktur, TL bazlı
  rapor değişiminin denetimsel kaydı tutulur.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List

from django.db import transaction
from django.db.models import Sum, Q
from django.db.models.functions import Coalesce

from apps.customers.models import CustomerLedger
from apps.custody.models import CustomerCustodyLedger
from apps.banking.exchange_rate_service import get_current_has_rate
from apps.customers.services.ledger import LedgerService
from apps.customers.services.exceptions import (
    InsufficientCustodyError,
    InvalidLedgerStateError,
)


_HS_QUANT = Decimal('0.001')
_TL_QUANT = Decimal('0.01')

# Kur farkı yazımı için minimum eşik (TL). Bu eşiğin altındaki yuvarlama
# farkları için ayrı bir FX satırı yazılmaz; sadece raporlama amaçlı.
_FX_DIFF_MIN_TL = Decimal('1.00')


def _q_hs(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_HS_QUANT, rounding=ROUND_HALF_UP)


def _q_tl(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_TL_QUANT, rounding=ROUND_HALF_UP)


@dataclass
class CustodyOffsetResult:
    custody_entry: CustomerCustodyLedger = None
    ledger_entry: CustomerLedger = None
    fx_entry: Optional[CustomerLedger] = None
    fx_diff_tl: Decimal = Decimal('0.00')
    custody_avg_rate: Decimal = Decimal('0.000000')
    current_rate: Decimal = Decimal('0.000000')
    offset_hs: Decimal = Decimal('0')
    new_custody_balance_hs: Decimal = Decimal('0')
    new_ledger_balance_hs: Decimal = Decimal('0')


def get_custody_balance_hs(customer) -> Decimal:
    """Müşterinin net emanet bakiyesi (Has).

    = Σ(IN.amount_hs)
    - Σ(OUT.amount_hs + OFFSET.amount_hs)
    - REVERSAL etkisi (parent tipine göre)

    Yalnız is_active=True ve is_deleted=False kayıtlar dahil edilir
    (FAZ 22 — soft-delete sonrası audit izi korunur, bakiyeye dahil
    edilmez).

    Geriye uyum: is_returned=True kayıtlar (eski toggle pattern'i)
    "iade edilmiş" sayılır → IN sayısından düşülür.
    """
    qs = CustomerCustodyLedger.objects.filter(
        customer=customer,
        is_active=True,
        is_deleted=False,
    )

    agg = qs.aggregate(
        in_total=Coalesce(
            Sum(
                'amount_hs',
                filter=Q(custody_type=CustomerCustodyLedger.CUSTODY_IN,
                         is_returned=False),
            ),
            Decimal('0'),
        ),
        out_total=Coalesce(
            Sum(
                'amount_hs',
                filter=Q(custody_type__in=(
                    CustomerCustodyLedger.CUSTODY_OUT,
                    CustomerCustodyLedger.CUSTODY_OFFSET,
                    # FAZ 24 — Stoğa transfer de bakiyeyi azaltır
                    CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER,
                )),
            ),
            Decimal('0'),
        ),
    )

    in_total = agg['in_total'] or Decimal('0')
    out_total = agg['out_total'] or Decimal('0')

    # REVERSAL etkisi
    reversal_effect = Decimal('0')
    reversals = qs.filter(custody_type=CustomerCustodyLedger.CUSTODY_REVERSAL).select_related('parent')
    for r in reversals:
        reversal_effect += r.signed_amount_hs

    return _q_hs(in_total - out_total + reversal_effect)


def _get_custody_avg_rate(customer, store) -> Decimal:
    """Aktif emanetlerin ağırlıklı ortalama bırakma kuru (1 gr Has = X TL).

    Mahsuplaşma anında "kur farkı" hesabı için baz alınır.
    Σ(amount_eur) / Σ(amount_hs) — ağırlıklı ortalama.
    Sıfır bakiye → 0 (kayıt yok ya da hepsi çıkmış).
    """
    qs = CustomerCustodyLedger.objects.filter(
        customer=customer,
        store=store,
        custody_type=CustomerCustodyLedger.CUSTODY_IN,
        is_active=True,
        is_deleted=False,
        is_returned=False,
    )
    agg = qs.aggregate(
        total_hs=Coalesce(Sum('amount_hs'), Decimal('0')),
        total_tl=Coalesce(Sum('amount_eur'), Decimal('0')),
    )
    total_hs = agg['total_hs'] or Decimal('0')
    total_tl = agg['total_tl'] or Decimal('0')
    if total_hs <= 0:
        return Decimal('0')
    return (total_tl / total_hs).quantize(Decimal('0.000001'))


def preview_offset_fx_diff(customer, store, amount_hs: Decimal) -> dict:
    """Mahsuplaşma öncesi kur farkı önizlemesi (yazma yapmadan).

    Returns:
        {
          'custody_avg_rate', 'current_rate',
          'amount_eur_at_custody', 'amount_eur_at_current',
          'fx_diff_tl', 'fx_direction' ('GAIN'|'LOSS'|'FLAT'),
        }
    """
    amount_hs = _q_hs(Decimal(amount_hs))
    custody_rate = _get_custody_avg_rate(customer, store)
    current_rate = get_current_has_rate(store) or Decimal('0')

    amount_eur_custody = _q_tl(amount_hs * custody_rate) if custody_rate > 0 else Decimal('0.00')
    amount_eur_current = _q_tl(amount_hs * current_rate) if current_rate > 0 else Decimal('0.00')

    fx_diff_tl = amount_eur_current - amount_eur_custody
    if abs(fx_diff_tl) < _FX_DIFF_MIN_TL:
        direction = 'FLAT'
    elif fx_diff_tl > 0:
        # Bugünkü kur eski kurdan yüksek → emanet TL bazda DEĞER kazanmış
        # → müşteri lehine, mağaza zararı (FX_GAIN tipini biz "mağaza zararı"
        # için kullanıyoruz; CustomerLedger.FX_GAIN tanımı: 'Kur Farkı
        # Zararı (mağaza zararı kabul)'). İşaret: borç kapatıldıkça
        # TL bazlı borç ek olarak silinmeli (mağaza zarar yazıyor).
        direction = 'GAIN'
    else:
        direction = 'LOSS'

    return {
        'custody_avg_rate': str(custody_rate),
        'current_rate': str(current_rate),
        'amount_eur_at_custody': str(amount_eur_custody),
        'amount_eur_at_current': str(amount_eur_current),
        'fx_diff_tl': str(fx_diff_tl.quantize(_TL_QUANT)),
        'fx_direction': direction,
    }


class CustodyOffsetService:

    @staticmethod
    @transaction.atomic
    def offset_custody_to_ledger(
        *,
        customer, store,
        amount_hs: Decimal,
        audit: dict,
        process_no: Optional[str] = None,
        description: str = '',
        write_fx_diff: bool = True,
    ) -> CustodyOffsetResult:
        """Emanet altın → Cari borç mahsuplaşma.

        Args:
            customer, store
            amount_hs: Mahsuplaşılacak Has miktarı (pozitif)
            audit: extract_audit_context çıktısı
            process_no: opsiyonel; otomatik üretilir
            description: opsiyonel açıklama
            write_fx_diff: True (default) ise kur farkı eşiği üstündeyse
                           CustomerLedger'a FX_GAIN/FX_LOSS satırı da yazılır.

        Raises:
            InsufficientCustodyError: emanet yetersiz
            InvalidLedgerStateError: borç yok / parametre hatası
        """
        from django.utils import timezone as dj_tz

        amount_hs = _q_hs(Decimal(amount_hs))
        if amount_hs <= 0:
            raise InvalidLedgerStateError(
                'Mahsuplaşma miktarı pozitif olmalıdır.',
            )

        # ── 1) Emanet bakiye kontrolü ─────────────────────────────
        custody_balance = get_custody_balance_hs(customer)
        if amount_hs > custody_balance:
            raise InsufficientCustodyError(
                available=custody_balance,
                requested=amount_hs,
            )

        # ── 2) Açık borç kontrolü ─────────────────────────────────
        open_balance = LedgerService.get_open_balance_hs(customer)
        if open_balance <= 0:
            raise InvalidLedgerStateError(
                'Müşterinin açık borcu yok; mahsuplaşma yapılamaz.',
            )

        if amount_hs > open_balance:
            raise InvalidLedgerStateError(
                f'Mahsuplaşma miktarı açık borçtan büyük: {amount_hs} HS > '
                f'{open_balance} HS.',
            )

        # ── 3) İşlem no ───────────────────────────────────────────
        if not process_no:
            process_no = f'EMT-{dj_tz.now().strftime("%Y%m%d%H%M%S")}'

        # ── 4) Kur (raporlama için) + Emanet ortalama kuru ────────
        rate = get_current_has_rate(store) or Decimal('0')
        amount_eur = _q_tl(amount_hs * rate)
        custody_avg_rate = _get_custody_avg_rate(customer, store)
        amount_eur_at_custody = _q_tl(amount_hs * custody_avg_rate) if custody_avg_rate > 0 else Decimal('0.00')

        # ── 5) Emanet havuzundan çıkış kaydı ──────────────────────
        # FAZ 26 — BUG-FIX: quantity_gram artık 0.
        # Eski kod `quantity_gram=amount_hs` yazıyordu — Has miktarı
        # gram alanına atanıyordu. Adetli ürünler için anlamsız;
        # gram-bullion için bile yanıltıcı (HS ≠ gram). OFFSET kaydı
        # spesifik bir gramaja değil müşterinin Has bakiyesine
        # yöneliktir; bu yüzden gram alanı 0 olmalı.
        custody_entry = CustomerCustodyLedger.objects.create(
            customer=customer,
            store=store,
            custody_type=CustomerCustodyLedger.CUSTODY_OFFSET,
            amount_hs=amount_hs,
            quantity_gram=Decimal('0'),
            quantity_piece=0,
            exchange_rate_eur=rate,
            amount_eur=amount_eur,
            process_no=process_no,
            description=description or 'Cari ile mahsuplaşma',
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

        # ── 6) Cari ledger karşı kayıt ────────────────────────────
        ledger_entry = LedgerService.write_custody_offset(
            customer=customer,
            store=store,
            amount_hs=amount_hs,
            process_no=process_no,
            audit=audit,
            related_custody=custody_entry,
            description=description or 'Emanet mahsuplaşması',
        )

        # ── 7) Karşılıklı bağlantıyı tamamla ──────────────────────
        custody_entry.related_ledger = ledger_entry
        custody_entry.save(update_fields=['related_ledger'])

        # ── 8) FAZ 22 — A-4: Kur farkı denetim satırı ────────────
        # Emanet bırakıldığı kur ile bugünkü kur arasında fark varsa
        # CustomerLedger'a kur farkı satırı yazılır. Has bazında borç
        # kapatma değişmez; bu satır yalnız TL raporlamasının audit
        # izini tutar.
        fx_entry = None
        fx_diff_tl = Decimal('0.00')
        if write_fx_diff and custody_avg_rate > 0 and rate > 0:
            fx_diff_tl = _q_tl(amount_eur - amount_eur_at_custody)
            if abs(fx_diff_tl) >= _FX_DIFF_MIN_TL:
                fx_type = (
                    CustomerLedger.FX_GAIN if fx_diff_tl > 0
                    else CustomerLedger.FX_LOSS
                )
                fx_desc = (
                    f'Mahsuplaşma kur farkı: emanet ortalama '
                    f'{custody_avg_rate} TL/HS → bugün {rate} TL/HS '
                    f'({amount_hs} HS için {fx_diff_tl} TL)'
                )
                # FX_GAIN/LOSS adjustment olarak yazılır; eşik üstüyse
                # otomatik PENDING (requires_approval=True) olur.
                try:
                    fx_entry = LedgerService.write_adjustment(
                        customer=customer,
                        store=store,
                        transaction_type=fx_type,
                        amount_hs=Decimal('0'),
                        amount_eur=abs(fx_diff_tl),
                        exchange_rate_eur=rate,
                        process_no=process_no,
                        audit=audit,
                        parent=ledger_entry,
                        description=fx_desc,
                        open_balance_eur=_q_tl(open_balance * rate),
                    )
                except InvalidLedgerStateError:
                    # Eşik altı vs. → fx satırı yazılmaz; mahsup tamamlanır
                    fx_entry = None

        # ── 9) Sonuç ──────────────────────────────────────────────
        result = CustodyOffsetResult()
        result.custody_entry = custody_entry
        result.ledger_entry = ledger_entry
        result.fx_entry = fx_entry
        result.fx_diff_tl = fx_diff_tl
        result.custody_avg_rate = custody_avg_rate
        result.current_rate = rate
        result.offset_hs = amount_hs
        result.new_custody_balance_hs = get_custody_balance_hs(customer)
        result.new_ledger_balance_hs = LedgerService.get_open_balance_hs(customer)
        return result

    # ─────────────────────────────────────────────────────────────
    # EMANET HAREKETİ İPTAL (REVERSAL pattern)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def assert_can_reverse_in(original: CustomerCustodyLedger) -> None:
        """FAZ 51 (R-04) — IN reversal öncesi bağımlı işlem taraması.

        Bir CUSTODY_IN kaydını iptal etmeden önce bu IN'e bağlı çocuk
        OUT/OFFSET/STOCK_TRANSFER satırlarının varlığını kontrol eder.
        Çocuk hareket varsa ana kayıt iptal edilemez — önce çocuklar
        REVERSAL yapılmalı. Aksi halde:

          - Stok: Çocuk OUT için yazılan StockLedger çıkışı yetim kalır
          - Cari: Çocuk OFFSET için yazılan CustomerLedger.CUSTODY_OFFSET
                  yetim kalır (manuel reverse gerekir)
          - Bakiye: REVERSAL+OUT+OFFSET aynı IN'e işaret eder ve toplam
                  bakiye negatife düşebilir

        Kullanıcıya hangi işlemlerin önce iptal edilmesi gerektiği net
        biçimde listelenir.

        Args:
            original: CUSTODY_IN tipli kayıt.

        Raises:
            InvalidLedgerStateError: bağlı çocuk hareket varsa.
        """
        if original.custody_type != CustomerCustodyLedger.CUSTODY_IN:
            return

        # delivered_amount_hs property'si parent FK + legacy fallback'i
        # birlikte topluyor (FAZ 24 BUG-3 ile getirildi). 0.0005 HS eşiği
        # küsurat tolerans payı.
        delivered_hs = original.delivered_amount_hs or Decimal('0')
        if delivered_hs <= Decimal('0.0005'):
            return

        # Bağlı aktif OUT/OFFSET/STOCK_TRANSFER kayıtlarını listele
        primary = CustomerCustodyLedger.objects.filter(
            parent=original,
            custody_type__in=(
                CustomerCustodyLedger.CUSTODY_OUT,
                CustomerCustodyLedger.CUSTODY_OFFSET,
                CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER,
            ),
            is_active=True, is_deleted=False,
        )
        legacy = original._legacy_outs_qs()
        children_qs = (primary | legacy).order_by('created_on')

        if not children_qs.exists():
            return

        details = []
        type_label = {
            CustomerCustodyLedger.CUSTODY_OUT: 'Teslim',
            CustomerCustodyLedger.CUSTODY_OFFSET: 'Mahsuplaşma',
            CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER: 'Stoğa Transfer',
        }
        for ch in children_qs[:10]:
            label = type_label.get(ch.custody_type, ch.custody_type)
            try:
                ts = ch.created_on.strftime('%d.%m.%Y %H:%M')
            except Exception:
                ts = '-'
            details.append(
                f'#{ch.pk} {label} {ch.amount_hs} HS ({ts})'
            )

        more = ''
        if children_qs.count() > 10:
            more = f' ve {children_qs.count() - 10} ek kayıt'

        raise InvalidLedgerStateError(
            'Bu emanet kaydı iptal edilemez: bağlı işlemler mevcut. '
            f'Toplam {delivered_hs} HS\'lik {children_qs.count()} aktif '
            f'çocuk kayıt var. Önce şu işlemleri iptal edin: '
            + '; '.join(details) + more
        )

    @staticmethod
    @transaction.atomic
    def reverse_custody_entry(
        *, original: CustomerCustodyLedger, audit: dict, reason: str,
        cascade_children: bool = False,
    ) -> CustomerCustodyLedger:
        """Bir emanet hareketini APPEND-ONLY mantığıyla iptal eder.

        FAZ 22:
          - Önce can_be_reversed() kontrolü yapılır (idempotent).
          - REVERSAL kaydı yazılır.
          - Orijinal kayıt mark_cancelled() ile is_active=False yapılır
            (denetim izi korunur, bakiyeye dahil edilmez).

        FAZ 51 — R-04:
          - CUSTODY_IN için `assert_can_reverse_in` ile bağımlılık taraması.
          - `cascade_children=True` ise (önce backend kararı + UI
            onayıyla) bağlı çocukları da otomatik reverse eder; varsayılan
            False, çağıran tarafa karar bırakır.
        """
        if not reason:
            raise InvalidLedgerStateError(
                'Emanet iptali için neden zorunludur.',
            )

        ok, msg = original.can_be_reversed()
        if not ok:
            raise InvalidLedgerStateError(msg)

        # FAZ 51 (R-04): IN için bağımlılık taraması.
        if original.custody_type == CustomerCustodyLedger.CUSTODY_IN:
            if cascade_children:
                # Önce çocukları reverse et (FIFO sırasıyla).
                primary = CustomerCustodyLedger.objects.filter(
                    parent=original,
                    custody_type__in=(
                        CustomerCustodyLedger.CUSTODY_OUT,
                        CustomerCustodyLedger.CUSTODY_OFFSET,
                        CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER,
                    ),
                    is_active=True, is_deleted=False,
                )
                legacy = original._legacy_outs_qs()
                for ch in (primary | legacy).order_by('created_on'):
                    try:
                        CustodyOffsetService.reverse_custody_entry(
                            original=ch, audit=audit,
                            reason=f'Cascade: {reason}',
                            cascade_children=False,
                        )
                    except InvalidLedgerStateError:
                        # Idempotent — zaten reverse edilmiş çocuğu atla
                        continue
            else:
                CustodyOffsetService.assert_can_reverse_in(original)

        rev = CustomerCustodyLedger.objects.create(
            customer=original.customer,
            store=original.store,
            product=original.product,
            custody_type=CustomerCustodyLedger.CUSTODY_REVERSAL,
            quantity_piece=original.quantity_piece,
            quantity_gram=original.quantity_gram,
            amount_hs=original.amount_hs,
            exchange_rate_eur=original.exchange_rate_eur,
            amount_eur=original.amount_eur,
            process_no=original.process_no,
            parent=original,
            description=f'İPTAL: {reason}',
            reverse_reason=reason[:255],
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

        # FAZ 22: Orijinali pasifleştir (bakiyeden düşmek için).
        original.mark_cancelled(user=audit.get('actor'), reason=reason)

        # OFFSET iptal ediliyorsa karşı CustomerLedger satırını da
        # REVERSAL ile dengele
        if (original.custody_type == CustomerCustodyLedger.CUSTODY_OFFSET
                and original.related_ledger_id):
            try:
                LedgerService.reverse_entry(
                    original=original.related_ledger,
                    audit=audit,
                    reason=f'Emanet mahsuplaşması iptal: {reason}',
                )
            except InvalidLedgerStateError:
                # Ledger tarafı zaten reverse edilmişse sessiz geç
                pass

        return rev
