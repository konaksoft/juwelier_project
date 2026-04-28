"""
ConversionService - Hurda / Ham Madde -> Mamul Urun Donusum Servisi
===================================================================

Kuyumculukta en kritik is sureci: Hurda altin -> Barkodlu urun / Bilezik donusumu.

Bu servis sunlari ATOMIK olarak yapar:
    1. Kaynak urunun stogunu kilitler ve yeterliligi kontrol eder
    2. Hedef urunun stogunu kilitler
    3. Kaynaktan gram dusurup, hedefe gram ekler
    4. Her iki islem icin StockLedger satiri yazar
    5. Iki ledger satirini paired_entry ile birbirine baglar
    6. WAC (Agirlikli Ortalama Maliyet) yeniden hesaplar
    7. Fire varsa ayri bir ledger satiri yazar

Donusum senaryolari:
    - 500g Hurda -> 480g Barkodlu Urun (20g fire)
    - 1000g Hurda -> 400g Barkodlu + 600g Bilezik (coklu hedef)
    - 100g 14K Hurda -> 58.33g Has (eritme/refine)

ONEMLI:
    - Kaynak ve hedef FARKLI urunler olmalidir.
    - Toplam cikis >= toplam giris olmalidir (fire olabilir, kazanc olamaz).
    - Fire varsa SCRAP_MELT sebebiyle ayri ledger satiri yazilir.
"""

import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction

from apps.stock_management.models import StockLedger, StockSnapshot
from apps.stock_management.services.stock_service import (
    StockService,
    InsufficientStockError,
)

logger = logging.getLogger('stock_management')


