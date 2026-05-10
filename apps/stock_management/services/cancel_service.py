"""
Cancel Service — Evrensel Stok ve Cari Geri Sarma (Reversal) Utility
================================================================================

FAZ B: İptal edilen bir işlemin hem StockLedger hem de SupplierLedger tarafında
"geri sarma" (reversal) kayıtlarını atomik bir transaction içinde oluşturur.

Prensipler:
  1. STOK IMMUTABILITY    : StockLedger satırları güncellenmez/silinmez. Yeni
                             ters yönlü reversal satırları eklenir; orijinal
                             satırlar audit trail için aktif kalır.
  2. ATOMIC               : Tüm reversal işlemleri tek bir transaction içinde
                             yapılır; herhangi bir adım hata verirse hepsi
                             geri alınır.
  3. MATERIAL_TYPE-AWARE  : Reversal hesaplamaları ürünün material_type'ına
                             göre doğru para birimi/birim semantiğiyle yapılır.

Çalışma Akışı:
  cancel_stock_entry(ref_type='process', ref_id='202600001')
    |
    +-- [1] Orijinal StockLedger satirlari -> ters yonlu reversal (StockService)
    |
    +-- [2] Orijinal SupplierLedger satirlari soft-disable (is_active=False)
    |         * balance_summary() sadece is_active=True okur; orijinal kayit
    |           pasif olunca bakiyeden cikar -> net = 0.
    |         * (Onarim Fazi 10 oncesi: ayrica ters tip "_CANCEL" reversal
    |           SupplierLedger kaydi olusturulurdu; bu kayit aktif kaldigi
    |           icin bakiyede tek-yonlu hayalet alacak/borc olusturuyordu.
    |           Onarim Fazi 10'da reversal kayit uretimi kaldirildi.)
    |
    +-- [3] Sonuc ozeti dondur.

ÖNEMLİ:
  - Orijinal StockLedger satirlari AKTIF KALIR (immutability); StockSnapshot
    dengesi yeni reversal satirlari ile kapatilir.
  - SupplierLedger tarafinda reversal satiri ARTIK URETILMEZ; sadece orijinal
    satirlar is_active=False yapilir. Bu davranis `process.operations.cancel_row`
    ile birebir tutarlidir ve `balance_summary()` cikti = 0 garantisi saglar.
"""

import logging
from decimal import Decimal
from typing import Optional

from django.db import transaction

from apps.stock_management.models import StockLedger
from apps.stock_management.services.stock_service import StockService

logger = logging.getLogger('stock_management')


# ============================================================================
# REVERSAL REASON MAP
# ============================================================================
# Orijinal islem sebebi -> reversal kaydinin sebebi.
# Amaç: Semantik olarak "geri cevrilmis" sebep yazabilmek (ornek: PURCHASE ->
# RETURN_OUT, SALE -> RETURN_IN). Bu, raporlarda ve audit trail'de iptalin
# nedenini netlestirir.
REVERSAL_REASON_MAP = {
    StockLedger.Reason.PURCHASE:          StockLedger.Reason.RETURN_OUT,
    StockLedger.Reason.SALE:              StockLedger.Reason.RETURN_IN,
    StockLedger.Reason.RETURN_IN:         StockLedger.Reason.SALE,
    StockLedger.Reason.RETURN_OUT:        StockLedger.Reason.PURCHASE,
    StockLedger.Reason.CONVERSION_OUT:    StockLedger.Reason.CONVERSION_IN,
    StockLedger.Reason.CONVERSION_IN:     StockLedger.Reason.CONVERSION_OUT,
    StockLedger.Reason.TRANSFER_OUT:      StockLedger.Reason.TRANSFER_IN,
    StockLedger.Reason.TRANSFER_IN:       StockLedger.Reason.TRANSFER_OUT,
    StockLedger.Reason.ADJUSTMENT_PLUS:   StockLedger.Reason.ADJUSTMENT_MINUS,
    StockLedger.Reason.ADJUSTMENT_MINUS:  StockLedger.Reason.ADJUSTMENT_PLUS,
    StockLedger.Reason.CUSTODY_IN:        StockLedger.Reason.CUSTODY_OUT,
    StockLedger.Reason.CUSTODY_OUT:       StockLedger.Reason.CUSTODY_IN,
    StockLedger.Reason.REPAIR_IN:         StockLedger.Reason.REPAIR_OUT,
    StockLedger.Reason.REPAIR_OUT:        StockLedger.Reason.REPAIR_IN,
    # FAZ 49 — Ürün ile Tahsilat / Ödeme simetrisi
    StockLedger.Reason.PAYMENT_IN:        StockLedger.Reason.PAYMENT_OUT,
    StockLedger.Reason.PAYMENT_OUT:       StockLedger.Reason.PAYMENT_IN,
    # INITIAL, SCRAP_MELT icin default fallback: ADJ_MINUS / ADJ_PLUS
}


