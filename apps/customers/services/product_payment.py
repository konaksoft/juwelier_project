"""ProductPaymentService — Ürün/Hurda ile Tahsilat ve Ödeme (FAZ 49).

Müşteri borcunu ödemek için fiziksel ürün/hurda getirir VEYA mağaza
müşterinin alacağını ürün vererek kapatır. Mevcut nakit tahsilat
modalını (CollectionService) bozmadan paralel çalışan ayrı bir servis.

Semantik:
  - Tahsilat (collect):
      Müşteri 2 çeyrek + 12 gr 22 ayar hurda + 1.803 HS nakit getirdi.
      Stoğa giriş yapılır (PAYMENT_IN), CustomerLedger.COLLECTION_HS
      yazılır (ürün karşılığı). Nakit kısım için Payment + CashboxLedger
      + CustomerLedger.COLLECTION_TL yazılır.

  - Ödeme (pay):
      Mağaza müşteriye 1 yarım altın + 5 gr 24 ayar hurda verdi.
      Stoktan çıkış yapılır (PAYMENT_OUT), CustomerLedger.DEBT yazılır
      (process_no='PAY-...' marker ile satıştan ayrılır).

Ciro/satış raporları korunur:
  - Satış raporları `Process.transaction_type='SALE'` üzerinden çekilir;
    bu servis Process kaydı YARATMAZ. Yalnız StockLedger + CustomerLedger
    + (varsa) Payment yazımı yapar.
  - StockLedger.reason ∈ {PAYMENT_IN, PAYMENT_OUT} ile filtre edilebilir.

Atomiklik:
  Tüm adımlar tek bir `transaction.atomic()` bloğunda çalışır. Herhangi
  bir adım başarısız olursa stoğa giren ürünler, kasa hareketi ve cari
  satırların hepsi geri sarılır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as dj_tz

from apps.banking.exchange_rate_service import (
    get_current_has_rate, get_current_fx_rate,
)
from apps.customers.models import Customers, CustomerLedger
from apps.customers.services.exceptions import InvalidLedgerStateError
from apps.customers.services.ledger import LedgerService
from apps.products.models import Products
from apps.stock_management.models import StockLedger
from apps.stock_management.services.stock_service import StockService

logger = logging.getLogger('customers.product_payment')


_HS_QUANT = Decimal('0.001')
_TL_QUANT = Decimal('0.01')
_GRAM_QUANT = Decimal('0.001')


def _q_hs(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(_HS_QUANT, rounding=ROUND_HALF_UP)


def _q_tl(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(_TL_QUANT, rounding=ROUND_HALF_UP)


def _q_gram(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(_GRAM_QUANT, rounding=ROUND_HALF_UP)


# ════════════════════════════════════════════════════════════════════════
# DTO'lar
# ════════════════════════════════════════════════════════════════════════

@dataclass
class ProductPaymentItemResult:
    """Bir kalemin yazıldıktan sonraki sonuç bilgisi."""
    kind: str
    product_id: str
    product_name: str
    gram: Decimal
    pieces: int
    hs_value: Decimal
    tl_value: Decimal
    stock_ledger_id: str


@dataclass
class ProductPaymentResult:
    process_no: str
    direction: str  # 'collect' | 'pay'
    items: List[ProductPaymentItemResult] = field(default_factory=list)
    total_items_hs: Decimal = Decimal('0.000')
    total_items_tl: Decimal = Decimal('0.00')
    cash_hs: Decimal = Decimal('0.000')
    cash_tl: Decimal = Decimal('0.00')
    customer_ledger_ids: List[str] = field(default_factory=list)
    payment_id: Optional[str] = None
    cashbox_ledger_id: Optional[str] = None
    new_balance_hs: Decimal = Decimal('0.000')


# ════════════════════════════════════════════════════════════════════════
# SERVİS
# ════════════════════════════════════════════════════════════════════════

class ProductPaymentService:
    """Ürün/Hurda ile tahsilat ve ödeme akışları."""

    # ─────────────────────────────────────────────────────────────────
    # ORTAK YARDIMCILAR
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_items(items, *, direction: str):
        """Item listesini normalize et + alan validasyonu."""
        if not items or not isinstance(items, list):
            raise InvalidLedgerStateError('En az bir kalem gereklidir.')
        if len(items) > 50:
            raise InvalidLedgerStateError(
                'Tek seferde en fazla 50 kalem işlenebilir.',
            )

        normalized = []
        for idx, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise InvalidLedgerStateError(
                    f'Kalem #{idx}: geçersiz veri tipi.',
                )
            kind = (raw.get('kind') or '').strip().lower()
            if kind not in ('product', 'scrap', 'bracelet'):
                raise InvalidLedgerStateError(
                    f'Kalem #{idx}: geçersiz tip ({kind}). '
                    f'Desteklenen: product / scrap / bracelet.',
                )
            try:
                gram = _q_gram(raw.get('gram', 0))
                pieces = int(raw.get('pieces') or 0)
                hs_value = _q_hs(raw.get('hs_value', 0))
                tl_value = _q_tl(raw.get('tl_value', 0))
            except Exception as ex:
                raise InvalidLedgerStateError(
                    f'Kalem #{idx}: sayısal alan hatası: {ex}',
                )

            if hs_value <= 0:
                raise InvalidLedgerStateError(
                    f'Kalem #{idx}: HS değeri pozitif olmalıdır.',
                )
            if gram <= 0 and pieces <= 0:
                raise InvalidLedgerStateError(
                    f'Kalem #{idx}: gram veya adet pozitif olmalıdır.',
                )

            normalized.append({
                'kind': kind,
                'product_id': raw.get('product_id'),
                'name': (raw.get('name') or '').strip(),
                'mileage': raw.get('mileage'),
                'gram': gram,
                'pieces': pieces,
                'hs_value': hs_value,
                'tl_value': tl_value,
                'description': (raw.get('description') or '').strip(),
            })
        return normalized

    @staticmethod
    def _resolve_or_create_pool(*, store, item: dict, user):
        """Item'a göre Products satırını bulur veya oluşturur.

        - kind='product' → mevcut Products kaydı (UUID ile getir)
        - kind='scrap'   → Hurda havuzu (find_scrap_pool_by_selected_karat)
                           yoksa yeni hurda ürünü oluştur (retail/scrap_add gibi)
        - kind='bracelet'→ Bilezik kategorisinde mevcut/yeni ürün
        """
        from apps.definitions.categories.models import Categories
        from apps.scraps.models import Scraps
        from apps.bracelets.models import Bracelets
        from apps.scraps.views import (
            find_scrap_pool_by_selected_karat,
            update_scrap_pool_weighted_mileage,
        )

        kind = item['kind']

        if kind == 'product':
            pid = item.get('product_id')
            if not pid:
                raise InvalidLedgerStateError(
                    'Mevcut ürün için product_id zorunludur.',
                )
            # FAZ 49 fix v3.1 — `cari_product_search` mağazaya ait VE global
            # (store=NULL) ürünleri birlikte gösteriyor (`Q(store=store) |
            # Q(store__isnull=True)`). Servis aynı koşulu uygulamadığında
            # global ürünler aramada görünüyor ama "Kaydet"te `DoesNotExist`
            # atıyordu (Eski Çeyrek/Yarım/Tam vb. standart altınlar tipik
            # `store=NULL` ile kayıtlı). Aynı OR koşulunu burada da uyguluyoruz.
            try:
                prod = Products.objects.get(
                    Q(store=store) | Q(store__isnull=True),
                    pk=pid, is_deleted=False, is_active=True,
                )
            except Products.DoesNotExist:
                raise InvalidLedgerStateError(
                    f'Ürün bulunamadı: {pid}',
                )
            if prod.is_currency:
                raise InvalidLedgerStateError(
                    f'Döviz ürünleri bu akışta kullanılamaz: {prod.name}',
                )
            # Barkodlu (özel/tekil) ürünler kapsam dışı (kullanıcı kararı)
            if not prod.is_gram_bullion and prod.material_type in ('WATCH', 'DIAMOND'):
                raise InvalidLedgerStateError(
                    f'Saat/Pırlanta türü ürünler bu akışta kullanılamaz: '
                    f'{prod.name}',
                )
            return prod, None  # weighted_mileage update gerekmez

        # kind in ('scrap', 'bracelet')
        try:
            mileage = Decimal(str(item.get('mileage') or 0))
        except Exception:
            mileage = Decimal('0')
        if mileage <= 0:
            raise InvalidLedgerStateError(
                f'{kind.capitalize()} kalem için ayar (milyem) zorunludur.',
            )
        name = item.get('name') or f"{int(mileage)} Milyem {kind.capitalize()}"

        if kind == 'scrap':
            cat, _ = Categories.objects.get_or_create(name='Hurda')
            existing = find_scrap_pool_by_selected_karat(
                store=store, category=cat, scrap_name=name,
                fallback_mileage=mileage, is_scrap=True,
                material_type='GOLD',
            )
            if existing:
                return existing, mileage  # pool update sinyali

            has_price = (mileage / Decimal('1000'))
            mileage_str = str(int(mileage))
            karat_map = {
                '995': '24 Ayar', '916': '22 Ayar', '875': '21 Ayar',
                '750': '18 Ayar', '585': '14 Ayar', '333': '8 Ayar',
            }
            karat_name = karat_map.get(mileage_str, f"{mileage_str} Milyem")
            new_prod = Products.objects.create(
                store=store, category=cat,
                name=f"{karat_name} Hurda",
                gram=Decimal('0'), product_mileage=mileage,
                buy_price_hs=has_price, sale_price_hs=has_price,
                is_scrap=True, is_gram_bullion=True, is_active=True,
            )
            Scraps.objects.create(store=store, product=new_prod, created_by=user)
            return new_prod, mileage

        # kind == 'bracelet'
        cat, _ = Categories.objects.get_or_create(name='Bilezik')
        # Bilezik için aynı name + mileage çiftine sahip aktif ürün varsa
        # onu kullan; yoksa yeni oluştur.
        existing = (
            Products.objects
            .filter(
                store=store, category=cat,
                name__iexact=name, product_mileage=mileage,
                is_deleted=False, is_active=True,
            )
            .order_by('created_on', 'id')
            .first()
        )
        if existing:
            return existing, mileage

        has_price = (mileage / Decimal('1000'))
        new_prod = Products.objects.create(
            store=store, category=cat, name=name,
            gram=Decimal('0'), product_mileage=mileage,
            buy_price_hs=has_price, sale_price_hs=has_price,
            is_gram_bullion=True, is_active=True,
        )
        Bracelets.objects.create(store=store, product=new_prod, created_by=user)
        return new_prod, mileage

    @staticmethod
    def _write_stock_movement(
        *, product, store, item: dict, direction: str,
        process_no: str, audit: dict, has_rate: Decimal,
    ):
        """Stok hareketi yaz (PAYMENT_IN veya PAYMENT_OUT)."""
        gram = item['gram']
        pieces = item['pieces']
        hs_value = item['hs_value']
        tl_value = item['tl_value']

        # Birim Has maliyeti: HS değeri / gram (gramajlı için);
        # adet bazlı için HS değeri / pieces.
        if gram > 0:
            unit_cost_hs = _q_hs(hs_value / gram) if gram > 0 else Decimal('0')
        else:
            unit_cost_hs = (
                _q_hs(hs_value / Decimal(pieces)) if pieces > 0 else Decimal('0')
            )

        # Birim TL maliyeti: TL değeri / gram (veya pieces)
        if gram > 0:
            unit_cost_eur = _q_tl(tl_value / gram) if gram > 0 else Decimal('0')
        else:
            unit_cost_eur = (
                _q_tl(tl_value / Decimal(pieces)) if pieces > 0 else Decimal('0')
            )

        notes = (
            f"Ürün ile {'Tahsilat' if direction == 'collect' else 'Ödeme'} — "
            f"{product.name}"
        )
        if item.get('description'):
            notes += f" | {item['description']}"

        if direction == 'collect':
            ledger = StockService.record_entry(
                product=product, store=store,
                quantity_gram=gram, quantity_pieces=pieces,
                reason=StockLedger.Reason.PAYMENT_IN,
                ref_type='customer_payment',
                ref_id=process_no,
                unit_cost_hs=unit_cost_hs,
                unit_cost_eur=unit_cost_eur,
                hs_rate_eur=has_rate,
                user=audit.get('actor'),
                notes=notes,
            )
        else:
            ledger = StockService.record_exit(
                product=product, store=store,
                quantity_gram=gram, quantity_pieces=pieces,
                reason=StockLedger.Reason.PAYMENT_OUT,
                ref_type='customer_payment',
                ref_id=process_no,
                unit_cost_hs=unit_cost_hs,
                unit_cost_eur=unit_cost_eur,
                hs_rate_eur=has_rate,
                user=audit.get('actor'),
                notes=notes,
            )
        return ledger

    # ─────────────────────────────────────────────────────────────────
    # SENARYO A + B: Ürün/Hurda + (opsiyonel) Nakit ile TAHSİLAT
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def collect_with_products(
        *,
        customer,
        store,
        items: list,
        cash_amount: Decimal = Decimal('0'),
        cash_currency: str = 'TRY',
        bank_account=None,
        audit: dict,
        description: str = '',
        process_no: Optional[str] = None,
    ) -> ProductPaymentResult:
        """Müşteriden ürün/hurda + (opsiyonel) nakit ile tahsilat.

        Args:
            customer, store: zorunlu.
            items: [{kind, product_id?, name?, mileage?, gram, pieces,
                    hs_value, tl_value, description?}, ...]
            cash_amount: Kalan nakit tutar (currency cinsinden, opsiyonel).
            cash_currency: 'TRY'|'USD'|'EUR'|'GBP'|'HS' (cash_amount > 0 ise).
            bank_account: Nakit kısım için kasa (cash_amount > 0 ise zorunlu).
            audit: extract_audit_context(request) çıktısı.
            description: Genel açıklama (tüm ledger satırlarına yazılır).
            process_no: Override; verilmezse otomatik 'PRC-YYYYMMDDHHMMSS'.
        """
        cash_amount = Decimal(cash_amount or 0)

        # Müşteri row-lock (eşzamanlılık)
        Customers.objects.select_for_update().filter(pk=customer.pk).first()

        # Item normalize + validasyon
        norm_items = ProductPaymentService._validate_items(items, direction='collect')

        # Anlık has kuru (tek sefer)
        has_rate = get_current_has_rate(store) or Decimal('0')
        if has_rate <= 0:
            raise InvalidLedgerStateError(
                'Has altın kuru servisi yanıt vermedi. İşlem yapılamaz.',
            )

        if not process_no:
            process_no = f'PRC-{dj_tz.now().strftime("%Y%m%d%H%M%S")}'

        result = ProductPaymentResult(
            process_no=process_no, direction='collect',
        )

        # ── 1) Her kalem için stoğa giriş + (varsa) hurda havuz update
        from apps.scraps.views import update_scrap_pool_weighted_mileage

        for item in norm_items:
            product, weighted_mileage = ProductPaymentService._resolve_or_create_pool(
                store=store, item=item, user=audit.get('actor'),
            )

            # Hurda/Bilezik havuzu için ağırlıklı milyem update
            # (yeni ayar pool'a eklenirken Products meta güncellenir).
            # update_scrap_pool_weighted_mileage StockSnapshot bağımsız
            # çalışır; sadece Products.product_mileage + buy_price_hs ayarlar.
            if weighted_mileage and item['gram'] > 0:
                try:
                    update_scrap_pool_weighted_mileage(
                        product=product, store=store,
                        new_gram=item['gram'],
                        new_mileage=weighted_mileage,
                    )
                except Exception as ex:
                    logger.warning(
                        f"Pool weighted mileage update başarısız "
                        f"(product={product.id}): {ex}"
                    )

            stock_ledger = ProductPaymentService._write_stock_movement(
                product=product, store=store, item=item, direction='collect',
                process_no=process_no, audit=audit, has_rate=has_rate,
            )

            # Bilezik için Products.gram'ı manuel artır (havuz mantığı)
            if item['kind'] == 'bracelet' and item['gram'] > 0:
                Products.objects.filter(pk=product.pk).update(
                    gram=(product.gram or Decimal('0')) + item['gram'],
                )

            result.items.append(ProductPaymentItemResult(
                kind=item['kind'],
                product_id=str(product.id),
                product_name=product.name,
                gram=item['gram'], pieces=item['pieces'],
                hs_value=item['hs_value'], tl_value=item['tl_value'],
                stock_ledger_id=str(stock_ledger.id),
            ))
            result.total_items_hs += item['hs_value']
            result.total_items_tl += item['tl_value']

        result.total_items_hs = _q_hs(result.total_items_hs)
        result.total_items_tl = _q_tl(result.total_items_tl)

        # ── 2) CustomerLedger.COLLECTION_HS — ürünler için tek satır
        if result.total_items_hs > 0:
            cl_desc = (
                f"Ürün ile Tahsilat ({len(result.items)} kalem)"
                + (f" — {description}" if description else "")
            )
            cl_entry = LedgerService.write_collection(
                customer=customer, store=store,
                transaction_type=CustomerLedger.COLLECTION_HS,
                amount_hs=result.total_items_hs,
                amount_eur=result.total_items_tl,
                exchange_rate_eur=has_rate,
                process_no=process_no, audit=audit,
                currency=CustomerLedger.CURRENCY_HS,
                description=cl_desc[:255],
            )
            result.customer_ledger_ids.append(str(cl_entry.id))

        # ── 3) (Opsiyonel) Nakit kısım için CollectionService.collect_and_close
        if cash_amount > 0:
            if bank_account is None:
                raise InvalidLedgerStateError(
                    'Nakit kısım için kasa seçimi zorunludur.',
                )
            from apps.customers.services.collection import CollectionService
            cash_result = CollectionService.collect_and_close(
                customer=customer, store=store,
                bank_account=bank_account,
                payment_amount=cash_amount,
                payment_currency=cash_currency,
                audit=audit,
                process_no=process_no,
            )
            result.payment_id = (
                str(cash_result.payment.id)
                if getattr(cash_result, 'payment', None) else None
            )
            result.cashbox_ledger_id = (
                str(cash_result.cashbox_entry.id)
                if getattr(cash_result, 'cashbox_entry', None) else None
            )
            if getattr(cash_result, 'collection_entry', None):
                result.customer_ledger_ids.append(
                    str(cash_result.collection_entry.id),
                )
            result.cash_hs = _q_hs(getattr(cash_result, 'closed_amount_hs', 0))
            result.cash_tl = _q_tl(getattr(cash_result, 'closed_amount_eur', 0))

        # ── 4) Yeni bakiye
        result.new_balance_hs = _q_hs(LedgerService.get_open_balance_hs(customer))

        logger.info(
            f"ProductPayment.collect process_no={process_no} "
            f"customer={customer.id} items={len(result.items)} "
            f"items_hs={result.total_items_hs} cash_hs={result.cash_hs} "
            f"new_balance_hs={result.new_balance_hs}"
        )
        return result

    # ─────────────────────────────────────────────────────────────────
    # SENARYO C: Mağaza müşteriye ürün ile ÖDEME
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def pay_with_products(
        *,
        customer,
        store,
        items: list,
        audit: dict,
        description: str = '',
        process_no: Optional[str] = None,
    ) -> ProductPaymentResult:
        """Mağaza müşteriye ürün/hurda vererek alacağı kapatır.

        Stoktan PAYMENT_OUT olarak çıkış yazılır (SALE'e benzemez ki
        ciro raporlarını şişirmesin). CustomerLedger'a DEBT yazılır
        (DEBT_INCREASING — müşterinin mağazaya borcu artar; alacaklı ise
        alacağı azalır). process_no='PAY-...' marker ile satıştan ayrışır.
        """
        Customers.objects.select_for_update().filter(pk=customer.pk).first()

        norm_items = ProductPaymentService._validate_items(items, direction='pay')

        has_rate = get_current_has_rate(store) or Decimal('0')
        if has_rate <= 0:
            raise InvalidLedgerStateError(
                'Has altın kuru servisi yanıt vermedi. İşlem yapılamaz.',
            )

        if not process_no:
            process_no = f'PAY-{dj_tz.now().strftime("%Y%m%d%H%M%S")}'

        result = ProductPaymentResult(
            process_no=process_no, direction='pay',
        )

        for item in norm_items:
            # Pay yönünde yalnız MEVCUT ürünler kullanılabilir
            # (yeni hurda/bilezik girişi pay yönünde anlamsız).
            if item['kind'] != 'product':
                raise InvalidLedgerStateError(
                    'Mağazadan müşteriye ödeme yalnız mevcut stok ürünleriyle '
                    'yapılabilir (yeni hurda/bilezik kalem oluşturulamaz).',
                )
            product, _ = ProductPaymentService._resolve_or_create_pool(
                store=store, item=item, user=audit.get('actor'),
            )

            stock_ledger = ProductPaymentService._write_stock_movement(
                product=product, store=store, item=item, direction='pay',
                process_no=process_no, audit=audit, has_rate=has_rate,
            )
            result.items.append(ProductPaymentItemResult(
                kind=item['kind'],
                product_id=str(product.id),
                product_name=product.name,
                gram=item['gram'], pieces=item['pieces'],
                hs_value=item['hs_value'], tl_value=item['tl_value'],
                stock_ledger_id=str(stock_ledger.id),
            ))
            result.total_items_hs += item['hs_value']
            result.total_items_tl += item['tl_value']

        result.total_items_hs = _q_hs(result.total_items_hs)
        result.total_items_tl = _q_tl(result.total_items_tl)

        # CustomerLedger.DEBT — DEBT_INCREASING ailesinde tek tip.
        # Müşteri alacaklı (CREDIT) durumda ise bu DEBT alacağı azaltır.
        # process_no='PAY-...' marker ile satıştan ayrışır; sales reports
        # Process.transaction_type='SALE' üzerinden çekildiği için
        # bu satır ciroya YANSIMAZ.
        cl_desc = (
            f"Ürün ile Ödeme ({len(result.items)} kalem)"
            + (f" — {description}" if description else "")
        )
        cl_entry = LedgerService.write_debt(
            customer=customer, store=store,
            amount_hs=result.total_items_hs,
            amount_eur=result.total_items_tl,
            exchange_rate_eur=has_rate,
            process_no=process_no, audit=audit,
            description=cl_desc[:255],
        )
        result.customer_ledger_ids.append(str(cl_entry.id))

        result.new_balance_hs = _q_hs(LedgerService.get_open_balance_hs(customer))

        logger.info(
            f"ProductPayment.pay process_no={process_no} "
            f"customer={customer.id} items={len(result.items)} "
            f"items_hs={result.total_items_hs} "
            f"new_balance_hs={result.new_balance_hs}"
        )
        return result

    # ─────────────────────────────────────────────────────────────────
    # FAZ 51 (R-05): ÜRÜN/HURDA İLE TAHSİLAT/ÖDEME GERİ ALMA
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def reverse_collection(
        *,
        customer,
        store,
        process_no: str,
        audit: dict,
        reason: str,
    ) -> dict:
        """Bir Ürün/Hurda ile Tahsilat veya Ödeme işlemini atomik olarak geri al.

        Akış:
          1) StockLedger reverse — process_no'ya bağlı tüm PAYMENT_IN/OUT
             satırlarını cancel_stock_entry ile geri sar (REVERSAL_REASON_MAP
             FAZ 49'dan beri PAYMENT_IN ↔ PAYMENT_OUT eşlemesini içeriyor).
          2) CustomerLedger reverse — bu process_no'ya yazılmış aktif tüm
             satırlar (COLLECTION_HS, COLLECTION_TL, DEBT) için
             LedgerService.reverse_entry çağrılır. Onaylı REVERSAL ise
             propagate_reversal_side_effects:
               • CashboxLedger.REVERSAL yazar (related_payment_id üzerinden)
               • Payment.is_cancelled=True yapar (R-08)
               • IncomeExpenseLedger.is_reversed flag düşer
             Onay yetersiz aktör için PENDING REVERSAL yazılır → manager
             onaylayınca yan etkiler tetiklenir.
          3) Atomik blok — herhangi adım fail → tümü rollback.

        Args:
            customer, store: zorunlu (yetki sınırı için).
            process_no: 'PRC-' veya 'PAY-' ile başlayan işlem no.
            audit, reason: standart audit + neden.

        Returns:
            {stock_reversed, customer_ledger_reversed, customer_ledger_skipped}
        """
        from apps.stock_management.services.cancel_service import (
            cancel_stock_entry,
        )

        if not process_no:
            raise InvalidLedgerStateError('process_no zorunludur.')
        if not reason:
            raise InvalidLedgerStateError(
                'Geri alma nedeni zorunludur.',
            )

        # ── 1) StockLedger reverse — ref_id=process_no, ref_type='customer_payment'
        stock_reversed = 0
        try:
            sl_res = cancel_stock_entry(
                ref_type='customer_payment',
                ref_id=process_no,
                user=audit.get('actor'),
                reverse_supplier_ledger=False,
                notes=f'ProductPayment REVERSE — {reason}',
                raise_if_not_found=False,
            )
            if isinstance(sl_res, dict):
                stock_reversed = (
                    sl_res.get('cancelled_stock_count')
                    or sl_res.get('reversed_count')
                    or 0
                )
        except Exception:
            # Atomic blok içinde — exception'ı bubble et
            logger.exception(
                "ProductPayment.reverse_collection: stok reverse başarısız "
                "(process_no=%s)", process_no,
            )
            raise

        # ── 2) CustomerLedger reverse — bu process_no'nun aktif satırları
        cl_reversed = 0
        cl_skipped = 0
        cl_qs = CustomerLedger.objects.filter(
            customer=customer,
            process_no=process_no,
            is_active=True,
        ).exclude(transaction_type=CustomerLedger.REVERSAL)
        for cl in cl_qs:
            try:
                LedgerService.reverse_entry(
                    original=cl, audit=audit,
                    reason=f'Ürün ile tahsilat iptali: {reason}',
                )
                cl_reversed += 1
            except InvalidLedgerStateError:
                cl_skipped += 1

        logger.info(
            "ProductPayment.reverse_collection: process_no=%s → "
            "stok %d satır reverse, CustomerLedger %d reverse (%d skip).",
            process_no, stock_reversed, cl_reversed, cl_skipped,
        )

        return {
            'stock_reversed': stock_reversed,
            'customer_ledger_reversed': cl_reversed,
            'customer_ledger_skipped': cl_skipped,
        }
