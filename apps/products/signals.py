"""
Products Signals — Uzanti Tablolarinin Garantisi
================================================================================

FAZ B2 / ONARIM FAZI 1: Bir Products kaydi WATCH veya DIAMOND tipi ile
olusturuldugunda, ilgili detay uzanti tablosunda (WatchDetail / DiamondDetail)
bos bir OneToOne karsilik otomatik olarak yaratilir.

Neden?
  - product.watch_detail veya product.diamond_detail erisiminde
    RelatedObjectDoesNotExist hatasi firlamasin.
  - UI kodunun her yerde 'try/except' ile guvenligini saglamaya gerek kalmasin.
  - Detay tablolari sonradan istendiginde UPDATE ile doldurulabilir.

Calisma Prensibi:
  - post_save sinyali ile Products create sonrasinda devreye girer.
  - created=False ise (update) hic bir sey yapmaz.
  - raw=True ise (loaddata) sessizce atlar.
  - OneToOne cakismasi (IntegrityError) olusursa get_or_create ile guvenli
    fallback saglar.

ZERO MIGRATION RISK: Bu sinyal sadece yeni kayitlarda calisir. Eski
WATCH/DIAMOND urunlerinde detay tablosu yoksa, bir yonetim komutu
(management/commands/backfill_detail_extensions.py) ile toplu olarak
doldurulabilir (Faz 1.5).
"""

import logging

from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger('products')


@receiver(post_save, sender='products.Products', dispatch_uid='products_ensure_detail_extension')
def ensure_detail_extension(sender, instance, created, raw=False, **kwargs):
    """
    WATCH/DIAMOND urunlerde detay tablosu kaydini garantile.

    - created=False  -> guncelleme, hic bir sey yapma.
    - raw=True       -> loaddata fixture, sessizce gec.
    - material_type  -> GOLD/SILVER ise hic bir sey yapma.
    - WATCH          -> WatchDetail get_or_create
    - DIAMOND        -> DiamondDetail get_or_create
    """
    # Fixture yuklemesinde post_save sinyali raw=True ile gelir.
    # Bu durumda sinyal tetiklenmemeli cunku fixture kendi verilerini
    # zaten yukluyordur.
    if raw:
        return

    if not created:
        return

    # Import'lari fonksiyon icinde yap: app registry hazir olmadan
    # modul yukluyken circular import'u onlemek icin.
    try:
        from apps.products.models import (
            MaterialType,
            WatchDetail,
            DiamondDetail,
        )
    except Exception as exc:
        logger.error(
            f"ensure_detail_extension: import hatasi -> {exc}. "
            f"Sinyal calistirilmadi."
        )
        return

    mat = getattr(instance, 'material_type', None)

    try:
        if mat == MaterialType.WATCH:
            # OneToOne cakismasina karsi get_or_create.
            # Atomic blok icinde: paralel istekler arasinda yaris kosulu
            # olursa IntegrityError'i yakalayip sessizce devam et.
            with transaction.atomic():
                WatchDetail.objects.get_or_create(product=instance)
            logger.info(
                f"WatchDetail olusturuldu: product_id={instance.pk}"
            )
        elif mat == MaterialType.DIAMOND:
            with transaction.atomic():
                DiamondDetail.objects.get_or_create(product=instance)
            logger.info(
                f"DiamondDetail olusturuldu: product_id={instance.pk}"
            )
    except IntegrityError as exc:
        # Paralel istek OneToOne ihlalinde: kayit zaten olusturulmus.
        logger.warning(
            f"ensure_detail_extension: IntegrityError (detay tablosu zaten "
            f"var olabilir) product_id={instance.pk}, material_type={mat}. "
            f"Hata: {exc}"
        )
    except Exception as exc:
        # Beklenmedik hata durumunda sinyal sessiz kalmali, urun olusumunu
        # bozmamali. Sadece logla.
        logger.error(
            f"ensure_detail_extension: beklenmedik hata "
            f"product_id={instance.pk}, material_type={mat} -> {exc}"
        )
