# FILE: apps/stores/signals.py
from django.core.files.uploadedfile import UploadedFile
from django.db.models import ImageField
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.stores.models import Stores
from .models import StoreConfiguration
from apps.helpers.image_resize import process_image
from apps.settings.conf import get


# DİKKAT:
# StoreBranch modeli artık yok; bu nedenle şube oluşturan post_save sinyali kaldırıldı.
# Görsel işleme sinyali aşağıda her model için (genel) çalışır.


def _enabled() -> bool:
    """Global toggle."""
    return bool(get("AUTO_IMAGE_PROCESSING_ENABLED"))


def _skip(sender) -> bool:
    """
    Bazı modelleri hariç tutmak için settings.py’de
    'AUTO_IMAGE_PROCESSING_SKIP_MODELS' = ["app.model", ...] tanımlanabilir.
    """
    skips = set(get("AUTO_IMAGE_PROCESSING_SKIP_MODELS") or [])
    skips = {str(m).lower() for m in skips}
    label = f"{sender._meta.app_label}.{sender._meta.model_name}"
    return label.lower() in skips


def _uploaded_file_or_none(fieldfile):
    """
    FieldFile üstünde storage'ı açmadan gerçekten yeni yüklenmiş dosya (UploadedFile)
    var mı anla. Yoksa None dön.
    """
    uf = getattr(fieldfile, "_file", None)
    if isinstance(uf, UploadedFile):
        return uf
    if not getattr(fieldfile, "_committed", True):
        try:
            f = fieldfile.file
            if isinstance(f, UploadedFile):
                return f
        except Exception:
            return None
    return None


@receiver(pre_save, dispatch_uid="auto_process_images_v2")
def auto_process_images(sender, instance, **kwargs):
    """
    Tüm ImageField’ler için YALNIZCA yeni upload geldiğinde process_image uygular.
    Mevcut/commit edilmiş dosyalarda storage’a dokunmaz; dosya yoksa sessizce geçer.
    """
    if not _enabled() or _skip(sender):
        return

    # Tek seferlik atlama isteyen modeller instance.__skip_auto_image__ = True set edebilir.
    if getattr(instance, "__skip_auto_image__", False):
        return

    for field in instance._meta.get_fields():
        if not isinstance(field, ImageField):
            continue

        fieldfile = getattr(instance, field.name, None)
        if not fieldfile:
            continue  # alan None

        uploaded = _uploaded_file_or_none(fieldfile)
        if not uploaded:
            # Yeni bir yükleme yok; mevcut dosyaya dokunma.
            continue

        try:
            filename, content = process_image(
                uploaded,
                max_width=get("IMAGE_MAX_WIDTH"),
                max_height=get("IMAGE_MAX_HEIGHT"),
                quality=get("IMAGE_QUALITY"),
                max_kb=get("IMAGE_MAX_KB"),
                prefer_webp=get("IMAGE_PREFER_WEBP"),
                keep_exif=get("IMAGE_KEEP_EXIF"),
            )
            # save=False => bu model kaydı sırasında tek seferde yazılacak
            fieldfile.save(filename, content, save=False)
        except Exception:
            # Burayı istersen loglayabilirsin.
            # import logging; logging.getLogger(__name__).exception("image process failed")
            pass




@receiver(post_save, sender=Stores)
def create_store_config(sender, instance, created, raw=False, **kwargs):
    # raw=True → Django serializer (loaddata / restore) tarafından kaydediliyor.
    # Bu durumda zaten yedek/fixture içinde StoreConfiguration var; otomatik
    # üretirsek UNIQUE(store_id) ihlali ile restore patlar.
    if created and not raw:
        StoreConfiguration.objects.create(store=instance)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard "Mağaza Varlıkları" cache invalidation sinyali
# ─────────────────────────────────────────────────────────────────────────────
# Payment (kasa/banka hareket) kayıtları değiştiğinde Dashboard varlık özeti
# cache'i temizlenmelidir; aksi halde Dashboard 10 dk boyunca eski nakit
# bakiyesini gösterir.
#
# StockSnapshot tarafı StockService içinden temizleniyor (record_entry /
# record_exit / adjustment). Burada sadece Payment side-effect'i işleniyor.
# ─────────────────────────────────────────────────────────────────────────────

from django.db.models.signals import post_delete  # noqa: E402


def _invalidate_assets_cache_from_payment(payment_instance):
    """
    Payment -> ProcessGroup -> store üzerinden cache temizliği yapar.

    Hata fırlatmaz; Payment akışı cache katmanından bağımsız kalmalı.
    """
    try:
        from django.core.cache import cache

        store_id = None
        if payment_instance.bank_account_id:
            try:
                store_id = payment_instance.bank_account.store_id
            except Exception:
                store_id = None

        if not store_id and payment_instance.process_group_id:
            try:
                store_id = payment_instance.process_group.store_id
            except Exception:
                store_id = None

        if store_id:
            cache.delete(f"dashboard_assets_summary:{store_id}")
    except Exception:
        # Cache temizliği fail olsa bile Payment işlemi aksamamalı
        pass


@receiver(post_save, dispatch_uid="payment_assets_cache_invalidation_v1")
def _on_payment_saved(sender, instance, **kwargs):
    """Payment save edildiğinde dashboard varlık cache'ini düşür."""
    # Sadece Payment modeli için çalış
    try:
        from apps.process.models import Payment  # lazy import
    except Exception:
        return
    if sender is not Payment:
        return
    _invalidate_assets_cache_from_payment(instance)


@receiver(post_delete, dispatch_uid="payment_assets_cache_invalidation_delete_v1")
def _on_payment_deleted(sender, instance, **kwargs):
    """Payment silindiğinde de dashboard varlık cache'ini düşür."""
    try:
        from apps.process.models import Payment  # lazy import
    except Exception:
        return
    if sender is not Payment:
        return
    _invalidate_assets_cache_from_payment(instance)