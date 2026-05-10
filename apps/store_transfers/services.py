# ============================================================================
# DOSYA: apps/store_transfers/services.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v2 — FAZ 47: Kasa + Stok Transferi Orkestrasyon Servisi
#
# AMAÇ:
#   StoreTransfer'ın yaşam döngüsünü orkestre eden tek giriş noktası.
#   Tüm ledger yazımları bu servis üzerinden geçer; doğrudan model yazımı
#   YASAKTIR.
#
# DURUM AKIŞI:
#   DRAFT      → IN_TRANSIT (dispatch_transfer)
#   IN_TRANSIT → ACCEPTED   (accept_transfer)
#   IN_TRANSIT → REJECTED   (reject_transfer)
#   DRAFT      → CANCELLED  (cancel_draft)
#
# DEFTER YAZIM KURALLARI (Append-Only):
#
#   ── CASH Akışı (FAZ 46) ─────────────────────────────────────────
#   dispatch:
#     - CashboxLedger(source_bank_account, TRANSFER_OUT, +amount)
#     - CashboxLedger(source_transit, TRANSFER_IN, +amount)
#   accept (per item):
#     - CashboxLedger(source_transit, TRANSFER_OUT) → transit boşaltılır
#     - CashboxLedger(destination_account, TRANSFER_IN) → para geldi
#   reject (per item):
#     - CashboxLedger(source_transit, TRANSFER_OUT)
#     - CashboxLedger(source_bank_account, REVERSAL, parent=orijinal)
#
#   ── STOCK Akışı (FAZ 47) ────────────────────────────────────────
#   dispatch:
#     - StockService.record_exit(source_store, product, qty, reason=TRANSFER_OUT,
#                                 unit_cost_hs=source_snapshot.WAC) → kaynak stoğu düşer
#     - Kaynak StockSnapshot.stock_gram/pieces azalır, WAC değişmez
#   accept (per item):
#     - _resolve_destination_product → hedefte ayna ürün bul/oluştur
#     - StockService.record_entry(destination_store, dest_product, qty,
#                                  reason=TRANSFER_IN, unit_cost_hs=item.unit_cost_hs)
#     - Hedef StockSnapshot WAC ağırlıklı ortalama ile güncellenir
#     - source_stock_ledger ↔ destination_stock_ledger paired_entry ile bağlanır
#   reject (per item):
#     - StockService.record_entry(source_store, product, qty,
#                                  reason=TRANSFER_IN, unit_cost_hs=item.unit_cost_hs)
#       → kaynağa iade; orijinal WAC kopyası kullanılır (matematiksel olarak
#       kaynak WAC'ını koruma garantisi: A × X − Q × X + Q × X = A × X)
#
# ATOMICITY:
#   Tüm yazımlar transaction.atomic içinde gerçekleşir.
#
# WAC BÜTÜNLÜK GARANTİSİ:
#   - Kaynak şubenin WAC'ı item.unit_cost_hs alanına KOPYALANIR (DRAFT'ta).
#   - Hedef şubeye yazılırken bu KOPYA değer kullanılır (canlı WAC değil).
#   - Böylece dispatch ile accept arasında geçen sürede kaynakta yeni alımlar
#     olsa bile, transfer kalemi eski maliyetle (tarihsel sabit) hedeflenir.
# ============================================================================

import logging
from decimal import Decimal
from datetime import datetime
from django.db import transaction
from django.utils import timezone

from apps.store_transfers.models import StoreTransfer, StoreTransferItem
from apps.banking.models import BankAccount, CashboxLedger
from apps.stores.models import Stores
from apps.products.models import Products
from apps.stock_management.models import StockLedger, StockSnapshot
from apps.stock_management.services.stock_service import StockService

log = logging.getLogger(__name__)


# ============================================================================
# HATALAR
# ============================================================================

class TransferError(Exception):
    """Transfer akışındaki herhangi bir doğrulama veya iş kuralı hatası."""
    pass


class InvalidTransferStateError(TransferError):
    """Geçerli olmayan durum geçişi (örn. ACCEPTED'a tekrar accept)."""
    pass


# ============================================================================
# YARDIMCI FONKSİYONLAR
# ============================================================================

def generate_transfer_no() -> str:
    """TRF-YYYY-NNNN formatında benzersiz transfer numarası üretir.

    Yıl bazlı sıralı numara verir; aynı yıl içindeki son TRF numarasından
    bir fazlasını döner. Atomik değildir — race koşulunda nadir çakışma
    olabilir; o durumda yeniden çağırılması beklenir (DB unique constraint
    çakışmayı yakalar).
    """
    year = timezone.now().year
    prefix = f"TRF-{year}-"
    last = (
        StoreTransfer.objects
        .filter(transfer_no__startswith=prefix)
        .order_by('-transfer_no')
        .values_list('transfer_no', flat=True)
        .first()
    )
    if last:
        try:
            last_seq = int(last.split('-')[-1])
        except (ValueError, IndexError):
            last_seq = 0
    else:
        last_seq = 0
    return f"{prefix}{(last_seq + 1):04d}"


