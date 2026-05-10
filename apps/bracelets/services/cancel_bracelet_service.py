"""
cancel_bracelet_service.py — Merkezi Bilezik Alım İptali Servisi
================================================================================

Hurda SSOT (apps/scraps/services/cancel_scrap_service.py) ile birebir simetrik.

SORUN (önceki durum):
    Bilezik PURCHASE iptali 3 farklı UI noktasından farklı kod yollarına gidiyordu:
      1. Bilezik Index  → bracelets/views.py:delete() → _cancel_bracelet_process()
      2. Havuz Detay    → bracelets/views.py:pool_bulk_cancel() → _cancel_bracelet_process()
         (loop'ta except yakalama atomic decorator ile çakışıyor → TransactionManagementError riski)
      3. İşlemler       → process/operations.py:cancel_row() → _revert_process_stock()
         (reverse_supplier_ledger=False + ayrı manuel SupplierLedger.update)

    Bu farklılıklar SupplierLedger pasifleştirme tutarsızlığına ve
    pool_bulk_cancel'da yarı-iptal riskine yol açıyordu.

ÇÖZÜM:
    cancel_bracelet_purchase(process_id, user) → tek merkezi servis.
    3 endpoint de bu fonksiyonu çağırır.

PRENSİPLER (Hurda SSOT ile aynı):
  1. ATOMIC      : @transaction.atomic — stok + cari + process status tek işlem.
  2. APPEND-ONLY : .delete() yok; soft-disable + StockLedger reversal.
  3. REF FALLBACK: 'bracelet_add'|'process' × process_no|str(id).
  4. SUPPLIER SSOT: yalnızca FAZ 51 reverse_supplier_ledger_for_process
                   (source_process_id bazlı audit-shadow REVERSAL).
                   FAZ 53 öncesinde cancel_stock_entry içi process_no-bazlı
                   SL pasifleştirmesi de vardı → ilk iterasyonda SL bulunup
                   _hit=True set ediliyor + döngü erken kırılıyordu →
                   stok geri sarılmıyordu (canlı bug 2026-05-05). Artık SL
                   pasifleştirmesi yalnızca adım 3'te.

FAZ 53 SAFETY GUARD:
  is_bracelet_product() yalnızca kategori adı 'bilezik' içerirse True döner.
  is_gram_bullion=True (default=True!) ile sarrafiye/ziynet ürünleri yanlışlıkla
  bu servise yönlenmesin diye. Ek olarak total_stock_reversed==0 + COMPLETED
  + gram>0 + not waiting_stock durumunda CancelBraceletError raise edilir.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest

logger = logging.getLogger('bracelets')


# ============================================================================
# ÖZEL HATA SINIFLARI
# ============================================================================

class CancelNotAllowedError(Exception):
    """İptal güvenlik kontrolü başarısız — işlem iptal edilemez."""
    pass


class CancelBraceletError(Exception):
    """Bilezik iptal servisi genel hatası."""
    pass


# ============================================================================
# YARDIMCI: Bilezik tespiti
# ============================================================================

def is_bracelet_product(product) -> bool:
    """
    Bir ürünün bilezik havuzuna ait olup olmadığını döndürür.

    Tespit kriteri:
      - Yalnızca `product.category.name` 'bilezik' içeriyorsa True.

    FAZ 53 — KRITIK MIMARI DUZELTMESI:
        Önceki sürüm `Products.is_gram_bullion=True` kontrolünü kapsama alıyordu.
        Ancak bu alan model'de `default=True` tanımlı; sistemde her ürünün
        varsayılan değeri True. Sarrafiye/Döviz/Ziynet ürünleri (örn.
        "22 ayar gram") da is_gram_bullion=True ile oluşuyordu → bu fonksiyon
        ziynet ürünleri de "bilezik" olarak işaretliyordu → cancel_row routing'i
        ziynet PURCHASE iptalini cancel_bracelet_purchase'a yönlendiriyordu →
        kod yolu uyumsuzluğu nedeniyle stok geri sarılmıyordu (canlı bug).

        Doğru ayraç: Categories tablosunda 'Ziynet'/'Döviz'/'Hurda'/'Bilezik'
        adlı 4 ayrı kategori var; gerçek bilezik ürünleri yalnızca 'Bilezik'
        kategorisine bağlı. Her havuz (Sarrafiye, Bilezik, Hurda) ayrı stok
        kimliği taşır; isim çakışması olsa bile birleştirilmez.
    """
    if product is None:
        return False
    try:
        cat_name = (product.category.name if product.category else '') or ''
        if 'bilezik' in cat_name.lower():
            return True
    except Exception:
        pass
    return False


# ============================================================================
# MERKEZI BİLEZİK ALIM İPTALİ
# ============================================================================

@transaction.atomic
def cancel_bracelet_purchase(
    *,
    process_id,
    user,
    skip_recalculate: bool = False,
    notes: str = '',
) -> dict:
    """
    Tek bir bilezik alım Process kaydını tam atomik olarak iptal eder.

    Sıra:
      1. Process.select_for_update() → güvenlik kontrolü (çift-iptal koruması)
      2. StockLedger reversal: cancel_stock_entry (ref_type + ref_id fallback,
         FAZ 53: reverse_supplier_ledger=False — SL adım 3'e ertelenir)
      3. SupplierLedger REVERSAL: source_process_id bazlı FAZ 51 helper
         (audit-shadow). cancel_stock_entry içi process_no-bazlı SL artık
         kullanılmıyor (early-break bug'ına neden oluyordu).
      4. Process.is_status='CANCELED', is_deleted=True
      5. Products.gram legacy alanı senkronizasyonu (Greatest(..., 0) guard)
      6. WAC / milyem yeniden hesaplama (skip_recalculate=True → N+1 önlenir)

    FAZ 53 silent-fail guard:
        total_stock_reversed == 0 + Process.is_status=='COMPLETED' + gram > 0
        + not waiting_stock → CancelBraceletError raise edilir; transaction
        rollback olur. Eski silent fail davranışı tamamen elimine.

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
            'gram'      : str,
            'stock_reversed': int,
            'sl_deactivated': int,
        }

    Raises:
        Process.DoesNotExist  : process_id geçersiz
        CancelNotAllowedError : zaten iptal / soft-deleted
        CancelBraceletError   : genel servis hatası
    """
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
    _note = notes or f"Bilezik alım iptali: {getattr(product, 'name', proc.id)}"

    # ── 2. StockLedger reversal + SupplierLedger (process_no bazlı) ──────
    #
    # Fallback zinciri:
    #   'bracelet_add' + process_no → perakende havuz girişleri (legacy)
    #   'bracelet_add' + str(id)    → bazı perakende girişleri
    #   'process'      + str(id)    → toptan / yeni perakende
    #   'process'      + process_no → bazı toptan girişleri
    #
    _ref_id_candidates = []
    if proc.process_no:
        _ref_id_candidates.append(proc.process_no)
    if str(proc.id) not in _ref_id_candidates:
        _ref_id_candidates.append(str(proc.id))

    total_stock_reversed = 0
    total_sl_deactivated = 0
    _stock_hit = False

    # FAZ 53 — KRITIK DUZELTME:
    #   Eski mantık: `_hit = True` `_sc > 0 or _sl > 0` ile set ediliyordu.
    #   cancel_stock_entry ilk iterasyonda (`bracelet_add`, process_no)
    #   StockLedger eşleşmesi bulamasa bile SupplierLedger'ı process_no ile
    #   bulup soft-disable ediyor (deactivated_supplier_ledgers=1) → `_hit=True`
    #   → döngü kırılıyor → doğru kombinasyon (`process`, str(uuid)) hiç
    #   denenmiyor → stok geri sarılmıyor.
    #
    #   Yeni mantık: `_stock_hit` yalnızca gerçek StockLedger reversal
    #   olduğunda set edilir. `_sl` bağımsız akümüle edilir; SL bulunması tek
    #   başına döngüyü kırmaz. Doğru (ref_type, ref_id) bulunana kadar tüm
    #   kombinasyonlar denenir; sonunda `total_stock_reversed == 0` + COMPLETED
    #   + gram > 0 ise CancelBraceletError raise edilir (silent fail eliminasyon).
    for _ref_type in ('bracelet_add', 'process'):
        if _stock_hit:
            break
        for _ref_id in _ref_id_candidates:
            try:
                _result = cancel_stock_entry(
                    ref_type=_ref_type,
                    ref_id=_ref_id,
                    user=user,
                    # FAZ 53: SL pasifleştirmesi sadece STOK eşleşen iterasyonda
                    # yapılsın; aksi halde 1. iterasyon SL'yi process_no üzerinden
                    # bulup pasifleştiriyor ve döngü erken bitiyor.
                    reverse_supplier_ledger=False,
                    notes=_note,
                    raise_if_not_found=False,
                )
            except Exception as _err:
                logger.error(
                    "cancel_bracelet_purchase: cancel_stock_entry hata "
                    "(proc=%s, ref_type=%s, ref_id=%s): %s",
                    proc.id, _ref_type, _ref_id, _err,
                )
                raise CancelBraceletError(str(_err)) from _err

            _sc = _result.get('cancelled_stock_count', 0)
            total_stock_reversed += _sc

            if _sc > 0:
                _stock_hit = True
                break

    # SL pasifleştirmesi adım 3'te FAZ 51 audit-shadow REVERSAL helper'ı ile
    # yapılır (process_id bazlı). Eski cancel_stock_entry içi SL temizliği
    # kaldırıldı çünkü process_no-bazlı early-break bug'a yol açıyordu.

    # FAZ 53 — Safety guard: COMPLETED + has gram + has supplier durumunda
    # stok reversal başarısız ise sessiz dönmek yerine açık hata.
    _was_completed = (str(getattr(proc, 'is_status', '') or '').upper()
                      == 'COMPLETED')
    _waiting = bool(getattr(proc, 'waiting_stock', False))
    if (total_stock_reversed == 0 and _was_completed and gram > 0
            and not _waiting):
        logger.error(
            "cancel_bracelet_purchase: STOK GERI SARILAMADI (silent fail "
            "eliminasyon) proc=%s, proc_no=%s, gram=%s — denenen kombinasyonlar "
            "ref_type ∈ ('bracelet_add','process') × ref_id ∈ %s.",
            proc.id, proc.process_no, gram, _ref_id_candidates,
        )
        raise CancelBraceletError(
            f"Stok geri sarılamadı: bilezik PURCHASE iptali için "
            f"StockLedger eşleşmesi bulunamadı "
            f"(process_no={proc.process_no or proc.id}). "
            f"İşlem iptal edilmedi; veri durumu kontrol edin."
        )

    if total_stock_reversed == 0:
        logger.warning(
            "cancel_bracelet_purchase: hiçbir StockLedger kaydı "
            "bulunamadı (proc=%s, proc_no=%s) — taslak veya tekrar iptal.",
            proc.id, proc.process_no,
        )

    # ── 3. SupplierLedger — source_process_id bazlı ek cleanup ───────────
    # FAZ 51 (R-02): Append-only REVERSAL helper'ına yönlendirildi.
    if has_supplier:
        try:
            from apps.suppliers.services import (
                reverse_supplier_ledger_for_process,
            )
            _audit = {'actor': user, 'ip_address': None, 'user_agent': ''}
            _res = reverse_supplier_ledger_for_process(
                audit=_audit,
                reason=f'Bilezik alımı iptal — {proc.process_no or proc.id}',
                process_id=proc.id,
            )
            _extra_sl = _res.get('reversed_count', 0)
            total_sl_deactivated += _extra_sl
            if _extra_sl:
                logger.info(
                    "cancel_bracelet_purchase: source_process_id=%s ile %d ek "
                    "SupplierLedger satırı REVERSAL yazıldı.",
                    proc.id, _extra_sl,
                )
        except Exception:
            logger.exception(
                "cancel_bracelet_purchase: SupplierLedger REVERSAL helper "
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
                "cancel_bracelet_purchase: Products.gram sync hata "
                "(product=%s, proc=%s): %s",
                getattr(product, 'id', None), proc.id, _gram_err,
            )

    # ── 6. WAC / milyem recalculate ───────────────────────────────────────
    if not skip_recalculate and product is not None and store is not None:
        try:
            # Lazy import: recalculate fonksiyonu bracelets/views.py içinde;
            # doğrudan import döngüsel bağımlılık yaratır.
            from apps.bracelets.views import recalculate_bracelet_pool_mileage_after_cancel
            recalculate_bracelet_pool_mileage_after_cancel(product=product, store=store)
        except Exception as _wac_err:
            logger.error(
                "cancel_bracelet_purchase: WAC recalculate hata "
                "(product=%s, proc=%s): %s",
                getattr(product, 'id', None), proc.id, _wac_err,
            )

    logger.info(
        "cancel_bracelet_purchase TAMAMLANDI: proc=%s, proc_no=%s, "
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
