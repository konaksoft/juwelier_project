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

# ============================================================================
# FAZ 48.4 — Emanet Havuzu Yönlendirme Sabitleri
# ============================================================================
# Bu reason kodları StockSnapshot.custody_gram / custody_pieces alanlarına
# yazar; stock_gram / stock_pieces'a DOKUNMAZ ve WAC'ı değiştirmez.
#
# CUSTODY_IN  (entry) → custody_gram artar, WAC değişmez
# CUSTODY_OUT (exit)  → custody_gram azalır, stock_gram değişmez
# CUSTODY_TO_STOCK:
#   - exit  tarafı → custody_gram azalır  (emanet havuzundan çıkış)
#   - entry tarafı → stock_gram artar + WAC güncellenir (serbest stoğa giriş)
_CUSTODY_ENTRY_REASONS = frozenset({'CUSTODY_IN'})
_CUSTODY_EXIT_REASONS  = frozenset({'CUSTODY_OUT', 'CUSTODY_2_STK'})


def _heal_missing_snapshot_for_barcoded(product, store):
    """
    FAZ 65 — Self-Healing Snapshot Restore Onarimi.

    Restore edilmis legacy barkodlu GoldPurchases urunlerinde eksik olan
    StockSnapshot kaydini, GoldPurchases gercekligine bakarak otomatik yaratir.

    Sadece su kosullar saglandiginda yaratir:
        1. Urunun bir barcode'u var (barkodlu tekil urun)
        2. GoldPurchases tablosunda is_status=True, is_deleted=False kaydi var
           (yani urun tezgahta, satilmamis)

    Bu durumlar yalnizca veri onarimi icindir; gercek stok yetersizligi
    senaryolarinda (snapshot var ama miktar 0) bu fonksiyon cagirilmaz
    cunku DoesNotExist tetiklenmez.

    Returns:
        StockSnapshot: Yeni yaratilan snapshot (kilitli)
        None: Onarim kosullari saglanmadi - cagiran taraf orijinal hatayi firlatir
    """
    try:
        if not getattr(product, 'barcode', None):
            return None

        # Lazy import (circular import korumasi)
        from apps.gold_purchases.models import GoldPurchases

        gp_active = GoldPurchases.objects.filter(
            product=product,
            store=store,
            is_deleted=False,
            is_status=True,
        ).exists()

        if not gp_active:
            return None

        # Urunun gerceklik durumunu yansit: tezgahta 1 adet, gram=product.gram
        product_gram = Decimal(str(getattr(product, 'gram', 0) or 0))
        buy_hs = Decimal(str(getattr(product, 'buy_price_hs', 0) or 0))
        buy_tl = Decimal(str(getattr(product, 'buy_price_eur', 0) or 0))

        # FAZ 65.1 — 1.05 EŞİK NORMALIZASYONU (SSOT, FAZ 44 ile aynı kural):
        # Restore edilen legacy ürünlerde Products.buy_price_hs hâlâ TOPLAM HAS
        # tutuyor olabilir (FAZ 34 öncesi formatın kalıntısı). Snapshot.WAC alanı
        # BİRİM bekliyor → 1.05 üzeri değeri ürün gramına bölerek normalize et.
        # Saf altın fraksiyonu ≤ 1.000; 1.05 yuvarlama tamponlu üst sınır.
        wac_hs = buy_hs
        wac_tl = buy_tl
        if buy_hs > Decimal('1.05') and product_gram > Decimal('0'):
            wac_hs = (buy_hs / product_gram).quantize(
                Decimal('0.0001'), rounding=ROUND_HALF_UP
            )
            # TL tarafı da paralel normalize (aynı çağdan geliyor)
            if buy_tl > Decimal('0'):
                wac_tl = (buy_tl / product_gram).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
            logger.warning(
                f"FAZ 65.1 self-heal normalize: legacy total tespit edildi "
                f"(product_id={product.id}, barcode={product.barcode}, "
                f"old_buy_hs={buy_hs}, normalized_wac_hs={wac_hs}, "
                f"product_gram={product_gram})"
            )

        snapshot, created = StockSnapshot.objects.select_for_update().get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': product_gram,
                'stock_pieces': 1,
                'weighted_avg_cost_hs': wac_hs,
                'weighted_avg_cost_eur': wac_tl,
            }
        )

        if created:
            logger.warning(
                f"FAZ 65 self-heal: StockSnapshot eksikti, restore edilmis "
                f"barkodlu urun icin yaratildi (product_id={product.id}, "
                f"barcode={product.barcode}, store_id={getattr(store, 'id', '?')}, "
                f"gram={product_gram})"
            )

        return snapshot
    except Exception as exc:
        # Self-heal basarisiz olursa orijinal hata firlatilsin
        logger.error(
            f"FAZ 65 self-heal hata (product_id={getattr(product, 'id', '?')}): {exc}"
        )
        return None