def get_transit_account(store: Stores, currency: str) -> BankAccount:
    """Şubenin verilen currency için transit kasasını döner.

    Transit hesaplar `banking/0011_create_transit_accounts.py` data
    migration'ı tarafından oluşturulur. Bulunamazsa otomatik üretilir
    (lazy creation — yeni eklenen şubeler için güvenli ağ).
    """
    transit, _created = BankAccount.objects.get_or_create(
        store=store,
        is_inter_branch_transit_account=True,
        currency=currency.upper(),
        defaults={
            'name': f'[TRANSIT-{currency.upper()}] {store.title or store.store_id or "Mağaza"}',
            'account_type': 'CASH',
            'reconciliation_tolerance': Decimal('0.00'),
            'is_active': False,
            'is_deleted': False,
        },
    )
    return transit


def _resolve_destination_account(store: Stores, currency: str) -> BankAccount:
    """Hedef şubede currency-eşleşen ilk standart CASH/BANK hesabını döner.

    Transit hesaplar elenir. Bulunamazsa hata fırlatılır.
    """
    qs = (
        BankAccount.objects
        .filter(
            store=store,
            currency=currency.upper(),
            is_active=True,
            is_deleted=False,
            is_inter_branch_transit_account=False,
        )
        .order_by('account_type', 'created_on')
    )
    account = qs.first()
    if account is None:
        raise TransferError(
            f"Hedef şube ({store}) için {currency} para biriminde aktif kasa hesabı bulunamadı. "
            "Önce hedef şubede bu currency için bir kasa tanımlayın."
        )
    return account


def _resolve_destination_product(source_product: Products, destination_store: Stores) -> Products:
    """Hedef şubede kaynak ürünün eşleşik ayna kaydını bulur veya oluşturur.

    Eşleşme stratejisi (sırasıyla denenir):
      1. Aynı barkodla eşleşme       (source.barcode → dest.barcode)
      2. Aynı isim+tip+milyem+gram   (gramajlı altın için tipik)
      3. Eşleşme yoksa: kaynak ürünü kopyalayıp store=destination ile yeni kayıt

    Bu strateji, kuyumcuların her şubede aynı ürünleri (22 Ayar, 14 Ayar vb.)
    tanımlamadan transfer yapabilmesini sağlar — hedef şubede ürün otomatik
    oluşur. Yeni kayıt soft-delete bayrağı temiz, is_completed=False, gram=0
    durumda gelir; stok hareketi accept'te yazılır.

    DİKKAT: Yeni Products kaydı oluştururken kaynak ürünün özel uzantı
    tablolarını (WatchDetail, DiamondDetail) KOPYALAMAZ. Bu durumda
    transfer kalemi reddedilir; barkodlu özel ürünler için patron önce
    hedef şubede manuel kayıt açmalıdır. Bu kısıt güvence içindir —
    DiamondStone gibi 4C alanlarının yanlışlıkla kopyalanması bilanço
    bozar.
    """
    if source_product.material_type in ('WATCH', 'DIAMOND'):
        # Yalnızca aynı barkod eşleşmesi kabul edilir; kopyalama yapılmaz.
        if source_product.barcode:
            existing = (
                Products.objects
                .filter(
                    store=destination_store,
                    barcode=source_product.barcode,
                    is_deleted=False,
                )
                .first()
            )
            if existing:
                return existing
        raise TransferError(
            f"WATCH/DIAMOND tipi ürünler ({source_product.name}) için hedef şubede "
            "aynı barkodlu kayıt bulunmalı. Lütfen önce hedefte ürünü tanımlayın."
        )

    # 1. Barkod eşleşmesi
    if source_product.barcode:
        match = (
            Products.objects
            .filter(
                store=destination_store,
                barcode=source_product.barcode,
                is_deleted=False,
            )
            .first()
        )
        if match:
            return match

    # 2. İsim + tip + milyem + gram eşleşmesi (gramajlı altın için)
    name_match = (
        Products.objects
        .filter(
            store=destination_store,
            name=source_product.name,
            material_type=source_product.material_type,
            product_mileage=source_product.product_mileage,
            is_deleted=False,
        )
        .first()
    )
    if name_match:
        return name_match

    # 3. Yeni kayıt: kaynaktan klonla
    new_product = Products(
        store=destination_store,
        name=source_product.name,
        barcode=source_product.barcode,
        material_type=source_product.material_type,
        gram=Decimal('0'),  # stok accept'te yazılacak
        product_mileage=source_product.product_mileage,
        gold_dry=source_product.gold_dry,
        gold_rate=source_product.gold_rate,
        category_id=getattr(source_product, 'category_id', None),
        # Fiyat alanları kopyalanmaz — hedef şube kendi fiyatını belirler
        is_active=True,
        is_deleted=False,
        is_completed=False,
        is_currency=getattr(source_product, 'is_currency', False),
    )
    # FAZ 27 ile gelen `display_order` varsayılanı korunsun
    if hasattr(source_product, 'display_order'):
        new_product.display_order = source_product.display_order or 0
    new_product.save()
    log.info(
        "TRANSFER: Hedef şubede ayna ürün kaydı oluşturuldu: %s (%s) → store %s",
        new_product.name, new_product.id, destination_store.id,
    )
    return new_product


