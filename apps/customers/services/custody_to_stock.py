"""CustodyToStockService — Emanet → Mağaza Serbest Stoğu Transferi.

FAZ 24 — GEREKSİNİM-2:
  Kuyumcu müşteriden emanet aldığı bir ürünü (örn. çeyrek altın)
  serbest stoğa alıp satışta kullanmak istediğinde bu servis çağrılır.

Atomik akış:
  1) CustomerCustodyLedger'a `STOCK` (Stoğa Transfer) kaydı yazılır.
     - parent = orijinal IN kaydı (append-only)
     - quantity_gram / quantity_piece / amount_hs aynı oranlarda
     - exchange_rate_eur + amount_eur bugünkü kurla raporlanır
  2) StockLedger'a `CUSTODY_2_STK` reason ile sıfır net efektli
     denetim kaydı yazılır (önce CUSTODY_OUT, sonra normal IN).
     Net stok değişimi: 0 (zaten girmişti, sadece reason değişiyor).
     Bu adımda `cancel_stock_entry` çağrılmaz; mevcut emanet IN
     kaydının kendisi mantıksal olarak "tüketilmiş" sayılır.
  3) CustomerLedger'a `CREDIT` (Has) yazılır — müşterinin emanet ettiği
     altın artık mağazaya geçtiği için MAĞAZA müşteriye borçlanmıştır
     (müşteri "Alacaklı" konumuna geçer; balance_hs negatife düşer).
     Kuyumcu sonradan bu alacağı kapatmak için müşteriye:
       - başka ürün satar (settlement / mahsup),
       - nakit/döviz öder (settlement),
       - cari mahsuplaşma yapar (CustodyOffsetService),
       - veya emanet geri istenirse REVERSAL.

  FAZ 27 — BUG-FIX (Senaryo 2 / Hata 3A):
     Önceki sürümde 3. adımda yanlışlıkla `write_debt` çağrılıyordu;
     bu, müşterinin mağazaya borçlu görünmesine yol açıyordu (yön ters).
     Mağaza müşterinin malını sahiplendiğinde müşteri ALACAKLI olur,
     dolayısıyla `CREDIT` yazılmalı. Append-only mimari etkilenmez:
     CustomerCustodyLedger ve StockLedger akışı aynı kalır.

Not: Bu servis kasayı veya başka altyapıyı değiştirmez; yalnız
emanet ↔ stok ↔ cari arasındaki muhasebesel köprüyü kurar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.custody.models import CustomerCustodyLedger
from apps.customers.models import CustomerLedger
from apps.customers.services.ledger import LedgerService
from apps.customers.services.exceptions import (
    InvalidLedgerStateError,
    InsufficientCustodyError,
)
from apps.banking.exchange_rate_service import get_current_has_rate
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockLedger


_HS_QUANT = Decimal('0.001')
_TL_QUANT = Decimal('0.01')


def _q_hs(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_HS_QUANT, rounding=ROUND_HALF_UP)


def _q_tl(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_TL_QUANT, rounding=ROUND_HALF_UP)


@dataclass
class CustodyToStockResult:
    custody_entry: CustomerCustodyLedger = None
    ledger_entry: CustomerLedger = None
    transferred_hs: Decimal = Decimal('0')
    transferred_gram: Decimal = Decimal('0')
    transferred_piece: int = 0


class CustodyToStockService:

    @staticmethod
    @transaction.atomic
    def transfer(
        *,
        custody_in: CustomerCustodyLedger,
        quantity_gram: Optional[Decimal] = None,
        quantity_piece: Optional[int] = None,
        audit: dict,
        description: str = '',
        write_customer_debt: bool = True,
    ) -> CustodyToStockResult:
        """Emanet IN kaydını (kısmen veya tamamen) serbest stoğa al.

        Args:
            custody_in: Aktif `CUSTODY_IN` satırı.
            quantity_gram / quantity_piece: Transfer edilecek miktar.
                Boş bırakılırsa kalan miktarın tamamı transfer edilir.
            audit: extract_audit_context çıktısı.
            description: opsiyonel açıklama.
            write_customer_debt: True (default) ise müşterinin cari hesabına
                Has bazlı CREDIT yazılır (mağaza müşteriye borçlanır;
                müşteri alacaklı konumuna geçer). False ise yalnız
                stok+custody kaydı yazılır (özel akışlar için).
                NOT: Parametre adı geriye uyum için "debt" kalıyor;
                fakat semantik olarak müşteri alacağı yazılır.

        Raises:
            InvalidLedgerStateError, InsufficientCustodyError
        """
        if custody_in is None:
            raise InvalidLedgerStateError('Emanet kaydı bulunamadı.')
        if custody_in.is_deleted or not custody_in.is_active:
            raise InvalidLedgerStateError(
                'İptal/silinmiş emanet kaydı transfer edilemez.',
            )
        if custody_in.custody_type != CustomerCustodyLedger.CUSTODY_IN:
            raise InvalidLedgerStateError(
                'Sadece IN tipli emanet kaydı stoğa transfer edilebilir.',
            )

        is_gram_based = custody_in.quantity_gram > 0
        remaining_gram = custody_in.remaining_quantity_gram
        remaining_piece = custody_in.remaining_quantity_piece
        remaining_hs = custody_in.remaining_amount_hs

        if remaining_hs <= Decimal('0.0005'):
            raise InsufficientCustodyError(
                available=remaining_hs,
                requested=Decimal('0'),
            )

        # Miktar belirleme
        if is_gram_based:
            req_gram = (
                _q_hs(Decimal(quantity_gram))
                if quantity_gram not in (None, '', 0)
                else remaining_gram
            )
            if req_gram <= 0:
                raise InvalidLedgerStateError('Transfer miktarı pozitif olmalı.')
            if req_gram > remaining_gram + Decimal('0.0005'):
                raise InsufficientCustodyError(
                    available=remaining_gram,
                    requested=req_gram,
                )
            ratio = req_gram / custody_in.quantity_gram
            req_piece = int(custody_in.quantity_piece * float(ratio))
        else:
            req_piece = (
                int(quantity_piece)
                if quantity_piece not in (None, '', 0)
                else remaining_piece
            )
            if req_piece <= 0:
                raise InvalidLedgerStateError('Transfer miktarı pozitif olmalı.')
            if req_piece > remaining_piece:
                raise InsufficientCustodyError(
                    available=Decimal(remaining_piece),
                    requested=Decimal(req_piece),
                )
            ratio = Decimal(req_piece) / Decimal(custody_in.quantity_piece)
            req_gram = (custody_in.quantity_gram * ratio).quantize(_HS_QUANT)

        transferred_hs = (custody_in.amount_hs * ratio).quantize(_HS_QUANT)

        # Anlık kur (raporlama)
        current_rate = get_current_has_rate(custody_in.store) or Decimal('0')
        amount_eur = _q_tl(transferred_hs * current_rate) if current_rate > 0 else Decimal('0.00')

        process_no = (
            custody_in.process_no
            or f'STK-{timezone.now().strftime("%Y%m%d%H%M%S")}'
        )

        # ── 1) CustomerCustodyLedger: STOCK kaydı ────────────────
        stk_entry = CustomerCustodyLedger.objects.create(
            customer=custody_in.customer,
            store=custody_in.store,
            product=custody_in.product,
            custody_type=CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER,
            quantity_piece=req_piece,
            quantity_gram=req_gram,
            amount_hs=transferred_hs,
            exchange_rate_eur=current_rate,
            amount_eur=amount_eur,
            process_no=process_no,
            parent=custody_in,
            description=(description or 'Emanetten Serbest Stoğa Transfer')[:255],
            is_returned=True,
            is_active=True, is_deleted=False,
            created_by=audit.get('actor'),
            received_by=custody_in.received_by,
            delivered_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )

        # ── 2) StockLedger: CUSTODY_2_STK denetim kaydı ──────────
        # Net etki sıfır olacak şekilde önce çıkış (emanet havuzu),
        # sonra giriş (serbest havuz). Stok sayımı değişmez.
        if custody_in.product:
            unit_cost_hs = (
                custody_in.product.buy_price_hs
                or (Decimal(str(custody_in.product.product_mileage or '0')) / Decimal('1000'))
            )
            unit_cost_eur = (
                (unit_cost_hs * current_rate).quantize(_TL_QUANT)
                if current_rate > 0 else Decimal('0.00')
            )
            try:
                StockService.record_exit(
                    product=custody_in.product, store=custody_in.store,
                    quantity_gram=req_gram, quantity_pieces=req_piece,
                    reason=StockLedger.Reason.CUSTODY_TO_STOCK,
                    ref_type='custody_to_stock_out',
                    ref_id=str(stk_entry.id),
                    user=audit.get('actor'),
                    notes=f'Emanet havuzundan çıkış (transfer) — Müşteri {custody_in.customer_id}',
                )
                StockService.record_entry(
                    product=custody_in.product, store=custody_in.store,
                    quantity_gram=req_gram, quantity_pieces=req_piece,
                    reason=StockLedger.Reason.CUSTODY_TO_STOCK,
                    ref_type='custody_to_stock_in',
                    ref_id=str(stk_entry.id),
                    unit_cost_hs=unit_cost_hs,
                    unit_cost_eur=unit_cost_eur,
                    hs_rate_eur=current_rate,
                    user=audit.get('actor'),
                    notes='Serbest stoğa giriş (transfer)',
                )
            except Exception:
                # StockService hata fırlatırsa atomic blok geri sarılır.
                raise

            # Defense-in-depth: Stok artık mağazaya ait olduğu için
            # ana Hurdalar/Bilezikler sayfalarında görünmesi gerekir.
            # add_custody zaten Scraps/Bracelets satırını oluşturuyor; ancak
            # eski/farklı yollarla açılmış havuzlar için son bir güvenlik ağı
            # olarak burada da get_or_create + soft-delete revival uygulanır.
            _p = custody_in.product
            _target_model = None
            if getattr(_p, 'is_scrap', False):
                from apps.scraps.models import Scraps
                _target_model = Scraps
            else:
                _cat_name = ''
                if getattr(_p, 'category_id', None):
                    _cat_name = (getattr(_p.category, 'name', '') or '').lower()
                if 'bilezik' in _cat_name:
                    from apps.bracelets.models import Bracelets
                    _target_model = Bracelets

            if _target_model is not None:
                _row = _target_model.objects.filter(
                    product=_p, store=custody_in.store,
                ).first()
                if _row:
                    _f = []
                    if _row.is_deleted:
                        _row.is_deleted = False
                        _f.append('is_deleted')
                    if _row.is_active is False:
                        _row.is_active = True
                        _f.append('is_active')
                    if _f:
                        _row.save(update_fields=_f)
                else:
                    _target_model.objects.create(
                        product=_p, store=custody_in.store,
                        created_by=audit.get('actor'),
                    )

            if _p.is_active is False or _p.is_deleted is True:
                from apps.products.models import Products as _Products
                _Products.objects.filter(id=_p.id).update(
                    is_active=True, is_deleted=False,
                )

        # ── 3) CustomerLedger: CREDIT (Has) ──────────────────────
        # FAZ 27: Yön düzeltmesi — emanet stoğa alındığında mağaza
        # müşteriye borçlanır (müşteri "alacaklı"). Bu nedenle
        # write_debt yerine write_credit çağrılır. balance_hs negatife
        # düşer; customer.payable_hs_computed bu işlemi yansıtır.
        ledger_entry = None
        if write_customer_debt:
            ledger_entry = LedgerService.write_credit(
                customer=custody_in.customer,
                store=custody_in.store,
                amount_hs=transferred_hs,
                process_no=process_no,
                audit=audit,
                description=(
                    description
                    or f'Emanet stoğa alındı (müşteri alacağı): '
                       f'{custody_in.product.name if custody_in.product else "-"} '
                       f'({req_gram} gr / {transferred_hs} HS)'
                )[:255],
            )
            stk_entry.related_ledger = ledger_entry
            stk_entry.save(update_fields=['related_ledger'])

        # ── 4) Sonuç ─────────────────────────────────────────────
        result = CustodyToStockResult()
        result.custody_entry = stk_entry
        result.ledger_entry = ledger_entry
        result.transferred_hs = transferred_hs
        result.transferred_gram = req_gram
        result.transferred_piece = req_piece
        return result

    # ─────────────────────────────────────────────────────────────
    # FAZ 51 — R-07: ATOMİK GERİ ALMA (TRANSFER REVERSE)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    @transaction.atomic
    def reverse_transfer(
        *,
        transfer_entry: CustomerCustodyLedger,
        audit: dict,
        reason: str,
    ) -> dict:
        """Bir Emanet→Stok transfer kaydını atomik olarak geri alır.

        Üç tabloyu birden simetrik biçimde günceller:

          1) CustomerCustodyLedger:
             - transfer satırı (CUSTODY_STOCK_TRANSFER) için CCL.REVERSAL
               yazılır + mark_cancelled (is_active=False).
             - Bu sayede `delivered_amount_hs` / `remaining_amount_hs`
               hesaplarında düşmüş sayılır → emanet bakiyesi geri artar.

          2) StockLedger:
             - Transfer sırasında `record_exit (CUSTODY_TO_STOCK)` ve
               `record_entry (CUSTODY_TO_STOCK)` çiftleri yazılmıştı.
             - `cancel_stock_entry(ref_type='custody_to_stock_in', ref_id=...)`
               ve `cancel_stock_entry(ref_type='custody_to_stock_out', ...)`
               ile her iki bacak da reverse edilir.

          3) CustomerLedger:
             - `LedgerService.reverse_entry(transfer_entry.related_ledger)`
               ile CREDIT satırı reverse edilir → müşterinin alacağı geri
               iptal olur. propagate_reversal_side_effects burada kasa
               hareketi tetiklemez (CREDIT akışı kasa üretmemişti).

        Hata anında `@transaction.atomic` tüm değişiklikleri geri alır.

        Args:
            transfer_entry: CCL.CUSTODY_STOCK_TRANSFER satırı.
            audit, reason: standart audit + neden.

        Raises:
            InvalidLedgerStateError: yanlış tip ya da zaten reverse edilmiş.

        Returns:
            dict: {custody_reversal_id, ledger_reversal_id, stock_reverted}.
        """
        from apps.stock_management.services.cancel_service import (
            cancel_stock_entry,
        )

        if transfer_entry is None:
            raise InvalidLedgerStateError('Transfer kaydı bulunamadı.')
        if transfer_entry.custody_type != CustomerCustodyLedger.CUSTODY_STOCK_TRANSFER:
            raise InvalidLedgerStateError(
                'Sadece STOCK_TRANSFER kayıtları geri alınabilir.',
            )
        if not reason:
            raise InvalidLedgerStateError(
                'Transfer geri alma nedeni zorunludur.',
            )

        ok, msg = transfer_entry.can_be_reversed()
        if not ok:
            raise InvalidLedgerStateError(msg)

        # ── 1) CCL.REVERSAL — emanet bakiyesi geri artar ──────────
        rev = CustomerCustodyLedger.objects.create(
            customer=transfer_entry.customer,
            store=transfer_entry.store,
            product=transfer_entry.product,
            custody_type=CustomerCustodyLedger.CUSTODY_REVERSAL,
            quantity_piece=transfer_entry.quantity_piece,
            quantity_gram=transfer_entry.quantity_gram,
            amount_hs=transfer_entry.amount_hs,
            exchange_rate_eur=transfer_entry.exchange_rate_eur,
            amount_eur=transfer_entry.amount_eur,
            process_no=transfer_entry.process_no,
            parent=transfer_entry,
            description=f'İPTAL (Stoğa Transfer geri al): {reason}'[:255],
            reverse_reason=reason[:255],
            created_by=audit.get('actor'),
            ip_address=audit.get('ip_address'),
            user_agent=audit.get('user_agent') or '',
        )
        transfer_entry.mark_cancelled(
            user=audit.get('actor'), reason=reason,
        )

        # ── 2) StockLedger çift reverse (in + out) ────────────────
        stock_reverted = False
        try:
            cancel_stock_entry(
                ref_type='custody_to_stock_in',
                ref_id=str(transfer_entry.id),
                user=audit.get('actor'),
                reverse_supplier_ledger=False,
                notes=f'CustodyToStock REVERSE — {reason}',
                raise_if_not_found=False,
            )
            cancel_stock_entry(
                ref_type='custody_to_stock_out',
                ref_id=str(transfer_entry.id),
                user=audit.get('actor'),
                reverse_supplier_ledger=False,
                notes=f'CustodyToStock REVERSE — {reason}',
                raise_if_not_found=False,
            )
            stock_reverted = True
        except Exception:
            # Atomic blok içinde — exception bubble up edip rollback'e
            # gitmesi gerekir. Burada yakalama yapmıyoruz.
            raise

        # ── 3) CustomerLedger karşı reverse ───────────────────────
        ledger_rev_id = None
        if transfer_entry.related_ledger_id:
            try:
                ledger_rev = LedgerService.reverse_entry(
                    original=transfer_entry.related_ledger,
                    audit=audit,
                    reason=f'Stoğa transfer iptal: {reason}',
                )
                ledger_rev_id = str(ledger_rev.id)
            except InvalidLedgerStateError:
                # Zaten reverse edilmiş — sessiz geç (idempotent)
                pass

        return {
            'custody_reversal_id': str(rev.id),
            'ledger_reversal_id': ledger_rev_id,
            'stock_reverted': stock_reverted,
        }
