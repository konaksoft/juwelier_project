# FILE: apps/stores/signals.py
import logging
from decimal import Decimal

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import ImageField
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from apps.stores.models import Stores
from .models import StoreConfiguration
from apps.helpers.image_resize import process_image
from apps.settings.conf import get


log = logging.getLogger(__name__)


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


# ─────────────────────────────────────────────────────────────────────────────
# FAZ 12 — Mağaza Kurulduğunda Otomatik StoreConfiguration Oluşturma
# ─────────────────────────────────────────────────────────────────────────────
@receiver(post_save, sender=Stores, dispatch_uid="create_store_config_v1")
def create_store_config(sender, instance, created, **kwargs):
    if created:
        StoreConfiguration.objects.create(store=instance)


# ─────────────────────────────────────────────────────────────────────────────
# FAZ 19 — Otomatik Varsayılan Kasa Açılışı (Cold-Start Çözümü)
#
# Yeni bir mağaza sisteme dahil olduğunda (DEMO veya ACTIVE fark etmez)
# kuyumcunun boş kasa ekranıyla karşılaşmaması için 3 varsayılan
# BankAccount kaydı otomatik oluşturulur:
#
#   1) Merkez Nakit Kasa     → account_type='CASH', currency='TRY'
#   2) Merkez Havale/EFT     → account_type='BANK', currency='TRY'
#   3) Merkez POS Kasası     → account_type='POS',  currency='TRY'
#       └─ Bağlı POSCommissionRate kaydı:
#           card_type='GENERIC', installment_count=1,
#           commission_rate=3.00, maturity_days=1
#
# SSOT: Sinyal tabanlı yaklaşım, mağazanın hangi kanaldan oluşturulduğundan
#       bağımsız çalışır. add_store(), auto_create_store_from_proposal() ve
#       create_demo_store() — hepsi sonunda Stores.objects.create() çağırır
#       ve bu sinyal tetiklenir.
#
# Idempotency: Aynı mağaza için ikinci kez tetiklenirse (manuel save vb.)
#              "created=True" yalnızca ilk kayıtta gelir; mükerrer kasa
#              oluşturulmaz. Ek güvence olarak filter().exists() kontrolü
#              yapılır (örn. fixture yüklemelerinde).
# ─────────────────────────────────────────────────────────────────────────────
@receiver(post_save, sender=Stores, dispatch_uid="create_default_bank_accounts_v1")
def create_default_bank_accounts(sender, instance, created, **kwargs):
    """
    Yeni mağaza oluşturulduğunda 3 varsayılan kasayı + POS komisyon oranını üretir.

    Bu sinyal yalnızca created=True olduğunda çalışır; mevcut bir mağazanın
    güncellenmesi (örn. is_active toggle) tetiklemez.

    Hatalı durumda mağaza oluşturma akışını KIRMAZ; loglar ve sessizce geçer.
    Aksi halde tek bir Banking modülü bug'ı tüm onboarding'i çökertirdi.
    """
    if not created:
        return

    # Lazy import: signals dosyası import sırasında apps.banking dependency'si
    # yüklenmek zorunda kalmasın; modeller hazır olduğunda çağrılır.
    try:
        from apps.banking.models import BankAccount, POSCommissionRate
    except Exception as exc:
        log.warning(
            "create_default_bank_accounts: Banking modülü yüklenemedi, "
            "varsayılan kasalar atlandı. store_id=%s err=%s",
            getattr(instance, 'store_id', None), exc
        )
        return

    # Idempotency: Bu mağaza için zaten kasa varsa hiçbir şey yapma.
    # (Fixture yüklemeleri, manuel create + sinyal yarış durumları için güvence.)
    if BankAccount.objects.filter(store=instance).exists():
        return

    try:
        with transaction.atomic():
            # 1) Merkez Nakit Kasa
            BankAccount.objects.create(
                store=instance,
                name='Merkez Nakit Kasa',
                bank_name=None,
                iban=None,
                currency='TRY',
                account_type=BankAccount.AccountType.CASH,
                reconciliation_tolerance=Decimal('0.50'),
                is_active=True,
            )

            # 2) Merkez Havale/EFT Hesabı
            BankAccount.objects.create(
                store=instance,
                name='Merkez Havale/EFT Hesabı',
                bank_name=None,
                iban=None,
                currency='TRY',
                account_type=BankAccount.AccountType.BANK,
                reconciliation_tolerance=Decimal('0.50'),
                is_active=True,
            )

            # 3) Merkez POS Kasası + varsayılan POSCommissionRate
            pos_account = BankAccount.objects.create(
                store=instance,
                name='Merkez POS Kasası',
                bank_name=None,
                iban=None,
                currency='TRY',
                account_type=BankAccount.AccountType.POS,
                reconciliation_tolerance=Decimal('0.50'),
                is_active=True,
            )
            POSCommissionRate.objects.create(
                bank_account=pos_account,
                card_type=POSCommissionRate.CardType.GENERIC,
                installment_count=1,
                commission_rate=Decimal('3.00'),
                maturity_days=1,
                is_active=True,
            )

        log.info(
            "create_default_bank_accounts: 3 varsayılan kasa + 1 POS komisyon "
            "kuralı oluşturuldu. store_id=%s status=%s",
            instance.store_id, instance.status
        )

    except Exception as exc:
        # Mağaza oluşturma akışını kırmamak için exception yutulur.
        # Banking modülünde bir bug olsa bile mağaza ve StoreConfiguration
        # başarıyla oluşur; admin gerekirse kasaları manuel ekleyebilir.
        log.exception(
            "create_default_bank_accounts: Varsayılan kasa oluşturma başarısız. "
            "store_id=%s err=%s",
            getattr(instance, 'store_id', None), exc
        )


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