def _refresh_transfer_totals(transfer: StoreTransfer) -> None:
    """StoreTransfer cache alanlarını TransferItem'lardan yeniden hesaplar.

    Otomatik transfer_type tespiti:
      - Sadece CASH item varsa  → CASH
      - Sadece STOCK item varsa → STOCK
      - Karışık                  → MIXED
    """
    items = list(transfer.items.all())

    cash_total = sum(
        (i.amount_eur_equivalent for i in items if i.item_type == StoreTransferItem.ItemType.CASH),
        Decimal('0'),
    )

    # STOCK için HS karşılığı: gramajlı için (gram × unit_cost_hs)
    # adet bazlı için (pieces × unit_cost_hs). Her ikisinde de unit_cost_hs WAC kopyasıdır.
    stock_hs = Decimal('0')
    for i in items:
        if i.item_type != StoreTransferItem.ItemType.STOCK:
            continue
        gram = i.quantity_gram or Decimal('0')
        pieces = i.quantity_pieces or 0
        wac = i.unit_cost_hs or Decimal('0')
        if gram > 0:
            stock_hs += gram * wac
        elif pieces > 0:
            stock_hs += Decimal(pieces) * wac

    has_cash = any(i.item_type == StoreTransferItem.ItemType.CASH for i in items)
    has_stock = any(i.item_type == StoreTransferItem.ItemType.STOCK for i in items)
    if has_cash and has_stock:
        new_type = StoreTransfer.TransferType.MIXED
    elif has_stock:
        new_type = StoreTransfer.TransferType.STOCK
    else:
        new_type = StoreTransfer.TransferType.CASH

    transfer.total_cash_tl_equivalent = cash_total
    transfer.total_stock_hs = stock_hs
    transfer.line_count = len(items)
    transfer.transfer_type = new_type
    transfer.save(update_fields=[
        'total_cash_tl_equivalent', 'total_stock_hs',
        'line_count', 'transfer_type', 'updated_on',
    ])


# ============================================================================
# ANA SERVİS SINIFI
# ============================================================================

