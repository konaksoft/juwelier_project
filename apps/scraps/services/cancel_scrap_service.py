"""
cancel_scrap_service.py — Merkezi Hurda Alım İptali Servisi
================================================================================

Bu modül, hurda (scrap) alım işlemlerinin iptalini tek bir noktadan yönetir.

SORUN (önceki durum):
    3 farklı UI noktasında iptal akışı birbirinden bağımsız kodla yürütülüyordu:
      1. Hurda Index  → scraps/views.py:delete() → _cancel_single_process()
         (cancel_stock_entry reverse_supplier_ledger=has_supplier + process_no ref)
      2. Havuz Detay  → scraps/views.py:pool_bulk_cancel() → _cancel_single_process()
         (aynı yukarıdaki + outer transaction.atomic eksikti)
      3. İşlemler     → process/operations.py:cancel_row() → _revert_process_stock()
         (cancel_stock_entry reverse_supplier_ledger=FALSE → sonra AYRI manuel
          SupplierLedger.filter(source_process_id=...).update(is_active=False))

    Bu farklılıklar:
      - Atomicity kaybına (pool_bulk_cancel dış döngüde atomic değildi)
      - SupplierLedger güncellenmemesine (cancel_row hurda için SL'yi
        ayrı ve koşullu kapatıyordu; bazı legacy durumlar kaçıyordu)
      - Kod tekrarına ve bakım yüküne yol açıyordu.

ÇÖZÜM:
    cancel_scrap_purchase(process_id, user) → tek merkezi servis.
    3 endpoint de bu fonksiyonu çağırır.

PRENSİPLER:
  1. ATOMIC   : @transaction.atomic — stok + cari + process status tek işlem.
  2. APPEND-ONLY : .delete() yok; Process/SupplierLedger soft-disable,
                  StockLedger yeni reversal satırı ile kapatılır.
  3. REF FALLBACK: 'scrap_add'|'process' × process_no|str(id) kombinasyonu
                  ile hem legacy hem yeni kayıtlar yakalanır.
  4. SUPPLIER SSOT: cancel_stock_entry (process_no bazlı) +
                    source_process_id bazlı ek cleanup → tek satır bile kaçmaz.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest

logger = logging.getLogger('scraps')


# ============================================================================
# ÖZEL HATA SINIFLARI
# ============================================================================

class CancelNotAllowedError(Exception):
    """İptal güvenlik kontrolü başarısız — işlem iptal edilemez."""
    pass


class CancelScrapError(Exception):
    """Hurda iptal servisi genel hatası."""
    pass


# ============================================================================
# MERKEZI HURDA ALIM İPTALİ
# ============================================================================

@transaction.atomic
def cancel_scrap_purchase(
    *,
    process_id,
    user,
    skip_recalculate: bool = False,
    notes: str = '',
) -> dict:
    """
    Tek bir hurda alım Process kaydını tam atomik olarak iptal eder.

    Sıra:
      1. Process.select_for_update() → güvenlik kontrolü (çift-iptal koruması)
      2. StockLedger reversal: cancel_stock_entry (ref_type + ref_id fallback)
      3. SupplierLedger soft-disable: process_no bazlı (cancel_stock_entry içi)
         + source_process_id bazlı ek cleanup (cancel_row kalıntısı fix)
      4. Process.is_status='CANCELED', is_deleted=True
      5. Products.gram legacy alanı senkronizasyonu (Greatest(..., 0) guard)
      6. WAC / milyem yeniden hesaplama (skip_recalculate=True → N+1 önlenir)

    Args:
        process_id : Process.id (UUID veya str)
        user       : İptali yapan kullanıcı (audit / log)
        skip_recalculate : True ise WAC/milyem recalculate atlanır;
                           toplu iptal döngüsü (pool_bulk_cancel) bunu
                           döngü sonunda tek seferlik çağırır.
        notes      : StockLedger reversal notuna eklenir.

    Returns:
        dict: {
            'process_no': str,
            'gram'      : str,   # iptal edilen gram miktarı
            'stock_reversed': int,   # reversal yazılan StockLedger satır sayısı
            'sl_deactivated': int,   # pasifleştirilen SupplierLedger satır sayısı
        }

    Raises:
        Process.DoesNotExist  : process_id geçersiz
        CancelNotAllowedError : zaten iptal / soft-deleted
        CancelScrapError      : genel servis hatası
    """
    # Lazy import — circular import riski (cancel_service ↔ scraps/views)
    from apps.process.models import Process
    from apps.products.models import Products
    from apps.suppliers.models import SupplierLedger
    from apps.stock_management.services.cancel_service import cancel_stock_entry

    # ── 1. Process kilitle ve kontrol et ─────────────────────────────────
    proc = Process.objects.select_for_update().get(id=process_id)

    if proc.is_deleted or str(getattr(proc, 'is_status', '') or '').upper() == 'CANCELED':
        raise CancelNotAllowedError(
            f"Process zaten iptal edilmiş: {proc.process_no or proc.id}"
        )

    if (proc.transaction_type or '').upper() not in ('PURCHASE',):
        raise CancelNotAllowedError(
            f"Bu servis yalnızca PURCHASE tipi işlemleri iptal eder "
            f"(tip={proc.transaction_type})."
        )

    product = proc.product
    store = proc.store
    has_supplier = bool(proc.supplier_id)
    gram = Decimal(str(proc.gram or 0))
    _note = notes or f"Hurda alım iptali: {getattr(product, 'name', proc.id)}"

    # ── 2. StockLedger reversal + SupplierLedger (process_no bazlı) ──────
    #
    # Fallback zinciri — hem legacy scrap_add hem yeni process ref_type,
    # hem str(id) hem process_no ref_id desteği:
    #
    #   'scrap_add' + process_no  → FAZ 4 öncesi eski hurda girişleri
    #   'scrap_add' + str(id)     → bazı perakende hurda girişleri
    #   'process'   + str(id)     → yeni perakende / toptan girişleri
    #   'process'   + process_no  → bazı toptan-hurda girişleri
    #
    _ref_id_candidates = []
    if proc.process_no:
        _ref_id_candidates.append(proc.process_no)
    if str(proc.id) not in _ref_id_candidates:
        _ref_id_candidates.append(str(proc.id))

    total_stock_reversed = 0
    total_sl_deactivated = 0
    _stock_hit = False

    # FAZ 53 — cancel_bracelet_purchase ile simetrik düzeltme:
    #   Eski mantık: `_hit = True` `_sc > 0 or _sl > 0` ile set ediliyordu;
    #   cancel_stock_entry SL'yi process_no üzerinden bulup pasifleştirince
    #   `_sl > 0` → erken break → doğru ref_type/ref_id kombinasyonu hiç
    #   denenmiyordu → stok geri sarılmıyor.
    #   Yeni mantık: `_stock_hit` yalnızca gerçek StockLedger reversal'ında set;
    #   SL pasifleştirmesi adım 3'te FAZ 51 helper'ı ile yapılır.
    for _ref_type in ('scrap_add', 'process'):
        if _stock_hit:
            break
        for _ref_id in _ref_id_candidates:
            try:
                _result = cancel_stock_entry(
                    ref_type=_ref_type,
                    ref_id=_ref_id,
                    user=user,
                    # FAZ 53: SL pasifleştirmesi yalnızca adım 3'te yapılır.
                    reverse_supplier_ledger=False,
                    notes=_note,
                    raise_if_not_found=False,
                )
            except Exception as _err:
                logger.error(
                    "cancel_scrap_purchase: cancel_stock_entry hata "
                    "(proc=%s, ref_type=%s, ref_id=%s): %s",
                    proc.id, _ref_type, _ref_id, _err,
                )
                raise CancelScrapError(str(_err)) from _err

            _sc = _result.get('cancelled_stock_count', 0)
            total_stock_reversed += _sc

            if _sc > 0:
                _stock_hit = True
                break

    # FAZ 53 — Safety guard: COMPLETED + gram > 0 + not waiting_stock + supplier
    # varsa stok reversal'ı zorunlu. Aksi halde silent fail elimine.
    _was_completed = (str(getattr(proc, 'is_status', '') or '').upper()
                      == 'COMPLETED')
    _waiting = bool(getattr(proc, 'waiting_stock', False))
    if (total_stock_reversed == 0 and _was_completed and gram > 0
            and not _waiting):
        logger.error(
            "cancel_scrap_purchase: STOK GERI SARILAMADI (silent fail "
            "eliminasyon) proc=%s, proc_no=%s, gram=%s — denenen kombinasyonlar "
            "ref_type ∈ ('scrap_add','process') × ref_id ∈ %s.",
            proc.id, proc.process_no, gram, _ref_id_candidates,
        )
        raise CancelScrapError(
            f"Stok geri sarılamadı: hurda PURCHASE iptali için "
            f"StockLedger eşleşmesi bulunamadı "
            f"(process_no={proc.process_no or proc.id}). "
            f"İşlem iptal edilmedi; veri durumu kontrol edin."
        )

    if total_stock_reversed == 0:
        logger.warning(
            "cancel_scrap_purchase: hiçbir StockLedger kaydı "
            "bulunamadı (proc=%s, proc_no=%s) — taslak veya tekrar iptal.",
            proc.id, proc.process_no,
        )

    # ── 3. SupplierLedger — source_process_id bazlı ek cleanup ───────────
    #
    # cancel_row'un eski manuel pasifleştirmesi process_no yerine
    # source_process_id (FAZ 21 sonrası satır-bazlı anahtar) kullanıyordu.
    # cancel_stock_entry process_no bazlı taradığı için bu satırları
    # kaçırabilir. Burada ek bir UPDATE ile gap kapatılır.
    #
    # FAZ 51 (R-02): Append-only REVERSAL helper'ına yönlendirildi. Helper
    # orijinali pasifleştirir + reversed_by/at/reason audit alanlarını
    # doldurur + REVERSAL satırı (is_active=False) yazar. Bakiye davranışı
    # değişmez.
    if has_supplier:
        try:
            from apps.suppliers.services import (
                reverse_supplier_ledger_for_process,
            )
            _audit = {'actor': user, 'ip_address': None, 'user_agent': ''}
            _res = reverse_supplier_ledger_for_process(
                audit=_audit,
                reason=f'Hurda alımı iptal — {proc.process_no or proc.id}',
                process_id=proc.id,
            )
            _extra_sl = _res.get('reversed_count', 0)
            total_sl_deactivated += _extra_sl
            if _extra_sl:
                logger.info(
                    "cancel_scrap_purchase: source_process_id=%s ile %d ek "
                    "SupplierLedger satırı REVERSAL yazıldı.",
                    proc.id, _extra_sl,
                )
        except Exception as _sl_err:
            # Yardımcı bir başarısızlığı ana iptal akışını bozmamalı;
            # eski davranışa düş (mass mutation) — atomic blok içinde.
            logger.exception(
                "cancel_scrap_purchase: SupplierLedger REVERSAL helper "
                "başarısız, legacy fallback uygulanıyor (proc=%s).",
                proc.id,
            )
            _extra_sl = SupplierLedger.objects.filter(
                source_process_id=proc.id, is_active=True,
            ).update(is_active=False)
            total_sl_deactivated += _extra_sl

    # ── 4. Process soft-disable ───────────────────────────────────────────
    proc.is_status = 'CANCELED'
    proc.is_deleted = True
    proc.save(update_fields=['is_status', 'is_deleted'])

    # ── 5. Products.gram legacy alanı sync (negatife düşmez) ─────────────
    if gram > 0 and product is not None:
        try:
            Products.objects.filter(id=product.id).update(
                gram=Greatest(F('gram') - gram, Decimal('0'))
            )
        except Exception as _gram_err:
            logger.error(
                "cancel_scrap_purchase: Products.gram sync hata "
                "(product=%s, proc=%s): %s",
                getattr(product, 'id', None), proc.id, _gram_err,
            )

    # ── 6. WAC / milyem recalculate ───────────────────────────────────────
    if not skip_recalculate and product is not None and store is not None:
        try:
            # Lazy import: recalculate fonksiyonu scraps/views.py içinde;
            # doğrudan import döngüsel bağımlılık yaratır.
            from apps.scraps.views import recalculate_scrap_pool_mileage_after_cancel
            recalculate_scrap_pool_mileage_after_cancel(product, store)
        except Exception as _wac_err:
            logger.error(
                "cancel_scrap_purchase: WAC recalculate hata "
                "(product=%s, proc=%s): %s",
                getattr(product, 'id', None), proc.id, _wac_err,
            )

    logger.info(
        "cancel_scrap_purchase TAMAMLANDI: proc=%s, proc_no=%s, "
        "stock_reversed=%d, sl_deactivated=%d, gram=%s",
        proc.id, proc.process_no,
        total_stock_reversed, total_sl_deactivated, gram,
    )

    return {
        'process_no': proc.process_no,
        'gram': str(gram),
        'stock_reversed': total_stock_reversed,
        'sl_deactivated': total_sl_deactivated,
    }
