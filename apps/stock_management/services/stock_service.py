"""
StockService - Tum stok giris/cikis islemleri icin tek giris noktasi.
===============================================================================

KURAL: Hicbir view, signal veya admin kodu StockSnapshot'a dogrudan yazmamalidir.
Tum stok degisiklikleri SADECE bu servis uzerinden yapilir.

Her islem sunlari yapar:
    1. StockSnapshot satirini select_for_update() ile kilitler
    2. Yeterlilik/gecerlilik kontrolu yapar
    3. StockLedger'a degismez bir satir yazar
    4. StockSnapshot'i gunceller (WAC dahil)
    5. Transaction basarili olursa commit, basarisiz olursa rollback

Race Condition korumalari:
    - select_for_update(): PostgreSQL satir kilidi
    - transaction.atomic(): Ya hepsi ya hicbiri
    - CheckConstraint: Veritabani seviyesinde negatif stok engeli (son savunma)
"""

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.core.cache import cache
from django.db import transaction

from apps.stock_management.models import StockLedger, StockSnapshot

logger = logging.getLogger('stock_management')


def _invalidate_dashboard_assets_cache(store):
    """
    Dashboard 'Magaza Varliklari' ozet cache'ini invalide eder.

    Cache key: dashboard_assets_summary:{store.id}
    Tetikleyici: Her StockSnapshot degisikligi (record_entry, record_exit,
    adjustment) sonrasinda cagirilir. Boylece Dashboard AJAX fetch'i daima
    guncel veriyi gorur (10 dakikalik TTL'e takilmadan).

    Bu fonksiyon hata firlatmaz - cache katmani bagimsizliga engel olmasin.
    """
    try:
        if store is None:
            return
        store_id = getattr(store, 'id', None)
        if store_id:
            cache.delete(f"dashboard_assets_summary:{store_id}")
    except Exception as _exc:
        # Cache invalidation hatasi asla islemi patlatmamali
        logger.warning(f"Dashboard assets cache invalidation gecti: {_exc}")


# ============================================================================
# OZEL HATA SINIFLARI
# ============================================================================

class InsufficientStockError(Exception):
    """
    Stok yetersizligi hatasi.
    Kullaniciya gosterilecek is hatasidir, 500 degil 400 donmelidir.
    """

    def __init__(self, product_name: str, available: Decimal, requested: Decimal, unit: str = 'g'):
        self.product_name = product_name
        self.available = available
        self.requested = requested
        self.unit = unit
        self.deficit = requested - available
        super().__init__(
            f"Yetersiz stok: '{product_name}' icin "
            f"mevcut={available}{unit}, talep={requested}{unit}, "
            f"eksik={self.deficit}{unit}"
        )


class StockIntegrityError(Exception):
    """
    Stok butunluk hatasi.
    Snapshot ile Ledger SUM arasinda tutarsizlik tespit edildiginde firlatilir.
    """
    pass


# ============================================================================
# YARDIMCI FONKSIYONLAR
# ============================================================================