class StoreTransferService:
    """Şubeler arası transfer orkestrasyon servisi.

    Tüm metodlar @staticmethod / @classmethod'tur — global state yok.
    Her yazım işlemi transaction.atomic içine alınır; çağıran view'ın
    ekstra atomic'e ihtiyacı yoktur.
    """

    # ----------------------------------------------------------------
    # DRAFT OLUŞTURMA
    # ----------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def create_draft(
        cls,
        *,
        source_store: Stores,
        destination_store: Stores,
        items_payload: list,
        initiated_by=None,
        notes_sender: str = '',
        expected_arrival_date=None,
    ) -> StoreTransfer:
        """DRAFT durumunda yeni transfer oluşturur.

        items_payload: list of dict; her dict aşağıdaki şemalardan birine uyar.

        CASH item:
            {
                'item_type': 'CASH',           # opsiyonel; varsayılan CASH
                'currency':  'TRY'|'USD'|...,
                'amount':    Decimal,
                'amount_eur': Decimal,
                'rate':      Decimal | None,
                'source_bank_account_id': UUID,
            }

        STOCK item:
            {
                'item_type':       'STOCK',
                'product_id':      UUID,                   # kaynak şubedeki ürün
                'quantity_pieces': int,                    # adet bazlı
                'quantity_gram':   Decimal,                # gram bazlı
                # unit_cost_hs/tl OPSİYONEL (verilmezse kaynak StockSnapshot
                # WAC'ı otomatik kopyalanır — tarihsel sabit kalır).
            }

        Returns:
            Oluşturulmuş StoreTransfer instance (status=DRAFT, transfer_no=NULL).
        """
        # --- Doğrulamalar ---
        if source_store.id == destination_store.id:
            raise TransferError("Kaynak ve hedef şube aynı olamaz.")
        if not source_store.is_branch_active:
            raise TransferError("Kaynak şube operasyonel değil.")
        if not destination_store.is_branch_active:
            raise TransferError("Hedef şube operasyonel değil.")
        if not destination_store.allows_inbound_transfer:
            raise TransferError("Hedef şube gelen transferleri kabul etmiyor.")
        if not items_payload:
            raise TransferError("Transferde en az bir kalem bulunmalı.")

        # --- Header oluştur (transfer_type _refresh_transfer_totals'ta otomatik düzelir) ---
        transfer = StoreTransfer.objects.create(
            source_store=source_store,
            destination_store=destination_store,
            transfer_type=StoreTransfer.TransferType.CASH,  # placeholder — refresh düzeltir
            status=StoreTransfer.Status.DRAFT,
            initiated_by=initiated_by,
            notes_sender=notes_sender or '',
            expected_arrival_date=expected_arrival_date,
        )

        # --- Items oluştur ---
        for raw in items_payload:
            kind = (raw.get('item_type') or 'CASH').upper()
            if kind == 'CASH':
                cls._build_cash_item(raw, transfer, source_store)
            elif kind == 'STOCK':
                cls._build_stock_item(raw, transfer, source_store)
            else:
                raise TransferError(f"Bilinmeyen kalem tipi: {kind!r}")

        _refresh_transfer_totals(transfer)
        log.info(
            "TRANSFER DRAFT created: id=%s source=%s dest=%s items=%d",
            transfer.id, source_store.id, destination_store.id, len(items_payload),
        )
        return transfer

    @staticmethod
    def _build_cash_item(raw: dict, transfer: StoreTransfer, source_store: Stores) -> StoreTransferItem:
        """CASH kalemi doğrular ve oluşturur (DRAFT aşamasında)."""
        currency = (raw.get('currency') or '').upper()
        if currency not in ('TRY', 'USD', 'EUR', 'GBP', 'HS'):
            raise TransferError(f"Geçersiz para birimi: {currency!r}")
        try:
            amount = Decimal(str(raw.get('amount', 0)))
        except Exception:
            raise TransferError("Geçersiz tutar.")
        if amount <= 0:
            raise TransferError("Tutar pozitif olmalı.")

        sba_id = raw.get('source_bank_account_id')
        if not sba_id:
            raise TransferError("Kaynak kasa seçilmedi.")
        try:
            sba = BankAccount.objects.get(id=sba_id)
        except BankAccount.DoesNotExist:
            raise TransferError("Kaynak kasa bulunamadı.")
        if sba.store_id != source_store.id:
            raise TransferError("Kaynak kasa, kaynak şubeye ait değil.")
        if sba.is_inter_branch_transit_account:
            raise TransferError("Transit hesap kaynak olarak seçilemez.")
        if (sba.currency or '').upper() != currency:
            raise TransferError(
                f"Kasa para birimi ({sba.currency}) ile kalem para birimi ({currency}) eşleşmiyor."
            )
        # Bakiye kontrolü dispatch sırasında yapılır (DRAFT'ta esnek tutuluyor).

        amount_eur = Decimal(str(raw.get('amount_eur', amount if currency == 'TRY' else 0)))
        rate = raw.get('rate')
        rate_dec = Decimal(str(rate)) if rate not in (None, '', 0) else None

        return StoreTransferItem.objects.create(
            transfer=transfer,
            item_type=StoreTransferItem.ItemType.CASH,
            currency=currency,
            amount=amount,
            amount_eur_equivalent=amount_eur,
            exchange_rate_at_dispatch=rate_dec,
            source_bank_account_id=sba_id,
            item_status=StoreTransferItem.ItemStatus.PENDING,
        )

    @staticmethod
    def _build_stock_item(raw: dict, transfer: StoreTransfer, source_store: Stores) -> StoreTransferItem:
        """STOCK kalemi doğrular ve oluşturur (DRAFT aşamasında).

        Kaynak şubedeki ürünün WAC'ı (StockSnapshot.weighted_avg_cost_hs/tl)
        TransferItem'a kopyalanır — bu tarihsel sabit kalır ve hedef WAC
        hesaplamasının girdisi olur.
        """
        product_id = raw.get('product_id')
        if not product_id:
            raise TransferError("Stok kalemi için ürün seçilmedi.")

        try:
            product = Products.objects.get(id=product_id, is_deleted=False)
        except Products.DoesNotExist:
            raise TransferError("Ürün bulunamadı veya silinmiş.")

        if product.store_id != source_store.id:
            raise TransferError(
                f"Ürün ({product.name}) kaynak şubeye ait değil. "
                "Yalnızca kendi mağazanızdaki ürünleri transfer edebilirsiniz."
            )
        if getattr(product, 'is_currency', False):
            raise TransferError("Döviz/para ürünleri stok transferi olarak gönderilemez.")

        try:
            qty_pieces = int(raw.get('quantity_pieces') or 0)
        except (TypeError, ValueError):
            raise TransferError("Adet sayısal olmalı.")
        try:
            qty_gram = Decimal(str(raw.get('quantity_gram') or 0))
        except Exception:
            raise TransferError("Gram sayısal olmalı.")

        if qty_pieces < 0 or qty_gram < 0:
            raise TransferError("Negatif miktar girilemez.")
        if qty_pieces == 0 and qty_gram == 0:
            raise TransferError("Adet veya gram'dan en az biri pozitif olmalı.")

        # Kaynak StockSnapshot — hem stok kontrolü hem WAC kopyası için
        try:
            snap = StockSnapshot.objects.get(store=source_store, product=product)
        except StockSnapshot.DoesNotExist:
            raise TransferError(
                f"Bu ürün ({product.name}) kaynak şubede stokta görünmüyor."
            )

        if qty_gram > 0 and snap.stock_gram < qty_gram:
            raise TransferError(
                f"Yetersiz gram stok: mevcut {snap.stock_gram}, gereken {qty_gram}."
            )
        if qty_pieces > 0 and snap.stock_pieces < qty_pieces:
            raise TransferError(
                f"Yetersiz adet stok: mevcut {snap.stock_pieces}, gereken {qty_pieces}."
            )

        # WAC kopyası — kullanıcı override ederse o, yoksa snapshot WAC
        unit_cost_hs = raw.get('unit_cost_hs')
        unit_cost_eur = raw.get('unit_cost_eur')
        if unit_cost_hs is None:
            unit_cost_hs = snap.weighted_avg_cost_hs or Decimal('0')
        else:
            unit_cost_hs = Decimal(str(unit_cost_hs))
        if unit_cost_eur is None:
            unit_cost_eur = snap.weighted_avg_cost_eur or Decimal('0')
        else:
            unit_cost_eur = Decimal(str(unit_cost_eur))

        return StoreTransferItem.objects.create(
            transfer=transfer,
            item_type=StoreTransferItem.ItemType.STOCK,
            product=product,
            quantity_pieces=qty_pieces,
            quantity_gram=qty_gram,
            unit_cost_hs=unit_cost_hs,
            unit_cost_eur=unit_cost_eur,
            item_status=StoreTransferItem.ItemStatus.PENDING,
        )

    # ----------------------------------------------------------------
    # DISPATCH (DRAFT → IN_TRANSIT)
    # ----------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def dispatch_transfer(cls, transfer: StoreTransfer, *, dispatched_by=None) -> StoreTransfer:
        """Transferi yola çıkarır: kaynak kasalar düşülür, transit hesaplara aktarılır.

        Kontroller:
            - status == DRAFT olmalı
            - Her CASH item için source_bank_account dolu olmalı
            - Kaynak kasa bakiyesi yeterli olmalı (kontrol günün kuruyla)

        Yazımlar (her item için):
            1. CashboxLedger(source_bank_account, TRANSFER_OUT, amount)
               → balance_snapshot güncellenir
            2. CashboxLedger(source_transit, TRANSFER_IN, amount)
               → bilanço korunur (kasa - amount, transit + amount)
        """
        if transfer.status != StoreTransfer.Status.DRAFT:
            raise InvalidTransferStateError(
                f"Transfer dispatch için DRAFT durumunda olmalı, mevcut: {transfer.status}"
            )

        items = list(transfer.items.all())
        if not items:
            raise TransferError("Boş transfer yola çıkarılamaz.")

        # Transfer no üret
        if not transfer.transfer_no:
            transfer.transfer_no = generate_transfer_no()

        # Her item için ledger yazımı
        for item in items:
            if item.item_type == StoreTransferItem.ItemType.CASH:
                cls._dispatch_cash_item(item, transfer, dispatched_by)
            elif item.item_type == StoreTransferItem.ItemType.STOCK:
                cls._dispatch_stock_item(item, transfer, dispatched_by)
            else:
                raise TransferError(f"Bilinmeyen kalem tipi: {item.item_type!r}")

        # Header güncelle
        transfer.status = StoreTransfer.Status.IN_TRANSIT
        transfer.dispatched_at = timezone.now()
        transfer.dispatched_by = dispatched_by
        transfer.save(update_fields=[
            'transfer_no', 'status', 'dispatched_at', 'dispatched_by', 'updated_on',
        ])

        log.info("TRANSFER DISPATCHED: %s (%d kalem)", transfer.transfer_no, len(items))
        return transfer

    @staticmethod
    def _dispatch_cash_item(item: StoreTransferItem, transfer: StoreTransfer, user) -> None:
        sba = item.source_bank_account
        if sba is None:
            raise TransferError(f"Kalem {item.id} için kaynak kasa boş.")
        if sba.store_id != transfer.source_store_id:
            raise TransferError("Kaynak kasa kaynak şubeye ait değil.")

        # Bakiye kontrolü
        balance = sba.get_balance(currency=item.currency)
        if balance < item.amount:
            raise TransferError(
                f"Kaynak kasa bakiyesi yetersiz ({sba.name}): "
                f"mevcut {balance} {item.currency}, gereken {item.amount}"
            )

        # 1. Kaynak kasadan TRANSFER_OUT
        new_balance_source = balance - item.amount
        source_entry = CashboxLedger.objects.create(
            cashbox=sba,
            store=transfer.source_store,
            movement_type=CashboxLedger.MovementType.TRANSFER_OUT,
            amount=item.amount,
            currency=item.currency,
            amount_eur_equivalent=item.amount_eur_equivalent or Decimal('0'),
            exchange_rate=item.exchange_rate_at_dispatch,
            balance_snapshot=new_balance_source,
            process_no=transfer.transfer_no,
            description=f"Transfer çıkış → {transfer.destination_store}",
            created_by=user,
        )

        # 2. Transit hesaba TRANSFER_IN (kaynak şube transit)
        transit = get_transit_account(transfer.source_store, item.currency)
        transit_balance = transit.get_balance(currency=item.currency)
        new_transit_balance = transit_balance + item.amount
        CashboxLedger.objects.create(
            cashbox=transit,
            store=transfer.source_store,
            movement_type=CashboxLedger.MovementType.TRANSFER_IN,
            amount=item.amount,
            currency=item.currency,
            amount_eur_equivalent=item.amount_eur_equivalent or Decimal('0'),
            exchange_rate=item.exchange_rate_at_dispatch,
            balance_snapshot=new_transit_balance,
            process_no=transfer.transfer_no,
            description=f"Transit (yolda): {transfer.transfer_no}",
            created_by=user,
        )

        item.source_cashbox_ledger = source_entry
        item.save(update_fields=['source_cashbox_ledger', 'updated_on'])

    @staticmethod
    def _dispatch_stock_item(item: StoreTransferItem, transfer: StoreTransfer, user) -> None:
        """STOCK kalemini yola çıkar: kaynak stoktan TRANSFER_OUT yazılır.

        StockService.record_exit kullanılır; WAC otomatik korunur (cikis WAC
        değiştirmez). source_stock_ledger FK'ye dönen ledger bağlanır.
        """
        if item.product is None:
            raise TransferError(f"Stok kalemi {item.id} için ürün boş.")
        if item.product.store_id != transfer.source_store_id:
            raise TransferError("Ürün artık kaynak şubeye ait değil (taşınmış olabilir).")

        # Kaynak StockSnapshot kontrolü (race koşulu için tekrar bakılır)
        try:
            snap = StockSnapshot.objects.select_for_update().get(
                store=transfer.source_store, product=item.product,
            )
        except StockSnapshot.DoesNotExist:
            raise TransferError(
                f"Stok bulunamadı: {item.product.name} kaynak şubede yok."
            )

        qty_gram = item.quantity_gram or Decimal('0')
        qty_pieces = item.quantity_pieces or 0

        if qty_gram > 0 and snap.stock_gram < qty_gram:
            raise TransferError(
                f"Yetersiz gram stok ({item.product.name}): "
                f"mevcut {snap.stock_gram}, gereken {qty_gram}."
            )
        if qty_pieces > 0 and snap.stock_pieces < qty_pieces:
            raise TransferError(
                f"Yetersiz adet stok ({item.product.name}): "
                f"mevcut {snap.stock_pieces}, gereken {qty_pieces}."
            )

        ledger_out = StockService.record_exit(
            product=item.product,
            store=transfer.source_store,
            quantity_gram=qty_gram,
            quantity_pieces=qty_pieces,
            reason=StockLedger.Reason.TRANSFER_OUT,
            ref_type='store_transfer',
            ref_id=str(transfer.id),
            unit_cost_hs=item.unit_cost_hs,
            unit_cost_eur=item.unit_cost_eur,
            user=user,
            notes=(
                f"Şubeler arası transfer çıkış → {transfer.destination_store} "
                f"({transfer.transfer_no})"
            ),
        )
        item.source_stock_ledger = ledger_out
        item.save(update_fields=['source_stock_ledger', 'updated_on'])

    # ----------------------------------------------------------------
    # ACCEPT (IN_TRANSIT → ACCEPTED)
    # ----------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def accept_transfer(
        cls,
        transfer: StoreTransfer,
        *,
        accepted_by=None,
        notes_receiver: str = '',
        partial_decisions: dict = None,
    ) -> StoreTransfer:
        """Hedef şubenin gelen transferi kabul etmesi.

        partial_decisions: {item_id: {'accept': bool, 'reason': str}} biçiminde
            kalem-bazlı kabul/red kararı. None ise tüm kalemler ACCEPTED olur.

        Yazımlar (kabul edilen her kalem için):
            1. CashboxLedger(source_transit, TRANSFER_OUT) → transit boşaltılır
            2. CashboxLedger(destination_account, TRANSFER_IN) → para hedefe ulaştı

        Reddedilen kalemler için kaynağa REVERSAL akışı işletilir.
        """
        if transfer.status != StoreTransfer.Status.IN_TRANSIT:
            raise InvalidTransferStateError(
                f"Transfer accept için IN_TRANSIT durumunda olmalı, mevcut: {transfer.status}"
            )

        items = list(transfer.items.all())
        any_rejected = False
        for item in items:
            decision = (partial_decisions or {}).get(str(item.id), {'accept': True})
            should_accept = decision.get('accept', True)
            reason = decision.get('reason', '') or 'Hedef şube reddetti.'

            if item.item_type == StoreTransferItem.ItemType.CASH:
                if should_accept:
                    cls._accept_cash_item(item, transfer, accepted_by)
                else:
                    cls._reject_cash_item(item, transfer, accepted_by, reason=reason)
                    any_rejected = True
            elif item.item_type == StoreTransferItem.ItemType.STOCK:
                if should_accept:
                    cls._accept_stock_item(item, transfer, accepted_by)
                else:
                    cls._reject_stock_item(item, transfer, accepted_by, reason=reason)
                    any_rejected = True
            else:
                raise TransferError(f"Bilinmeyen kalem tipi: {item.item_type!r}")

        # Status hesapla
        if any_rejected:
            transfer.status = StoreTransfer.Status.PARTIALLY_ACCEPTED
        else:
            transfer.status = StoreTransfer.Status.ACCEPTED

        transfer.completed_at = timezone.now()
        transfer.accepted_by = accepted_by
        transfer.notes_receiver = (notes_receiver or '').strip()
        transfer.save(update_fields=[
            'status', 'completed_at', 'accepted_by', 'notes_receiver', 'updated_on',
        ])

        log.info("TRANSFER ACCEPTED: %s status=%s", transfer.transfer_no, transfer.status)
        return transfer

    @staticmethod
    def _accept_cash_item(item: StoreTransferItem, transfer: StoreTransfer, user) -> None:
        # Hedef kasa çözümlemesi
        if item.destination_bank_account_id is None:
            dest_account = _resolve_destination_account(
                transfer.destination_store, item.currency,
            )
        else:
            dest_account = item.destination_bank_account
            if dest_account.store_id != transfer.destination_store_id:
                raise TransferError("Hedef kasa hedef şubeye ait değil.")
            if dest_account.is_inter_branch_transit_account:
                raise TransferError("Transit hesap, hedef olarak seçilemez.")

        amount = item.amount
        currency = item.currency

        # 1. Source transit'ten TRANSFER_OUT
        source_transit = get_transit_account(transfer.source_store, currency)
        transit_bal = source_transit.get_balance(currency=currency)
        CashboxLedger.objects.create(
            cashbox=source_transit,
            store=transfer.source_store,
            movement_type=CashboxLedger.MovementType.TRANSFER_OUT,
            amount=amount,
            currency=currency,
            amount_eur_equivalent=item.amount_eur_equivalent or Decimal('0'),
            exchange_rate=item.exchange_rate_at_dispatch,
            balance_snapshot=transit_bal - amount,
            process_no=transfer.transfer_no,
            description=f"Transit boşaltma (hedef kabul etti): {transfer.transfer_no}",
            created_by=user,
        )

        # 2. Hedef kasaya TRANSFER_IN
        dest_bal = dest_account.get_balance(currency=currency)
        dest_entry = CashboxLedger.objects.create(
            cashbox=dest_account,
            store=transfer.destination_store,
            movement_type=CashboxLedger.MovementType.TRANSFER_IN,
            amount=amount,
            currency=currency,
            amount_eur_equivalent=item.amount_eur_equivalent or Decimal('0'),
            exchange_rate=item.exchange_rate_at_dispatch,
            balance_snapshot=dest_bal + amount,
            process_no=transfer.transfer_no,
            description=f"Transfer giriş ← {transfer.source_store}",
            created_by=user,
        )

        item.destination_bank_account = dest_account
        item.destination_cashbox_ledger = dest_entry
        item.item_status = StoreTransferItem.ItemStatus.ACCEPTED
        item.accepted_amount = amount
        item.save(update_fields=[
            'destination_bank_account', 'destination_cashbox_ledger',
            'item_status', 'accepted_amount', 'updated_on',
        ])

    @staticmethod
    def _accept_stock_item(item: StoreTransferItem, transfer: StoreTransfer, user) -> None:
        """STOCK kalemini hedef şubede kabul et: ayna ürün çözülür, hedefte
        record_entry yazılır (WAC ağırlıklı ortalama ile güncellenir),
        paired_entry source ↔ destination olarak bağlanır.
        """
        if item.product is None:
            raise TransferError(f"Stok kalemi {item.id} için ürün bilgisi kayıp.")
        if item.source_stock_ledger is None:
            raise TransferError(
                f"Stok kalemi {item.id} dispatch sırasında ledger yazımı yapılmamış."
            )

        # Hedef şubedeki ayna ürünü çöz (gerekirse oluşturur)
        dest_product = _resolve_destination_product(item.product, transfer.destination_store)

        qty_gram = item.quantity_gram or Decimal('0')
        qty_pieces = item.quantity_pieces or 0

        ledger_in = StockService.record_entry(
            product=dest_product,
            store=transfer.destination_store,
            quantity_gram=qty_gram,
            quantity_pieces=qty_pieces,
            reason=StockLedger.Reason.TRANSFER_IN,
            ref_type='store_transfer',
            ref_id=str(transfer.id),
            unit_cost_hs=item.unit_cost_hs,
            unit_cost_eur=item.unit_cost_eur,
            user=user,
            notes=(
                f"Şubeler arası transfer giriş ← {transfer.source_store} "
                f"({transfer.transfer_no})"
            ),
        )

        # paired_entry: source TRANSFER_OUT ↔ destination TRANSFER_IN
        # StockLedger normalde immutable; ancak save() sadece paired_entry
        # alanına izin veriyor (model bunu açıkça destekliyor).
        try:
            ledger_in.paired_entry = item.source_stock_ledger
            ledger_in.save(update_fields=['paired_entry'])
        except Exception as exc:
            log.warning(
                "TRANSFER paired_entry yazımı başarısız (item=%s): %s",
                item.id, exc,
            )

        # Kaynaktaki OUT satırının paired_entry'sini de geri-yönlü bağla
        try:
            src = item.source_stock_ledger
            src.paired_entry = ledger_in
            src.save(update_fields=['paired_entry'])
        except Exception as exc:
            log.warning(
                "TRANSFER source paired_entry yazımı başarısız: %s", exc,
            )

        item.destination_stock_ledger = ledger_in
        item.item_status = StoreTransferItem.ItemStatus.ACCEPTED
        item.accepted_pieces = qty_pieces
        item.accepted_gram = qty_gram
        item.save(update_fields=[
            'destination_stock_ledger', 'item_status',
            'accepted_pieces', 'accepted_gram', 'updated_on',
        ])

    # ----------------------------------------------------------------
    # REJECT (IN_TRANSIT → REJECTED)
    # ----------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def reject_transfer(
        cls,
        transfer: StoreTransfer,
        *,
        rejected_by=None,
        reason: str = '',
    ) -> StoreTransfer:
        """Tüm kalemleri reddeder; kaynağa geri iade akışı."""
        if transfer.status != StoreTransfer.Status.IN_TRANSIT:
            raise InvalidTransferStateError(
                f"Transfer reject için IN_TRANSIT durumunda olmalı, mevcut: {transfer.status}"
            )
        if not (reason or '').strip():
            raise TransferError("Red sebebi zorunludur.")

        items = list(transfer.items.all())
        for item in items:
            if item.item_type == StoreTransferItem.ItemType.CASH:
                cls._reject_cash_item(item, transfer, rejected_by, reason=reason)
            elif item.item_type == StoreTransferItem.ItemType.STOCK:
                cls._reject_stock_item(item, transfer, rejected_by, reason=reason)
            else:
                raise TransferError(f"Bilinmeyen kalem tipi: {item.item_type!r}")

        transfer.status = StoreTransfer.Status.REJECTED
        transfer.is_reversed = True
        transfer.completed_at = timezone.now()
        transfer.rejected_by = rejected_by
        transfer.notes_receiver = reason.strip()
        transfer.save(update_fields=[
            'status', 'is_reversed', 'completed_at', 'rejected_by',
            'notes_receiver', 'updated_on',
        ])

        log.info("TRANSFER REJECTED: %s reason=%s", transfer.transfer_no, reason)
        return transfer

    @staticmethod
    def _reject_cash_item(item: StoreTransferItem, transfer: StoreTransfer, user, *, reason: str) -> None:
        amount = item.amount
        currency = item.currency
        original_source = item.source_bank_account
        original_ledger = item.source_cashbox_ledger

        # 1. Source transit'ten TRANSFER_OUT (transit boşaltılır)
        source_transit = get_transit_account(transfer.source_store, currency)
        transit_bal = source_transit.get_balance(currency=currency)
        CashboxLedger.objects.create(
            cashbox=source_transit,
            store=transfer.source_store,
            movement_type=CashboxLedger.MovementType.TRANSFER_OUT,
            amount=amount,
            currency=currency,
            amount_eur_equivalent=item.amount_eur_equivalent or Decimal('0'),
            exchange_rate=item.exchange_rate_at_dispatch,
            balance_snapshot=transit_bal - amount,
            process_no=transfer.transfer_no,
            description=f"Transit iadesi (red): {transfer.transfer_no}",
            created_by=user,
        )

        # 2. Kaynak kasaya REVERSAL (parent: orijinal TRANSFER_OUT)
        if original_source is None:
            raise TransferError("Kaynak kasa kaybolmuş; reversal yazılamaz.")
        src_bal = original_source.get_balance(currency=currency)
        CashboxLedger.objects.create(
            cashbox=original_source,
            store=transfer.source_store,
            movement_type=CashboxLedger.MovementType.REVERSAL,
            amount=amount,
            currency=currency,
            amount_eur_equivalent=item.amount_eur_equivalent or Decimal('0'),
            exchange_rate=item.exchange_rate_at_dispatch,
            balance_snapshot=src_bal + amount,  # Reversal kasayı arttırır (giriş yönlü)
            parent=original_ledger,
            process_no=transfer.transfer_no,
            description=f"Transfer reddedildi, iade: {reason[:120]}",
            created_by=user,
        )

        item.item_status = StoreTransferItem.ItemStatus.REJECTED
        item.accepted_amount = Decimal('0')
        item.rejection_reason = (reason or '')[:255]
        item.save(update_fields=[
            'item_status', 'accepted_amount', 'rejection_reason', 'updated_on',
        ])

    @staticmethod
    def _reject_stock_item(item: StoreTransferItem, transfer: StoreTransfer, user, *, reason: str) -> None:
        """STOCK kalemini reddet: kaynağa geri stok yazılır.

        StockService.record_entry kullanılır; reason=TRANSFER_IN.
        unit_cost_hs olarak orijinal item.unit_cost_hs (kaynaktan kopyalanan
        WAC) geçilir. Bu sayede kaynak şubenin WAC'ı matematiksel olarak
        korunur:
            Önce: A_gram × A_WAC
            Dispatch: (A_gram − Q) × A_WAC
            Reject  : (A_gram − Q + Q) × ((A_gram − Q) × A_WAC + Q × A_WAC) / A_gram
                    = A_gram × A_WAC  ✓
        """
        if item.product is None:
            raise TransferError(f"Stok kalemi {item.id} için ürün bilgisi kayıp.")

        qty_gram = item.quantity_gram or Decimal('0')
        qty_pieces = item.quantity_pieces or 0

        ledger_back = StockService.record_entry(
            product=item.product,
            store=transfer.source_store,
            quantity_gram=qty_gram,
            quantity_pieces=qty_pieces,
            reason=StockLedger.Reason.TRANSFER_IN,
            ref_type='store_transfer_reject',
            ref_id=str(transfer.id),
            unit_cost_hs=item.unit_cost_hs,
            unit_cost_eur=item.unit_cost_eur,
            user=user,
            notes=(
                f"Transfer reddedildi, stok iade edildi: {reason[:120]} "
                f"({transfer.transfer_no})"
            ),
        )

        # paired_entry: ret iade satırını orijinal OUT'a bağla (audit zinciri)
        if item.source_stock_ledger:
            try:
                ledger_back.paired_entry = item.source_stock_ledger
                ledger_back.save(update_fields=['paired_entry'])
            except Exception as exc:
                log.warning("TRANSFER reject paired_entry yazımı başarısız: %s", exc)

        item.item_status = StoreTransferItem.ItemStatus.REJECTED
        item.accepted_pieces = 0
        item.accepted_gram = Decimal('0')
        item.rejection_reason = (reason or '')[:255]
        item.save(update_fields=[
            'item_status', 'accepted_pieces', 'accepted_gram',
            'rejection_reason', 'updated_on',
        ])

    # ----------------------------------------------------------------
    # CANCEL (DRAFT → CANCELLED)
    # ----------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def cancel_draft(cls, transfer: StoreTransfer, *, cancelled_by=None, reason: str = '') -> StoreTransfer:
        """Sadece DRAFT durumundaki transferi iptal eder.
        Ledger'a hiçbir şey yazılmadığından temiz iptal."""
        if transfer.status != StoreTransfer.Status.DRAFT:
            raise InvalidTransferStateError(
                "Yalnızca DRAFT durumundaki transfer iptal edilebilir. "
                "Yola çıkmış transferler için 'Reject' kullanılır."
            )
        transfer.status = StoreTransfer.Status.CANCELLED
        transfer.completed_at = timezone.now()
        transfer.notes_admin = (reason or '').strip()
        transfer.save(update_fields=['status', 'completed_at', 'notes_admin', 'updated_on'])
        log.info("TRANSFER CANCELLED (draft): %s", transfer.id)
        return transfer