# ============================================================================
# ÖZEL HATA SINIFLARI
# ============================================================================

class CancelNotFoundError(Exception):
    """Verilen ref_type/ref_id icin hicbir StockLedger/SupplierLedger kaydi bulunamadi."""
    pass


class CancelIntegrityError(Exception):
    """Cancel islemi sirasinda veri butunlugu ihlali (reversal basarisiz)."""
    pass


# ============================================================================
# YARDIMCI FONKSİYONLAR
# ============================================================================

def _reverse_reason_for(original_reason: str, original_direction: str) -> str:
    """
    Orijinal ledger satirinin sebep/yon bilgilerine gore reversal sebebi secer.

    REVERSAL_REASON_MAP'te eslesme yoksa guvenli default:
      - original IN  -> ADJUSTMENT_MINUS
      - original OUT -> ADJUSTMENT_PLUS
    """
    mapped = REVERSAL_REASON_MAP.get(original_reason)
    if mapped:
        return mapped
    if original_direction == StockLedger.Direction.IN:
        return StockLedger.Reason.ADJUSTMENT_MINUS
    return StockLedger.Reason.ADJUSTMENT_PLUS


# ============================================================================
# ANA FONKSIYON: cancel_stock_entry
# ============================================================================

@transaction.atomic
def cancel_stock_entry(
    *,
    ref_type: str,
    ref_id: str,
    user=None,
    reverse_supplier_ledger: bool = True,
    fiat_currency: str = 'TRY',
    notes: str = '',
    raise_if_not_found: bool = False,
) -> dict:
    """
    Evrensel Stok + Cari Geri Sarma (Reversal) Utility.

    Belirli bir (ref_type, ref_id) çiftine ait tüm orijinal StockLedger ve
    SupplierLedger kayıtlarını tespit edip, hepsinin ters yönlü reversal
    karşılıklarını atomik bir transaction içinde oluşturur.

    Args:
        ref_type: Orijinal işlemin referans tipi (ör: 'process', 'conversion',
                  'invoice', 'transfer'). StockLedger.ref_type ile eşleşmeli.
        ref_id: Orijinal işlemin referans ID'si (UUID veya process_no).
                Hem StockLedger.ref_id hem de SupplierLedger.process_no
                üzerinden eşleme aranır.
        user: İptali gerçekleştiren kullanıcı (audit amaçlı).
        reverse_supplier_ledger: True ise SupplierLedger orijinal kayıtları
                                  is_active=False olarak işaretlenir (soft
                                  disable). False ise SupplierLedger'a hiç
                                  dokunulmaz, yalnızca stok geri sarılır.
                                  NOT (Onarım Fazı 10): Ters tip "_CANCEL"
                                  reversal kaydı ARTIK ÜRETİLMEZ; tek-yönlü
                                  bakiye hayaleti (Bulgu 5) bu sayede önlenir.
        fiat_currency: WATCH/DIAMOND ürünler için kullanılacak fiat birim kodu
                       (ledger'da yazılacak currency). Default: 'TRY'.
                       GOLD/SILVER ürünlerde bu parametre yok sayılır
                       (otomatik HS/HG seçilir).
        notes: Reversal satırlarına yazılacak açıklama. Başına 'IPTAL:' eklenir.
        raise_if_not_found: True ise hiçbir kayıt bulunamadığında
                            CancelNotFoundError fırlatır. Default: False.

    Returns:
        dict: {
            'stock_reversals': [StockLedger, ...],          # Yeni stok reversal kayıtları
            'supplier_ledger_reversals': [],                 # GERI UYUMLULUK: Onarim Fazi 10'dan
                                                              # itibaren bu liste daima bostur.
                                                              # (Reversal kaydi uretilmiyor.)
            'deactivated_supplier_ledgers': int,             # Kaç tane orijinal SL is_active=False oldu
            'cancelled_stock_count': int,                    # Toplam stok reversal sayısı
            'ref_type': str,
            'ref_id': str,
        }

    Raises:
        CancelNotFoundError: raise_if_not_found=True ve hiçbir kayıt yoksa.
        InsufficientStockError: Stok reversal sırasında yetersiz stok varsa.
                                (Orijinal giriş sonrası stok eridi ise oluşur.)
        ValueError: material_type validasyon hatası (saat/pırlanta gram!=0 vs).
        CancelIntegrityError: Beklenmedik veri bütünlük sorunu.

    Örnekler:
        # 1) Bir satış işlemini tam iptal et (stok + cari):
        cancel_stock_entry(
            ref_type='process',
            ref_id=str(process.id),
            user=request.user,
            notes='Musteri iadesi',
        )

        # 2) Sadece stok geri sarma (cari'ye dokunma):
        cancel_stock_entry(
            ref_type='conversion',
            ref_id=conversion_id,
            reverse_supplier_ledger=False,
        )

        # 3) Saat/pırlanta iptali - fiat birimi USD olarak geri sar:
        cancel_stock_entry(
            ref_type='process',
            ref_id=str(process.id),
            fiat_currency='USD',
            user=request.user,
        )
    """
    # Lazy import (circular import riskini azaltmak icin)
    from apps.suppliers.models import SupplierLedger

    result = {
        'stock_reversals': [],
        'supplier_ledger_reversals': [],
        'deactivated_supplier_ledgers': 0,
        'cancelled_stock_count': 0,
        'ref_type': ref_type,
        'ref_id': str(ref_id),
    }

    # ------------------------------------------------------------------
    # 1) StockLedger orijinal satirlarini cek (Snapshot kilidi yok - ancak
    #    StockService cagrisi kendi select_for_update()'ini uygulayacak).
    # ------------------------------------------------------------------
    # Yalnizca bu ref'e ait orijinal satirlari topla. Daha onceden uretilmis
    # reversal satirlari ('{ref_type}_cancel') tekrar ters cevrilmemeli.
    original_stock_entries = (
        StockLedger.objects
        .filter(ref_type=ref_type, ref_id=str(ref_id))
        .select_related('product', 'store')
        .order_by('created_on')
    )

    if not original_stock_entries.exists():
        logger.warning(
            f"cancel_stock_entry: StockLedger'da kayit yok "
            f"(ref_type={ref_type}, ref_id={ref_id})"
        )
        if raise_if_not_found and not reverse_supplier_ledger:
            raise CancelNotFoundError(
                f"ref_type='{ref_type}', ref_id='{ref_id}' icin hicbir "
                f"StockLedger kaydi bulunamadi."
            )

    # ------------------------------------------------------------------
    # 2) Stok reversal: her orijinal satir icin ters yonlu bir StockService
    #    cagrisi yap. StockService kendisi material_type validasyonunu,
    #    stok yeterlilik kontrolunu, WAC guncellemesini ve cache'i yonetir.
    # ------------------------------------------------------------------
    reversal_ref_type = f"{ref_type}_cancel"
    base_note = (notes or '').strip()

    for orig in original_stock_entries:
        compound_note = f"IPTAL: {base_note or (orig.notes or '')}".strip(': ').strip()

        reverse_reason = _reverse_reason_for(orig.reason, orig.direction)

        if orig.direction == StockLedger.Direction.IN:
            # Orijinal GIRIS -> ters CIKIS
            reversal = StockService.record_exit(
                product=orig.product,
                store=orig.store,
                quantity_gram=orig.quantity_gram,
                quantity_pieces=orig.quantity_pieces,
                reason=reverse_reason,
                ref_type=reversal_ref_type,
                ref_id=str(ref_id),
                unit_cost_hs=orig.unit_cost_hs,
                unit_cost_eur=orig.unit_cost_eur,
                hs_rate_eur=orig.hs_rate_eur,
                user=user,
                notes=compound_note,
            )
        else:
            # Orijinal CIKIS -> ters GIRIS
            reversal = StockService.record_entry(
                product=orig.product,
                store=orig.store,
                quantity_gram=orig.quantity_gram,
                quantity_pieces=orig.quantity_pieces,
                reason=reverse_reason,
                ref_type=reversal_ref_type,
                ref_id=str(ref_id),
                unit_cost_hs=orig.unit_cost_hs,
                unit_cost_eur=orig.unit_cost_eur,
                hs_rate_eur=orig.hs_rate_eur,
                user=user,
                notes=compound_note,
            )

        result['stock_reversals'].append(reversal)
        result['cancelled_stock_count'] += 1

    # ------------------------------------------------------------------
    # 3) SupplierLedger SOFT-DISABLE (Onarım Fazı 10 — Bulgu 5 düzeltmesi)
    # ------------------------------------------------------------------
    # KÖK NEDEN (Bulgu 5):
    #   Eski akış orijinal SL kaydını is_active=False yapıyor VE ek olarak ters
    #   transaction_type'lı, is_active=True bir "_CANCEL" reversal satırı
    #   üretiyordu. Niyet "double-entry net sıfır" idi; ancak balance_summary()
    #   sadece is_active=True kayıtları topladığı için:
    #       receivable += reversal (EXIT, aktif)   = +23.80
    #       payable    += orijinal (ENTRY, pasif)  =   0.00
    #       --------------------------------------------------
    #       net                                    = +23.80   (HAYALET ALACAK)
    #   Yani matematik tek-yönlü kalıyor; iptal sonrası UI hâlâ
    #   "ALACAKLISINIZ 23.80 HS" gösteriyordu (Test Bulgusu 5).
    #
    # ÇÖZÜM (Seçenek A — `process.operations.cancel_row` ile tutarlı):
    #   Reversal SupplierLedger satırı ARTIK ÜRETİLMEZ. Yalnızca orijinal
    #   satırlar is_active=False yapılır → balance_summary() onları görmez →
    #   ilgili process'in cariye katkısı temiz şekilde 0'a düşer.
    #   Audit trail: orijinal kayıt is_active=False ile DB'de kalır; iptal
    #   sebebi/kullanıcısı Process tarafındaki CANCELED + is_deleted işareti
    #   ve (varsa) StockLedger reversal satırlarındaki "IPTAL: ..." notuyla
    #   takip edilir.
    # ------------------------------------------------------------------
    if reverse_supplier_ledger:
        original_sl_entries = list(
            SupplierLedger.objects
            .filter(process_no=str(ref_id), is_active=True)
            .values_list('pk', flat=True)
        )

        if not original_sl_entries and raise_if_not_found and not result['cancelled_stock_count']:
            raise CancelNotFoundError(
                f"ref_id='{ref_id}' icin ne StockLedger ne SupplierLedger kaydi "
                f"bulunamadi; iptal edilecek bir sey yok."
            )

        if original_sl_entries:
            deactivated_count = SupplierLedger.objects.filter(
                pk__in=original_sl_entries
            ).update(is_active=False)
            result['deactivated_supplier_ledgers'] = deactivated_count

    logger.info(
        f"cancel_stock_entry TAMAMLANDI: ref_type={ref_type}, ref_id={ref_id}, "
        f"stock_reversals={result['cancelled_stock_count']}, "
        f"sl_deactivated={result['deactivated_supplier_ledgers']}"
    )

    return result