def _q4(value: Decimal) -> Decimal:
    """4 ondalik haneye yuvarla (gram ve Has islemleri icin)."""
    return (value or Decimal('0')).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def _q2(value: Decimal) -> Decimal:
    """2 ondalik haneye yuvarla (TL islemleri icin)."""
    return (value or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


# ============================================================================
# FAZ B: MATERIAL_TYPE GUVENLIK DUVARI (VALIDATION)
# ============================================================================

# Gram ile islem gormeyecek (adet bazli) materyal tipleri
_PIECE_ONLY_MATERIALS = frozenset({'WATCH', 'DIAMOND'})

# Metal bazli (gram + milyem) materyal tipleri
_METAL_BASED_MATERIALS = frozenset({'GOLD', 'SILVER'})


def _validate_material_type_quantities(
    product,
    quantity_gram: Decimal,
    quantity_pieces: int,
) -> str:
    """
    FAZ B Guvenlik Duvari: material_type bazli miktar dogrulamasi.

    Kurallar:
      - WATCH / DIAMOND : quantity_gram == 0 ZORUNLU, quantity_pieces >= 1 ZORUNLU.
                          Saat ve pirlanta adet bazlidir; gram kavrami yoktur.
      - GOLD            : Mevcut altin akisi. Gram VEYA pieces olabilir
                          (en az biri > 0, mevcut record_entry/exit bunu kontrol eder).
      - SILVER          : Gumus. Altin ile ayni alan yapisini kullanir ama
                          WAC/maliyetler HG (Has Gumus) biriminden yorumlanmalidir.
                          Bu fonksiyon SILVER icin yalnizca log notu birakir;
                          asil HG ledger yonlendirmesi get_ledger_currency() ile yapilir.

    Args:
        product: Products instance (material_type alani okunur)
        quantity_gram: Gram miktari (Decimal)
        quantity_pieces: Adet miktari (int)

    Returns:
        str: Normalize edilmis material_type ('GOLD', 'SILVER', 'WATCH', 'DIAMOND').

    Raises:
        ValueError: material_type kurali ihlal edilmisse.
    """
    mat_type = getattr(product, 'material_type', None) or 'GOLD'
    mat_type = str(mat_type).upper()

    product_label = getattr(product, 'name', None) or getattr(product, 'id', '<bilinmeyen>')

    if mat_type in _PIECE_ONLY_MATERIALS:
        # WATCH / DIAMOND: gram SIFIR olmali, adet EN AZ 1 olmali.
        if quantity_gram and Decimal(str(quantity_gram)) != Decimal('0'):
            raise ValueError(
                f"material_type='{mat_type}' urunlerinde quantity_gram=0 olmalidir. "
                f"Verilen deger: {quantity_gram}. "
                f"'{product_label}' adet bazli bir urundur (Saat/Pirlanta); "
                f"gram bazli stok hareketi yapilamaz."
            )
        if int(quantity_pieces or 0) < 1:
            raise ValueError(
                f"material_type='{mat_type}' urunlerinde quantity_pieces en az 1 olmalidir. "
                f"Verilen deger: {quantity_pieces}. "
                f"'{product_label}' icin adet belirtmelisiniz."
            )
    elif mat_type == 'SILVER':
        # SILVER: mevcut gram/milyem alan yapisi kullanilir; ledger HG.
        logger.debug(
            f"SILVER material_type islem goruyor: product='{product_label}', "
            f"gram={quantity_gram}, pieces={quantity_pieces}. "
            f"Maliyetler HG (Has Gumus) biriminden yorumlanir; SupplierLedger "
            f"get_ledger_currency() ile 'HG' olarak yazilmalidir."
        )
    elif mat_type == 'GOLD':
        # GOLD: mevcut davranis. Ekstra kontrol yok - record_entry/exit kendi
        # jenerik kontrollerini uygular.
        pass
    else:
        # Bilinmeyen material_type — default GOLD davranisina dus ama uyari log'la.
        logger.warning(
            f"Bilinmeyen material_type='{mat_type}' (product='{product_label}'). "
            f"GOLD varsayilan davranisi uygulaniyor."
        )
        mat_type = 'GOLD'

    return mat_type


# ============================================================================
# ANA SERVIS SINIFI
# ============================================================================

class StockService:
    """
    Stok giris ve cikis islemleri icin merkezi servis katmani.

    Kullanim ornekleri:

        # 1. Tedarikci alimi (stok girisi):
        StockService.record_entry(
            product=hurda_product,
            store=magaza,
            quantity_gram=Decimal('500.000'),
            reason=StockLedger.Reason.PURCHASE,
            ref_type='process',
            ref_id=str(process.id),
            unit_cost_hs=Decimal('0.9166'),
            hs_rate_tl=Decimal('3250.00'),
            user=request.user,
        )

        # 2. Satis (stok cikisi):
        StockService.record_exit(
            product=barkodlu_urun,
            store=magaza,
            quantity_pieces=1,
            quantity_gram=Decimal('3.500'),
            reason=StockLedger.Reason.SALE,
            ref_type='process',
            ref_id=str(process.id),
            user=request.user,
        )
    """

    @classmethod
    @transaction.atomic
    def record_entry(
        cls,
        *,
        product,
        store,
        quantity_gram: Decimal = Decimal('0.0000'),
        quantity_pieces: int = 0,
        reason: str,
        ref_type: str,
        ref_id: str,
        unit_cost_hs: Decimal = Decimal('0.0000'),
        unit_cost_tl: Decimal = Decimal('0.00'),
        hs_rate_tl: Decimal = Decimal('0.0000'),
        user=None,
        notes: str = '',
    ) -> StockLedger:
        """
        Stok GIRISI: Alis, iade, acilis sayimi, donusum girisi vb.

        Islem adimalari:
            1. StockSnapshot'i kilitli oku (yoksa olustur)
            2. StockLedger'a IN kaydi yaz
            3. WAC (Agirlikli Ortalama Maliyet) yeniden hesapla
            4. StockSnapshot'i guncelle

        WAC Formulü (gram bazli):
            Yeni WAC = (Eski Toplam Deger + Yeni Gelen Deger) / Yeni Toplam Miktar
            Yeni WAC = ((eski_gram * eski_wac) + (gelen_gram * gelen_maliyet)) / (eski_gram + gelen_gram)

        Args:
            product: Products model instance
            store: Stores model instance
            quantity_gram: Giris miktari (gram)
            quantity_pieces: Giris miktari (adet)
            reason: StockLedger.Reason enum degeri
            ref_type: Ust islem tipi ('process', 'invoice', 'conversion', vb.)
            ref_id: Ust islem kimlik degeri (UUID veya process_no)
            unit_cost_hs: Birim Has maliyeti
            unit_cost_tl: Birim TL maliyeti
            hs_rate_tl: Islem anindaki Has/TL kuru
            user: Islemi yapan kullanici
            notes: Aciklama

        Returns:
            StockLedger: Olusturulan ledger kaydi

        Raises:
            ValueError: Gecersiz parametre durumunda
        """
        # Parametre dogrulama
        quantity_gram = _q4(Decimal(str(quantity_gram)))
        unit_cost_hs = _q4(Decimal(str(unit_cost_hs)))
        unit_cost_tl = _q2(Decimal(str(unit_cost_tl)))
        hs_rate_tl = _q4(Decimal(str(hs_rate_tl)))

        if quantity_gram < 0 or quantity_pieces < 0:
            raise ValueError("Miktar negatif olamaz. Cikis icin record_exit() kullanin.")

        if quantity_gram == 0 and quantity_pieces == 0:
            raise ValueError("En az bir miktar (gram veya adet) sifirdan buyuk olmalidir.")

        # FAZ B: material_type bazli guvenlik dogrulamasi
        # WATCH/DIAMOND -> quantity_gram=0 ve quantity_pieces>=1 zorunlu.
        # SILVER -> log notu (HG birimden yorumlanir).
        # GOLD -> mevcut akis (ekstra kontrol yok).
        _validate_material_type_quantities(product, quantity_gram, quantity_pieces)

        # ADIM 1: StockSnapshot'i kilitli oku veya olustur
        snapshot, created = StockSnapshot.objects.select_for_update().get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': Decimal('0.0000'),
                'stock_pieces': 0,
                'weighted_avg_cost_hs': Decimal('0.0000'),
                'weighted_avg_cost_tl': Decimal('0.00'),
            }
        )

        if created:
            logger.info(
                f"Yeni StockSnapshot olusturuldu: "
                f"product={product.id}, store={store.id}"
            )

        # ADIM 2: StockLedger'a giris kaydi yaz
        entry = StockLedger.objects.create(
            product=product,
            store=store,
            direction=StockLedger.Direction.IN,
            reason=reason,
            quantity_gram=quantity_gram,
            quantity_pieces=quantity_pieces,
            unit_cost_hs=unit_cost_hs,
            unit_cost_tl=unit_cost_tl,
            hs_rate_tl=hs_rate_tl,
            ref_type=ref_type,
            ref_id=ref_id,
            notes=notes,
            created_by=user,
        )

        # ADIM 3: WAC (Agirlikli Ortalama Maliyet) hesapla
        old_gram = snapshot.stock_gram
        old_wac_hs = snapshot.weighted_avg_cost_hs
        old_wac_tl = snapshot.weighted_avg_cost_tl

        new_total_gram = old_gram + quantity_gram

        if new_total_gram > Decimal('0'):
            # WAC HS = (Eski Toplam Deger HS + Yeni Deger HS) / Yeni Toplam Gram
            new_wac_hs = _q4(
                ((old_gram * old_wac_hs) + (quantity_gram * unit_cost_hs)) / new_total_gram
            )

            # WAC TL = (Eski Toplam Deger TL + Yeni Deger TL) / Yeni Toplam Gram
            new_wac_tl = _q2(
                ((old_gram * old_wac_tl) + (quantity_gram * unit_cost_tl)) / new_total_gram
            )
        else:
            # Toplam gram 0 veya altindaysa (olmamali ama guvenlik icin)
            new_wac_hs = unit_cost_hs
            new_wac_tl = unit_cost_tl

        # ADIM 4: Snapshot guncelle
        snapshot.stock_gram = new_total_gram
        snapshot.stock_pieces = snapshot.stock_pieces + quantity_pieces
        snapshot.weighted_avg_cost_hs = new_wac_hs
        snapshot.weighted_avg_cost_tl = new_wac_tl
        snapshot.save(update_fields=[
            'stock_gram',
            'stock_pieces',
            'weighted_avg_cost_hs',
            'weighted_avg_cost_tl',
            'updated_on',
        ])

        logger.info(
            f"Stok girisi: product={product.name}, store={store}, "
            f"+{quantity_gram}g / +{quantity_pieces}ad, "
            f"yeni_toplam={snapshot.stock_gram}g, WAC_HS={new_wac_hs}"
        )

        # Dashboard 'Magaza Varliklari' cache'ini invalide et
        transaction.on_commit(lambda: _invalidate_dashboard_assets_cache(store))

        return entry

    @classmethod
    @transaction.atomic
    def record_exit(
        cls,
        *,
        product,
        store,
        quantity_gram: Decimal = Decimal('0.0000'),
        quantity_pieces: int = 0,
        reason: str,
        ref_type: str,
        ref_id: str,
        unit_cost_hs: Optional[Decimal] = None,
        unit_cost_tl: Optional[Decimal] = None,
        hs_rate_tl: Decimal = Decimal('0.0000'),
        user=None,
        notes: str = '',
    ) -> StockLedger:
        """
        Stok CIKISI: Satis, donusum cikisi, fire, transfer cikisi vb.

        Islem adimalari:
            1. StockSnapshot'i kilitli oku
            2. Stok yeterliligi kontrolu (uygulama seviyesi)
            3. StockLedger'a OUT kaydi yaz (maliyet WAC'tan muhurlenir)
            4. StockSnapshot'i guncelle

        Maliyet mantigi:
            - Cikislarda unit_cost belirtilmezse mevcut WAC kullanilir.
            - WAC, cikis isleminde degismez (sadece girisler WAC'i etkiler).

        Args:
            product: Products model instance
            store: Stores model instance
            quantity_gram: Cikis miktari (gram)
            quantity_pieces: Cikis miktari (adet)
            reason: StockLedger.Reason enum degeri
            ref_type: Ust islem tipi
            ref_id: Ust islem kimlik degeri
            unit_cost_hs: Birim maliyet Has (None ise WAC kullanilir)
            unit_cost_tl: Birim maliyet TL (None ise WAC kullanilir)
            hs_rate_tl: Islem anindaki Has/TL kuru
            user: Islemi yapan kullanici
            notes: Aciklama

        Returns:
            StockLedger: Olusturulan ledger kaydi

        Raises:
            InsufficientStockError: Yetersiz stok
            ValueError: Gecersiz parametre
        """
        # Parametre dogrulama
        quantity_gram = _q4(Decimal(str(quantity_gram)))
        hs_rate_tl = _q4(Decimal(str(hs_rate_tl)))

        if quantity_gram < 0 or quantity_pieces < 0:
            raise ValueError("Miktar negatif olamaz.")

        if quantity_gram == 0 and quantity_pieces == 0:
            raise ValueError("En az bir miktar (gram veya adet) sifirdan buyuk olmalidir.")

        # FAZ B: material_type bazli guvenlik dogrulamasi
        # WATCH/DIAMOND -> quantity_gram=0 ve quantity_pieces>=1 zorunlu.
        # SILVER -> log notu (HG birimden yorumlanir).
        # GOLD -> mevcut akis (ekstra kontrol yok).
        _validate_material_type_quantities(product, quantity_gram, quantity_pieces)

        # ADIM 1: StockSnapshot'i kilitli oku
        try:
            snapshot = (
                StockSnapshot.objects
                .select_for_update()
                .get(product=product, store=store)
            )
        except StockSnapshot.DoesNotExist:
            raise InsufficientStockError(
                product_name=getattr(product, 'name', str(product)),
                available=Decimal('0'),
                requested=quantity_gram,
                unit='g'
            )

        # ADIM 2: Stok yeterliligi kontrolu (uygulama seviyesi)
        if quantity_gram > 0 and snapshot.stock_gram < quantity_gram:
            raise InsufficientStockError(
                product_name=product.name,
                available=snapshot.stock_gram,
                requested=quantity_gram,
                unit='g'
            )

        if quantity_pieces > 0 and snapshot.stock_pieces < quantity_pieces:
            raise InsufficientStockError(
                product_name=product.name,
                available=Decimal(str(snapshot.stock_pieces)),
                requested=Decimal(str(quantity_pieces)),
                unit=' adet'
            )

        # Maliyet: Belirtilmemisse WAC kullan (cikis isleminde standart yontem)
        if unit_cost_hs is None:
            unit_cost_hs = snapshot.weighted_avg_cost_hs
        else:
            unit_cost_hs = _q4(Decimal(str(unit_cost_hs)))

        if unit_cost_tl is None:
            unit_cost_tl = snapshot.weighted_avg_cost_tl
        else:
            unit_cost_tl = _q2(Decimal(str(unit_cost_tl)))

        # ADIM 3: StockLedger'a cikis kaydi yaz
        entry = StockLedger.objects.create(
            product=product,
            store=store,
            direction=StockLedger.Direction.OUT,
            reason=reason,
            quantity_gram=quantity_gram,
            quantity_pieces=quantity_pieces,
            unit_cost_hs=unit_cost_hs,
            unit_cost_tl=unit_cost_tl,
            hs_rate_tl=hs_rate_tl,
            ref_type=ref_type,
            ref_id=ref_id,
            notes=notes,
            created_by=user,
        )

        # ADIM 4: Snapshot guncelle
        # Cikis isleminde WAC degismez!
        snapshot.stock_gram = snapshot.stock_gram - quantity_gram
        snapshot.stock_pieces = max(0, snapshot.stock_pieces - quantity_pieces)
        snapshot.save(update_fields=[
            'stock_gram',
            'stock_pieces',
            'updated_on',
        ])

        logger.info(
            f"Stok cikisi: product={product.name}, store={store}, "
            f"-{quantity_gram}g / -{quantity_pieces}ad, "
            f"kalan={snapshot.stock_gram}g, maliyet_hs={unit_cost_hs}"
        )

        # Dashboard 'Magaza Varliklari' cache'ini invalide et
        transaction.on_commit(lambda: _invalidate_dashboard_assets_cache(store))

        return entry

    @classmethod
    @transaction.atomic
    def adjustment(
        cls,
        *,
        product,
        store,
        actual_gram: Decimal,
        actual_pieces: int,
        ref_id: str,
        user=None,
        notes: str = 'Sayim duzeltmesi',
        is_initial: bool = False,
    ) -> Optional[StockLedger]:
        """
        Sayim sonrasi stok duzeltme islemi.

        Mevcut stok ile gercek sayim arasindaki farki hesaplar ve
        gerekli giris veya cikis kaydini olusturur.

        FAZ 19: is_initial=True ile çağrıldığında reason='INITIAL' olur.
        Açılış bakiyesi (devir) işlemleri bu şekilde işaretlenir.
        Payment/Kasa kaydı oluşturulmaz — yalnızca stok hareketi.

        Args:
            product: Products model instance
            store: Stores model instance
            actual_gram: Sayimda bulunan gercek gram
            actual_pieces: Sayimda bulunan gercek adet
            ref_id: Sayim islemi referans ID'si
            user: Islemi yapan kullanici
            notes: Aciklama
            is_initial: True ise acilis bakiyesi (INITIAL reason)

        Returns:
            StockLedger: Olusturulan ledger kaydi veya None (fark yoksa)
        """
        actual_gram = _q4(Decimal(str(actual_gram)))

        snapshot, created = StockSnapshot.objects.select_for_update().get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': Decimal('0.0000'),
                'stock_pieces': 0,
                'weighted_avg_cost_hs': Decimal('0.0000'),
                'weighted_avg_cost_tl': Decimal('0.00'),
            }
        )

        gram_diff = actual_gram - snapshot.stock_gram
        piece_diff = actual_pieces - snapshot.stock_pieces

        # Fark yoksa islem yapma
        if abs(gram_diff) < Decimal('0.0001') and piece_diff == 0:
            logger.info(
                f"Sayim duzeltme: Fark yok. product={product.name}, store={store}"
            )
            return None

        if gram_diff > 0 or piece_diff > 0:
            # Fazla bulundu -> giris kaydi
            direction = StockLedger.Direction.IN
            # FAZ 19: Açılış bakiyesi ise INITIAL, değilse ADJ_PLUS
            reason = StockLedger.Reason.INITIAL if is_initial else StockLedger.Reason.ADJUSTMENT_PLUS
            qty_gram = abs(gram_diff) if gram_diff > 0 else Decimal('0')
            qty_pieces = max(0, piece_diff) if piece_diff > 0 else 0
        else:
            # Eksik bulundu -> cikis kaydi
            direction = StockLedger.Direction.OUT
            reason = StockLedger.Reason.ADJUSTMENT_MINUS
            qty_gram = abs(gram_diff) if gram_diff < 0 else Decimal('0')
            qty_pieces = abs(piece_diff) if piece_diff < 0 else 0

        # FAZ 19: ref_type açılış bakiyesi için 'initial', düzeltme için 'count'
        _ref_type = 'initial' if is_initial else 'count'

        entry = StockLedger.objects.create(
            product=product,
            store=store,
            direction=direction,
            reason=reason,
            quantity_gram=_q4(qty_gram),
            quantity_pieces=qty_pieces,
            unit_cost_hs=snapshot.weighted_avg_cost_hs,
            unit_cost_tl=snapshot.weighted_avg_cost_tl,
            hs_rate_tl=Decimal('0'),
            ref_type=_ref_type,
            ref_id=ref_id,
            notes=notes,
            created_by=user,
        )

        # Snapshot'i dogrusundan guncelle
        snapshot.stock_gram = actual_gram
        snapshot.stock_pieces = actual_pieces
        snapshot.save(update_fields=[
            'stock_gram',
            'stock_pieces',
            'updated_on',
        ])

        logger.warning(
            f"Sayim duzeltme: product={product.name}, store={store}, "
            f"gram_fark={gram_diff}, adet_fark={piece_diff}, "
            f"yeni_gram={actual_gram}, yeni_adet={actual_pieces}"
        )

        # Dashboard 'Magaza Varliklari' cache'ini invalide et
        transaction.on_commit(lambda: _invalidate_dashboard_assets_cache(store))

        return entry