def _invalidate_dashboard_assets_cache(store):
    """
    Dashboard 'Magaza Varliklari' ozet cache'lerini invalide eder.

    Cache keys:
        - dashboard_assets_summary:{store.id}  (eski endpoint — get_store_assets_summary)
        - dashboard_assets_v2:{store.id}       (FAZ 26 — Patron Dashboard / TAB 1 assets-v2)

    FAZ 34 (2026-05-01): Eskiden yalniz dashboard_assets_summary key'i siliniyordu.
    FAZ 26 ile gelen assets_v2_view ayri bir cache key kullaniyor (5 dk TTL).
    Stok hareketinde bu key invalide edilmedigi icin Dashboard TAB 1 5 dakikaya
    kadar bayat WAC/Has degerleri gosterebiliyordu. Artik her iki key birlikte
    invalide ediliyor.

    Tetikleyici: Her StockSnapshot degisikligi (record_entry, record_exit,
    adjustment) sonrasinda ve products.delete_single sonrasinda cagirilir.

    Bu fonksiyon hata firlatmaz - cache katmani bagimsizliga engel olmasin.
    """
    try:
        if store is None:
            return
        store_id = getattr(store, 'id', None)
        if store_id:
            cache.delete(f"dashboard_assets_summary:{store_id}")
            cache.delete(f"dashboard_assets_v2:{store_id}")
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
            hs_rate_eur=Decimal('3250.00'),
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
        unit_cost_eur: Decimal = Decimal('0.00'),
        hs_rate_eur: Decimal = Decimal('0.0000'),
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
            unit_cost_eur: Birim TL maliyeti
            hs_rate_eur: Islem anindaki Has/TL kuru
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
        unit_cost_eur = _q2(Decimal(str(unit_cost_eur)))
        hs_rate_eur = _q4(Decimal(str(hs_rate_eur)))

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
                'weighted_avg_cost_eur': Decimal('0.00'),
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
            unit_cost_eur=unit_cost_eur,
            hs_rate_eur=hs_rate_eur,
            ref_type=ref_type,
            ref_id=ref_id,
            notes=notes,
            created_by=user,
        )

        # ADIM 3 & 4: Snapshot güncelle — Emanet havuzu mu, mağaza stoğu mu?
        # FAZ 48.4: CUSTODY_IN → custody_gram (WAC değişmez, stock_gram değişmez)
        #           Diğer nedenler → stock_gram + WAC hesabı (mevcut davranış)
        if reason in _CUSTODY_ENTRY_REASONS:
            # ── Emanet girişi: custody havuzuna yaz ──────────────────────────
            snapshot.custody_gram = _q4(snapshot.custody_gram + quantity_gram)
            snapshot.custody_pieces = snapshot.custody_pieces + quantity_pieces
            snapshot.save(update_fields=['custody_gram', 'custody_pieces', 'updated_on'])

            logger.info(
                f"Emanet girisi (custody): product={product.name}, store={store}, "
                f"+{quantity_gram}g / +{quantity_pieces}ad, "
                f"yeni_custody={snapshot.custody_gram}g"
            )
        else:
            # ── Mağaza stoğu girişi: WAC hesapla + stock_gram güncelle ───────
            old_gram = snapshot.stock_gram
            old_wac_hs = snapshot.weighted_avg_cost_hs
            old_wac_tl = snapshot.weighted_avg_cost_eur

            new_total_gram = old_gram + quantity_gram

            if new_total_gram > Decimal('0'):
                new_wac_hs = _q4(
                    ((old_gram * old_wac_hs) + (quantity_gram * unit_cost_hs)) / new_total_gram
                )
                new_wac_tl = _q2(
                    ((old_gram * old_wac_tl) + (quantity_gram * unit_cost_eur)) / new_total_gram
                )
            else:
                new_wac_hs = unit_cost_hs
                new_wac_tl = unit_cost_eur

            snapshot.stock_gram = new_total_gram
            snapshot.stock_pieces = snapshot.stock_pieces + quantity_pieces
            snapshot.weighted_avg_cost_hs = new_wac_hs
            snapshot.weighted_avg_cost_eur = new_wac_tl
            snapshot.save(update_fields=[
                'stock_gram',
                'stock_pieces',
                'weighted_avg_cost_hs',
                'weighted_avg_cost_eur',
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
        unit_cost_eur: Optional[Decimal] = None,
        hs_rate_eur: Decimal = Decimal('0.0000'),
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
            unit_cost_eur: Birim maliyet TL (None ise WAC kullanilir)
            hs_rate_eur: Islem anindaki Has/TL kuru
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
        hs_rate_eur = _q4(Decimal(str(hs_rate_eur)))

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
        # FAZ 65 — Self-Healing Snapshot:
        # Restore edilmis legacy barkodlu GoldPurchases urunlerinde
        # StockSnapshot eksik olabiliyor (pre-FAZ 3 era, ya da acilis
        # stogu olarak eklenmis urunler). Bu durumda DoesNotExist
        # firlatilmadan once GoldPurchases gercekligine bakilarak
        # snapshot otomatik onarilir. Mevcut yetersiz stok mantigi
        # (snapshot var ama miktar yetersiz) etkilenmez.
        try:
            snapshot = (
                StockSnapshot.objects
                .select_for_update()
                .get(product=product, store=store)
            )
        except StockSnapshot.DoesNotExist:
            snapshot = _heal_missing_snapshot_for_barcoded(product, store)
            if snapshot is None:
                raise InsufficientStockError(
                    product_name=getattr(product, 'name', str(product)),
                    available=Decimal('0'),
                    requested=quantity_gram,
                    unit='g'
                )

        # FAZ 48.4: Emanet çıkış mı, mağaza stoğu çıkış mı?
        _is_custody_exit = reason in _CUSTODY_EXIT_REASONS

        # ADIM 2: Yeterlilik kontrolü (uygulama seviyesi)
        if _is_custody_exit:
            # Emanet havuzundan çıkış — custody_gram / custody_pieces kontrol
            if quantity_gram > 0 and snapshot.custody_gram < quantity_gram:
                raise InsufficientStockError(
                    product_name=product.name,
                    available=snapshot.custody_gram,
                    requested=quantity_gram,
                    unit='g (emanet)'
                )
            if quantity_pieces > 0 and snapshot.custody_pieces < quantity_pieces:
                raise InsufficientStockError(
                    product_name=product.name,
                    available=Decimal(str(snapshot.custody_pieces)),
                    requested=Decimal(str(quantity_pieces)),
                    unit=' adet (emanet)'
                )
        else:
            # Mağaza stoğundan çıkış — stock_gram / stock_pieces kontrol
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

        if unit_cost_eur is None:
            unit_cost_eur = snapshot.weighted_avg_cost_eur
        else:
            unit_cost_eur = _q2(Decimal(str(unit_cost_eur)))

        # ADIM 3: StockLedger'a cikis kaydi yaz
        entry = StockLedger.objects.create(
            product=product,
            store=store,
            direction=StockLedger.Direction.OUT,
            reason=reason,
            quantity_gram=quantity_gram,
            quantity_pieces=quantity_pieces,
            unit_cost_hs=unit_cost_hs,
            unit_cost_eur=unit_cost_eur,
            hs_rate_eur=hs_rate_eur,
            ref_type=ref_type,
            ref_id=ref_id,
            notes=notes,
            created_by=user,
        )

        # ADIM 4: Snapshot güncelle
        if _is_custody_exit:
            # Emanet havuzundan çıkış — WAC değişmez, stock_gram değişmez
            snapshot.custody_gram = _q4(snapshot.custody_gram - quantity_gram)
            snapshot.custody_pieces = max(0, snapshot.custody_pieces - quantity_pieces)
            snapshot.save(update_fields=['custody_gram', 'custody_pieces', 'updated_on'])

            logger.info(
                f"Emanet cikisi (custody): product={product.name}, store={store}, "
                f"-{quantity_gram}g / -{quantity_pieces}ad, "
                f"kalan_custody={snapshot.custody_gram}g, reason={reason}"
            )
        else:
            # Mağaza stoğundan çıkış — WAC değişmez, stock_gram azalır
            snapshot.stock_gram = snapshot.stock_gram - quantity_gram
            snapshot.stock_pieces = max(0, snapshot.stock_pieces - quantity_pieces)
            # FAZ 65.2 — Phantom gram düzeltmesi:
            # Barkodlu parça satışında (FAZ 42) Process.gram=0 olarak gelir;
            # quantity_gram=0 olduğundan stock_gram azalmaz. stock_pieces 0'a
            # düşse bile stock_gram=product.gram olarak kalır → dashboard
            # "Fiziksel Stok HAS" satılmış ürünleri saymaya devam eder.
            # Adet sıfıra düştüyse gram da sıfırlansın.
            if (
                quantity_pieces > 0
                and snapshot.stock_pieces == 0
                and snapshot.stock_gram > Decimal('0')
            ):
                logger.warning(
                    f"FAZ 65.2 phantom gram sifirlama: product={product.name}, "
                    f"store={store}, eski_stock_gram={snapshot.stock_gram}g -> 0g "
                    f"(barkodlı parça çıkışı, adet 0'a düştü)"
                )
                snapshot.stock_gram = Decimal('0.0000')
            snapshot.save(update_fields=['stock_gram', 'stock_pieces', 'updated_on'])

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
        unit_cost_hs: Optional[Decimal] = None,
        unit_cost_eur: Optional[Decimal] = None,
    ) -> Optional[StockLedger]:
        """
        Sayim sonrasi stok duzeltme islemi.

        Mevcut stok ile gercek sayim arasindaki farki hesaplar ve
        gerekli giris veya cikis kaydini olusturur.

        FAZ 19: is_initial=True ile çağrıldığında reason='INITIAL' olur.
        Açılış bakiyesi (devir) işlemleri bu şekilde işaretlenir.
        Payment/Kasa kaydı oluşturulmaz — yalnızca stok hareketi.

        FAZ 34 (2026-05-01): Opsiyonel unit_cost_hs / unit_cost_eur parametreleri
        eklendi. POZITIF fark (giris yonu) durumunda eski WAC=0 olan yeni
        bir snapshot icin maliyet hic guncellenmiyordu — bu yuzden
        "Urunler ve Stok Yonetimi" sayfasindan eklenen ozel urunlerin WAC i
        sifir kaliyor, Dashboard ta HAS=0 olarak gozukuyordu. Artik cagri
        yapan unit_cost_hs gecirirse:
            - Yeni snapshot ise: WAC = unit_cost_hs (dogrudan)
            - Mevcut snapshot ise: agirlikli ortalama formuluyle birlestirilir
        Negatif fark (cikis yonu) durumunda WAC degismez (mevcut WAC ile
        ledger e cikis kaydi yazilir — record_exit ile ayni davranis).

        Args:
            product: Products model instance
            store: Stores model instance
            actual_gram: Sayimda bulunan gercek gram
            actual_pieces: Sayimda bulunan gercek adet
            ref_id: Sayim islemi referans ID'si
            user: Islemi yapan kullanici
            notes: Aciklama
            is_initial: True ise acilis bakiyesi (INITIAL reason)
            unit_cost_hs: Opsiyonel — POZITIF fark icin birim Has maliyet.
                          None ise WAC degismez (eski davranis).
            unit_cost_eur: Opsiyonel — POZITIF fark icin birim TL maliyet.

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
                'weighted_avg_cost_eur': Decimal('0.00'),
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

        # FAZ 34: Pozitif fark (giris yonu) ve cagri unit_cost_hs gecirdiyse
        # WAC i agirlikli ortalama ile guncelle. Negatif fark veya parametre
        # gecilmediyse mevcut WAC korunur (eski davranis).
        ledger_unit_cost_hs = snapshot.weighted_avg_cost_hs
        ledger_unit_cost_eur = snapshot.weighted_avg_cost_eur
        new_wac_hs = snapshot.weighted_avg_cost_hs
        new_wac_tl = snapshot.weighted_avg_cost_eur

        if direction == StockLedger.Direction.IN and unit_cost_hs is not None:
            _uc_hs = _q4(Decimal(str(unit_cost_hs)))
            _uc_tl = _q2(Decimal(str(unit_cost_eur))) if unit_cost_eur is not None else Decimal('0.00')
            ledger_unit_cost_hs = _uc_hs
            ledger_unit_cost_eur = _uc_tl

            # WAC formulu: agirlikli ortalama
            old_gram = snapshot.stock_gram  # snapshot henuz guncellenmedi
            old_wac_hs = snapshot.weighted_avg_cost_hs
            old_wac_tl = snapshot.weighted_avg_cost_eur
            _new_total_gram = old_gram + qty_gram
            if _new_total_gram > Decimal('0'):
                new_wac_hs = _q4(
                    ((old_gram * old_wac_hs) + (qty_gram * _uc_hs)) / _new_total_gram
                )
                new_wac_tl = _q2(
                    ((old_gram * old_wac_tl) + (qty_gram * _uc_tl)) / _new_total_gram
                )
            else:
                new_wac_hs = _uc_hs
                new_wac_tl = _uc_tl

        entry = StockLedger.objects.create(
            product=product,
            store=store,
            direction=direction,
            reason=reason,
            quantity_gram=_q4(qty_gram),
            quantity_pieces=qty_pieces,
            unit_cost_hs=ledger_unit_cost_hs,
            unit_cost_eur=ledger_unit_cost_eur,
            hs_rate_eur=Decimal('0'),
            ref_type=_ref_type,
            ref_id=ref_id,
            notes=notes,
            created_by=user,
        )

        # Snapshot'i dogrusundan guncelle
        snapshot.stock_gram = actual_gram
        snapshot.stock_pieces = actual_pieces
        snapshot.weighted_avg_cost_hs = new_wac_hs
        snapshot.weighted_avg_cost_eur = new_wac_tl
        snapshot.save(update_fields=[
            'stock_gram',
            'stock_pieces',
            'weighted_avg_cost_hs',
            'weighted_avg_cost_eur',
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