def _q4(value: Decimal) -> Decimal:
    """4 ondalik haneye yuvarla."""
    return (value or Decimal('0')).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def _q2(value: Decimal) -> Decimal:
    """2 ondalik haneye yuvarla."""
    return (value or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class ConversionIntegrityError(Exception):
    """
    Donusum butunluk hatasi.
    Toplam cikis < toplam giris durumunda firlatilir (fiziksel olarak imkansiz).
    """
    pass


class ConversionService:
    """
    Hurda / Ham madde -> Barkodlu Urun / Bilezik donusum servisi.

    Temel kullanim:
        result = ConversionService.convert(
            source_product=hurda,
            source_gram=Decimal('500.0000'),
            target_product=barkodlu_urun,
            target_gram=Decimal('480.0000'),
            fire_gram=Decimal('20.0000'),
            store=magaza,
            user=request.user,
        )

    Coklu hedef (1 hurda -> N mamul):
        results = ConversionService.convert_multi(
            source_product=hurda,
            source_gram=Decimal('1000.0000'),
            targets=[
                {'product': barkodlu, 'gram': Decimal('400.0000')},
                {'product': bilezik, 'gram': Decimal('580.0000')},
            ],
            fire_gram=Decimal('20.0000'),
            store=magaza,
            user=request.user,
        )
    """

    @classmethod
    @transaction.atomic
    def convert(
        cls,
        *,
        source_product,
        source_gram: Decimal,
        target_product,
        target_gram: Decimal,
        fire_gram: Decimal = Decimal('0.0000'),
        store,
        user=None,
        ref_id: Optional[str] = None,
        hs_rate_tl: Decimal = Decimal('0.0000'),
        notes: str = '',
    ) -> dict:
        """
        Tek kaynaktan tek hedefe donusum islemi.

        Ornek: 500g hurda -> 480g barkodlu urun + 20g fire

        Islem adimalari:
            1. Parametreleri dogrula
            2. Gram dengesi kontrolu: source_gram == target_gram + fire_gram
            3. Kaynak snapshot'i kilitli oku, yeterlilik kontrolu
            4. Hedef snapshot'i kilitli oku
            5. Kaynak icin OUT ledger satiri yaz
            6. Hedef icin IN ledger satiri yaz
            7. Fire varsa SCRAP_MELT ledger satiri yaz
            8. OUT ve IN satirlarini paired_entry ile bagla
            9. Her iki snapshot'i guncelle

        Args:
            source_product: Kaynak urun (orn: hurda). Products instance.
            source_gram: Kaynaktan dusulecek gram miktari
            target_product: Hedef urun (orn: barkodlu). Products instance.
            target_gram: Hedefe eklenecek gram miktari
            fire_gram: Isleme sirasinda kaybolan gram (eritme kaybi)
            store: Magaza (Stores instance)
            user: Islemi yapan kullanici
            ref_id: Donusum islem numarasi (bos ise otomatik UUID uretilir)
            hs_rate_tl: Islem anindaki Has/TL kuru
            notes: Serbest metin aciklama

        Returns:
            dict: {
                'ref_id': str,
                'out_ledger_id': str,
                'in_ledger_id': str,
                'fire_ledger_id': str | None,
                'source_stock_after_gram': float,
                'target_stock_after_gram': float,
                'fire_gram': float,
                'source_wac_hs': float,
            }

        Raises:
            InsufficientStockError: Kaynak stok yetersiz
            ConversionIntegrityError: Gram dengesi tutmuyor
            ValueError: Gecersiz parametre
        """
        # ── Parametre dogrulama ──
        source_gram = _q4(Decimal(str(source_gram)))
        target_gram = _q4(Decimal(str(target_gram)))
        fire_gram = _q4(Decimal(str(fire_gram)))
        hs_rate_tl = _q4(Decimal(str(hs_rate_tl)))

        if source_gram <= 0:
            raise ValueError("Kaynak gram sifirdan buyuk olmalidir.")

        if target_gram <= 0:
            raise ValueError("Hedef gram sifirdan buyuk olmalidir.")

        if fire_gram < 0:
            raise ValueError("Fire negatif olamaz.")

        if source_product == target_product:
            raise ValueError("Kaynak ve hedef urun ayni olamaz.")

        # Gram dengesi kontrolu: Cikis = Giris + Fire
        expected_source = target_gram + fire_gram
        if abs(source_gram - expected_source) > Decimal('0.001'):
            raise ConversionIntegrityError(
                f"Gram dengesi tutmuyor: "
                f"kaynak={source_gram}g != hedef({target_gram}g) + fire({fire_gram}g) = {expected_source}g. "
                f"Fark: {abs(source_gram - expected_source)}g"
            )

        # Referans ID olustur
        if not ref_id:
            ref_id = f"CONV-{uuid.uuid4().hex[:12].upper()}"

        # ── ADIM 1: Kaynak snapshot'i kilitli oku ──
        try:
            source_snapshot = (
                StockSnapshot.objects
                .select_for_update()
                .get(product=source_product, store=store)
            )
        except StockSnapshot.DoesNotExist:
            raise InsufficientStockError(
                product_name=getattr(source_product, 'name', str(source_product)),
                available=Decimal('0'),
                requested=source_gram,
            )

        # Yeterlilik kontrolu
        if source_snapshot.stock_gram < source_gram:
            raise InsufficientStockError(
                product_name=source_product.name,
                available=source_snapshot.stock_gram,
                requested=source_gram,
            )

        # ── ADIM 2: Hedef snapshot'i kilitli oku/olustur ──
        target_snapshot, _ = (
            StockSnapshot.objects
            .select_for_update()
            .get_or_create(
                product=target_product,
                store=store,
                defaults={
                    'stock_gram': Decimal('0.0000'),
                    'stock_pieces': 0,
                    'weighted_avg_cost_hs': Decimal('0.0000'),
                    'weighted_avg_cost_tl': Decimal('0.00'),
                }
            )
        )

        # Maliyet: Kaynak'in WAC'i hedef'e devredilir
        source_wac_hs = source_snapshot.weighted_avg_cost_hs
        source_wac_tl = source_snapshot.weighted_avg_cost_tl

        # ── ADIM 3: Kaynak icin OUT ledger satiri ──
        out_entry = StockLedger.objects.create(
            product=source_product,
            store=store,
            direction=StockLedger.Direction.OUT,
            reason=StockLedger.Reason.CONVERSION_OUT,
            quantity_gram=source_gram,
            quantity_pieces=0,
            unit_cost_hs=source_wac_hs,
            unit_cost_tl=source_wac_tl,
            hs_rate_tl=hs_rate_tl,
            ref_type='conversion',
            ref_id=ref_id,
            notes=notes or f"Donusum cikisi -> {target_product.name}",
            created_by=user,
        )

        # ── ADIM 4: Hedef icin IN ledger satiri ──
        in_entry = StockLedger.objects.create(
            product=target_product,
            store=store,
            direction=StockLedger.Direction.IN,
            reason=StockLedger.Reason.CONVERSION_IN,
            quantity_gram=target_gram,
            quantity_pieces=0,
            unit_cost_hs=source_wac_hs,  # Maliyet kaynaktan miras alinir
            unit_cost_tl=source_wac_tl,
            hs_rate_tl=hs_rate_tl,
            ref_type='conversion',
            ref_id=ref_id,
            notes=notes or f"Donusum girisi <- {source_product.name}",
            created_by=user,
        )

        # ── ADIM 5: Fire varsa ayri ledger satiri ──
        fire_ledger_id = None
        if fire_gram > Decimal('0'):
            fire_entry = StockLedger.objects.create(
                product=source_product,
                store=store,
                direction=StockLedger.Direction.OUT,
                reason=StockLedger.Reason.SCRAP_MELT,
                quantity_gram=fire_gram,
                quantity_pieces=0,
                unit_cost_hs=source_wac_hs,
                unit_cost_tl=source_wac_tl,
                hs_rate_tl=hs_rate_tl,
                ref_type='conversion',
                ref_id=ref_id,
                notes=f"Isleme firesi ({fire_gram}g)",
                created_by=user,
            )
            fire_ledger_id = str(fire_entry.id)

        # ── ADIM 6: Ledger ciftini birbirine bagla ──
        out_entry.paired_entry = in_entry
        out_entry.save(update_fields=['paired_entry'])
        in_entry.paired_entry = out_entry
        in_entry.save(update_fields=['paired_entry'])

        # ── ADIM 7: Kaynak snapshot guncelle ──
        source_snapshot.stock_gram = source_snapshot.stock_gram - source_gram
        # Cikis isleminde WAC degismez
        source_snapshot.save(update_fields=['stock_gram', 'updated_on'])

        # ── ADIM 8: Hedef snapshot guncelle (WAC yeniden hesapla) ──
        old_target_gram = target_snapshot.stock_gram
        old_target_wac_hs = target_snapshot.weighted_avg_cost_hs
        old_target_wac_tl = target_snapshot.weighted_avg_cost_tl
        new_target_gram = old_target_gram + target_gram

        if new_target_gram > Decimal('0'):
            new_target_wac_hs = _q4(
                ((old_target_gram * old_target_wac_hs) + (target_gram * source_wac_hs))
                / new_target_gram
            )
            new_target_wac_tl = _q2(
                ((old_target_gram * old_target_wac_tl) + (target_gram * source_wac_tl))
                / new_target_gram
            )
        else:
            new_target_wac_hs = source_wac_hs
            new_target_wac_tl = source_wac_tl

        target_snapshot.stock_gram = new_target_gram
        target_snapshot.weighted_avg_cost_hs = new_target_wac_hs
        target_snapshot.weighted_avg_cost_tl = new_target_wac_tl
        target_snapshot.save(update_fields=[
            'stock_gram',
            'weighted_avg_cost_hs',
            'weighted_avg_cost_tl',
            'updated_on',
        ])

        logger.info(
            f"Donusum tamamlandi: ref={ref_id}, "
            f"{source_product.name}(-{source_gram}g) -> "
            f"{target_product.name}(+{target_gram}g), fire={fire_gram}g"
        )

        return {
            'ref_id': ref_id,
            'out_ledger_id': str(out_entry.id),
            'in_ledger_id': str(in_entry.id),
            'fire_ledger_id': fire_ledger_id,
            'source_stock_after_gram': float(source_snapshot.stock_gram),
            'target_stock_after_gram': float(target_snapshot.stock_gram),
            'fire_gram': float(fire_gram),
            'source_wac_hs': float(source_wac_hs),
        }

    @classmethod
    @transaction.atomic
    def convert_multi(
        cls,
        *,
        source_product,
        source_gram: Decimal,
        targets: list,
        fire_gram: Decimal = Decimal('0.0000'),
        store,
        user=None,
        ref_id: Optional[str] = None,
        hs_rate_tl: Decimal = Decimal('0.0000'),
        notes: str = '',
    ) -> dict:
        """
        Tek kaynaktan coklu hedefe donusum islemi.

        Ornek: 1000g hurda -> 400g barkodlu + 580g bilezik + 20g fire

        Args:
            source_product: Kaynak urun
            source_gram: Kaynaktan dusulecek toplam gram
            targets: Hedef listesi. Her eleman:
                {
                    'product': Products instance,
                    'gram': Decimal('400.0000'),
                    'pieces': 0,  (opsiyonel)
                }
            fire_gram: Toplam fire miktari
            store: Magaza
            user: Islemi yapan kullanici
            ref_id: Donusum referans ID
            hs_rate_tl: Has/TL kuru
            notes: Aciklama

        Returns:
            dict: {
                'ref_id': str,
                'out_ledger_id': str,
                'in_ledger_ids': [str, ...],
                'fire_ledger_id': str | None,
                'target_results': [{product_id, stock_after_gram}, ...],
            }

        Raises:
            InsufficientStockError: Kaynak stok yetersiz
            ConversionIntegrityError: Gram dengesi tutmuyor
            ValueError: Gecersiz parametre
        """
        source_gram = _q4(Decimal(str(source_gram)))
        fire_gram = _q4(Decimal(str(fire_gram)))
        hs_rate_tl = _q4(Decimal(str(hs_rate_tl)))

        if not targets:
            raise ValueError("En az bir hedef belirtilmelidir.")

        if source_gram <= 0:
            raise ValueError("Kaynak gram sifirdan buyuk olmalidir.")

        # Toplam hedef gramini hesapla
        total_target_gram = Decimal('0')
        for t in targets:
            t_gram = _q4(Decimal(str(t.get('gram', 0))))
            if t_gram <= 0:
                raise ValueError(
                    f"Hedef gram sifirdan buyuk olmalidir: {t.get('product', '?')}"
                )
            total_target_gram += t_gram

        # Gram dengesi
        expected = total_target_gram + fire_gram
        if abs(source_gram - expected) > Decimal('0.001'):
            raise ConversionIntegrityError(
                f"Gram dengesi tutmuyor: kaynak={source_gram}g != "
                f"hedefler({total_target_gram}g) + fire({fire_gram}g) = {expected}g"
            )

        if not ref_id:
            ref_id = f"CONV-{uuid.uuid4().hex[:12].upper()}"

        # ── Kaynak snapshot kilitli oku ──
        try:
            source_snapshot = (
                StockSnapshot.objects
                .select_for_update()
                .get(product=source_product, store=store)
            )
        except StockSnapshot.DoesNotExist:
            raise InsufficientStockError(
                product_name=getattr(source_product, 'name', str(source_product)),
                available=Decimal('0'),
                requested=source_gram,
            )

        if source_snapshot.stock_gram < source_gram:
            raise InsufficientStockError(
                product_name=source_product.name,
                available=source_snapshot.stock_gram,
                requested=source_gram,
            )

        source_wac_hs = source_snapshot.weighted_avg_cost_hs
        source_wac_tl = source_snapshot.weighted_avg_cost_tl

        # ── Kaynak OUT ledger ──
        out_entry = StockLedger.objects.create(
            product=source_product,
            store=store,
            direction=StockLedger.Direction.OUT,
            reason=StockLedger.Reason.CONVERSION_OUT,
            quantity_gram=source_gram,
            quantity_pieces=0,
            unit_cost_hs=source_wac_hs,
            unit_cost_tl=source_wac_tl,
            hs_rate_tl=hs_rate_tl,
            ref_type='conversion',
            ref_id=ref_id,
            notes=notes or f"Coklu donusum cikisi ({len(targets)} hedef)",
            created_by=user,
        )

        # ── Her hedef icin IN ledger ve snapshot guncelle ──
        in_ledger_ids = []
        target_results = []

        for target_item in targets:
            target_product = target_item['product']
            target_gram = _q4(Decimal(str(target_item['gram'])))
            target_pieces = int(target_item.get('pieces', 0))

            if target_product == source_product:
                raise ValueError(
                    f"Hedef urun kaynak ile ayni olamaz: {target_product}"
                )

            # Hedef snapshot kilitli oku/olustur
            t_snapshot, _ = (
                StockSnapshot.objects
                .select_for_update()
                .get_or_create(
                    product=target_product,
                    store=store,
                    defaults={
                        'stock_gram': Decimal('0.0000'),
                        'stock_pieces': 0,
                        'weighted_avg_cost_hs': Decimal('0.0000'),
                        'weighted_avg_cost_tl': Decimal('0.00'),
                    }
                )
            )

            # IN ledger yaz
            in_entry = StockLedger.objects.create(
                product=target_product,
                store=store,
                direction=StockLedger.Direction.IN,
                reason=StockLedger.Reason.CONVERSION_IN,
                quantity_gram=target_gram,
                quantity_pieces=target_pieces,
                unit_cost_hs=source_wac_hs,
                unit_cost_tl=source_wac_tl,
                hs_rate_tl=hs_rate_tl,
                ref_type='conversion',
                ref_id=ref_id,
                notes=notes or f"Donusum girisi <- {source_product.name}",
                created_by=user,
            )

            in_ledger_ids.append(str(in_entry.id))

            # Hedef snapshot WAC guncelle
            old_g = t_snapshot.stock_gram
            old_wac_hs = t_snapshot.weighted_avg_cost_hs
            old_wac_tl = t_snapshot.weighted_avg_cost_tl
            new_g = old_g + target_gram

            if new_g > Decimal('0'):
                new_wac_hs = _q4(
                    ((old_g * old_wac_hs) + (target_gram * source_wac_hs)) / new_g
                )
                new_wac_tl = _q2(
                    ((old_g * old_wac_tl) + (target_gram * source_wac_tl)) / new_g
                )
            else:
                new_wac_hs = source_wac_hs
                new_wac_tl = source_wac_tl

            t_snapshot.stock_gram = new_g
            t_snapshot.stock_pieces = t_snapshot.stock_pieces + target_pieces
            t_snapshot.weighted_avg_cost_hs = new_wac_hs
            t_snapshot.weighted_avg_cost_tl = new_wac_tl
            t_snapshot.save(update_fields=[
                'stock_gram', 'stock_pieces',
                'weighted_avg_cost_hs', 'weighted_avg_cost_tl',
                'updated_on',
            ])

            target_results.append({
                'product_id': str(target_product.id),
                'product_name': target_product.name,
                'gram_added': float(target_gram),
                'stock_after_gram': float(t_snapshot.stock_gram),
            })

        # ── Fire ledger (varsa) ──
        fire_ledger_id = None
        if fire_gram > Decimal('0'):
            fire_entry = StockLedger.objects.create(
                product=source_product,
                store=store,
                direction=StockLedger.Direction.OUT,
                reason=StockLedger.Reason.SCRAP_MELT,
                quantity_gram=fire_gram,
                quantity_pieces=0,
                unit_cost_hs=source_wac_hs,
                unit_cost_tl=source_wac_tl,
                hs_rate_tl=hs_rate_tl,
                ref_type='conversion',
                ref_id=ref_id,
                notes=f"Isleme firesi ({fire_gram}g)",
                created_by=user,
            )
            fire_ledger_id = str(fire_entry.id)

        # ── Kaynak snapshot guncelle ──
        source_snapshot.stock_gram = source_snapshot.stock_gram - source_gram
        source_snapshot.save(update_fields=['stock_gram', 'updated_on'])

        logger.info(
            f"Coklu donusum tamamlandi: ref={ref_id}, "
            f"{source_product.name}(-{source_gram}g) -> "
            f"{len(targets)} hedef, fire={fire_gram}g"
        )

        return {
            'ref_id': ref_id,
            'out_ledger_id': str(out_entry.id),
            'in_ledger_ids': in_ledger_ids,
            'fire_ledger_id': fire_ledger_id,
            'source_stock_after_gram': float(source_snapshot.stock_gram),
            'target_results': target_results,
        }

    # ====================================================================
    # YENI METOT: StockService Uzerinden Donusum (Mimari Uyumlu)
    # ====================================================================
    # Yukaridaki convert() ve convert_multi() metotlari StockService'i
    # atliyor (dogrudan Ledger/Snapshot yaziyorlar). Bu metot mimariye
    # uygun olarak TUM islemleri StockService uzerinden yapar.
    # ====================================================================

    @classmethod
    @transaction.atomic
    def convert_scrap_to_product(
        cls,
        *,
        source_scrap,
        target_product,
        store,
        used_scrap_gram: Decimal,
        target_quantity_pieces: int = 0,
        target_quantity_gram: Decimal,
        melt_loss_gram: Decimal = Decimal('0.0000'),
        user=None,
        notes: str = '',
    ) -> dict:
        """
        Hurda -> Barkodlu Urun donusumu (StockService uzerinden).

        Mevcut convert() ve convert_multi() metotlarindan FARKI:
        Bu metot StockService.record_exit / record_entry uzerinden calisir,
        dogrudan Ledger/Snapshot manipulasyonu YAPMAZ. Boylece:
            - select_for_update() kilitleme StockService tarafindan yonetilir
            - WAC hesaplamasi StockService tarafindan yapilir
            - Negatif stok kontrolu StockService + CheckConstraint ile cift katmanli
            - Tum stok degisiklikleri tek kanal (StockService) uzerinden akar

        Fiziksel gerceklik:
            500g Hurda eritilir -> 480g Barkodlu Urun + 20g fire kaybi
            used_scrap_gram     = 500  (toplam tuketilen hurda)
            target_quantity_gram = 480  (urune donusen miktar)
            melt_loss_gram      = 20   (fire kaybi)
            Denge: used_scrap_gram == target_quantity_gram + melt_loss_gram

        Ledger cikti yapisi:
            1. CONV_OUT  : Kaynaktan (used_scrap_gram - melt_loss_gram) gram cikar
                           Net donusum miktari — CONV_IN ile eslesen paired_entry.
            2. CONV_IN   : Hedefe target_quantity_gram gram ekle
                           Maliyet kaynak WAC'tan miras alinir.
            3. SCRAP_MELT: Kaynaktan melt_loss_gram gram cikar (fire kaybi)
                           Ayri bir cikis kaydi — paired_entry YOKTUR.
            Toplam cikis = (used_scrap_gram - melt_loss_gram) + melt_loss_gram
                         = used_scrap_gram

        NEDEN CONV_OUT miktari (used_scrap_gram - melt_loss_gram)?
            StockService.record_exit() her cagrildiginda snapshot'i dusuruyor.
            Eger CONV_OUT = used_scrap_gram (tam miktar) + SCRAP_MELT = melt_loss_gram
            olsaydi, toplam dusus = used_scrap_gram + melt_loss_gram olurdu (cift sayim!).
            Bu yuzden CONV_OUT sadece net donusum kismini cikarir, fire ayrica cikar.
            Ek avantaj: CONV_OUT.quantity_gram == CONV_IN.quantity_gram (kusursuz eslestirme).

        WAC (Agirlikli Ortalama Maliyet):
            Kaynak urunun WAC degeri, record_exit sirasinda muhurlenir (unit_cost_hs/tl).
            Bu maliyet record_entry ile hedefe aktarilir.

        Args:
            source_scrap: Kaynak hurda urunu (Products instance)
            target_product: Hedef barkodlu urun (Products instance)
            store: Magaza (Stores instance)
            used_scrap_gram: Toplam tuketilen hurda grami (Decimal)
            target_quantity_pieces: Hedef urun adedi (int, varsayilan: 0)
            target_quantity_gram: Hedef urun grami (Decimal)
            melt_loss_gram: Eritme/isleme fire kaybi grami (Decimal, varsayilan: 0)
            user: Islemi yapan kullanici (Users instance veya None)
            notes: Serbest metin aciklama (str)

        Returns:
            dict: {
                'ref_id': str,                           # Donusum referans ID
                'conv_out_ledger': StockLedger,          # CONV_OUT kaydi
                'conv_in_ledger': StockLedger,           # CONV_IN kaydi
                'fire_ledger': StockLedger | None,       # SCRAP_MELT kaydi (fire yoksa None)
                'source_wac_hs': Decimal,                # Kaynak WAC Has
                'source_wac_tl': Decimal,                # Kaynak WAC TL
            }

        Raises:
            InsufficientStockError: Kaynak stok yetersiz
            ConversionIntegrityError: Gram dengesi tutmuyor
            ValueError: Gecersiz parametre (ayni urun, negatif deger vb.)
        """
        # ── 0. Parametre dogrulama ──
        used_scrap_gram = _q4(Decimal(str(used_scrap_gram)))
        target_quantity_gram = _q4(Decimal(str(target_quantity_gram)))
        melt_loss_gram = _q4(Decimal(str(melt_loss_gram)))

        if used_scrap_gram <= 0:
            raise ValueError("used_scrap_gram sifirdan buyuk olmalidir.")

        if target_quantity_gram <= 0:
            raise ValueError("target_quantity_gram sifirdan buyuk olmalidir.")

        if melt_loss_gram < 0:
            raise ValueError("melt_loss_gram negatif olamaz.")

        if target_quantity_pieces < 0:
            raise ValueError("target_quantity_pieces negatif olamaz.")

        if source_scrap == target_product:
            raise ValueError("Kaynak ve hedef urun ayni olamaz.")

        # ── Gram dengesi kontrolu ──
        # used_scrap_gram == target_quantity_gram + melt_loss_gram
        expected_total = target_quantity_gram + melt_loss_gram
        if abs(used_scrap_gram - expected_total) > Decimal('0.001'):
            raise ConversionIntegrityError(
                f"Gram dengesi tutmuyor: "
                f"used_scrap_gram({used_scrap_gram}g) != "
                f"target_quantity_gram({target_quantity_gram}g) + "
                f"melt_loss_gram({melt_loss_gram}g) = {expected_total}g. "
                f"Fark: {abs(used_scrap_gram - expected_total)}g"
            )

        # Referans ID olustur
        ref_id = f"CONV-{uuid.uuid4().hex[:12].upper()}"

        # Net donusum miktari (urune donusen kisim, fire haric)
        net_conversion_gram = _q4(used_scrap_gram - melt_loss_gram)

        # ── ADIM 1: Kaynak hurda cikisi (CONV_OUT) ──
        # Net donusum miktarini cikar. Fire ayri adimda cikarilacak.
        # StockService kilitleme, yeterlilik kontrolu ve WAC muhurlemesini yapar.
        conv_out_entry = StockService.record_exit(
            product=source_scrap,
            store=store,
            quantity_gram=net_conversion_gram,
            quantity_pieces=0,
            reason=StockLedger.Reason.CONVERSION_OUT,
            ref_type='conversion',
            ref_id=ref_id,
            user=user,
            notes=notes or f"Donusum cikisi -> {target_product.name}",
        )

        # Kaynak WAC: cikis isleminde StockService tarafindan muhurlenmis maliyet
        source_wac_hs = conv_out_entry.unit_cost_hs
        source_wac_tl = conv_out_entry.unit_cost_tl

        # ── ADIM 2: Hedef urun girisi (CONV_IN) ──
        # Maliyet kaynak WAC'tan miras alinir.
        # StockService otomatik WAC yeniden hesaplamasi yapar.
        conv_in_entry = StockService.record_entry(
            product=target_product,
            store=store,
            quantity_gram=target_quantity_gram,
            quantity_pieces=target_quantity_pieces,
            reason=StockLedger.Reason.CONVERSION_IN,
            ref_type='conversion',
            ref_id=ref_id,
            unit_cost_hs=source_wac_hs,
            unit_cost_tl=source_wac_tl,
            user=user,
            notes=notes or f"Donusum girisi <- {source_scrap.name}",
        )

        # ── ADIM 3: Fire cikisi (SCRAP_MELT) ──
        # Eger melt_loss_gram > 0 ise, fire icin ayri bir cikis kaydi olustur.
        # Bu ikinci record_exit ayni transaction icinde calisir ve ilk cikisin
        # sonrasindaki guncel snapshot'i gorur (PostgreSQL MVCC).
        fire_entry = None
        if melt_loss_gram > Decimal('0'):
            fire_entry = StockService.record_exit(
                product=source_scrap,
                store=store,
                quantity_gram=melt_loss_gram,
                quantity_pieces=0,
                reason=StockLedger.Reason.SCRAP_MELT,
                ref_type='conversion',
                ref_id=ref_id,
                user=user,
                notes=f"Isleme firesi ({melt_loss_gram}g)",
            )

        # ── ADIM 4: CONV_OUT <-> CONV_IN paired_entry eslestirmesi ──
        # StockLedger.save() immutability korumasi sadece paired_entry
        # guncellenmesine izin verir (update_fields=['paired_entry']).
        conv_out_entry.paired_entry = conv_in_entry
        conv_out_entry.save(update_fields=['paired_entry'])
        conv_in_entry.paired_entry = conv_out_entry
        conv_in_entry.save(update_fields=['paired_entry'])

        logger.info(
            f"[convert_scrap_to_product] ref={ref_id}, "
            f"{source_scrap.name}(-{used_scrap_gram}g) -> "
            f"{target_product.name}(+{target_quantity_gram}g, "
            f"{target_quantity_pieces}ad), "
            f"fire={melt_loss_gram}g, WAC_HS={source_wac_hs}"
        )

        return {
            'ref_id': ref_id,
            'conv_out_ledger': conv_out_entry,
            'conv_in_ledger': conv_in_entry,
            'fire_ledger': fire_entry,
            'source_wac_hs': source_wac_hs,
            'source_wac_tl': source_wac_tl,
        }
