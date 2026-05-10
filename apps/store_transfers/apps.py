from django.apps import AppConfig


class StoreTransfersConfig(AppConfig):
    """FAZ 45 — Çoklu Şube Transfer App'i.

    StoreTransfer ve StoreTransferItem modellerini barındırır.
    FAZ 46+ aktivasyonuna kadar yalnızca şema seviyesinde durur;
    hiçbir view/servis bu modelleri henüz yazmaz.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.store_transfers'
    verbose_name = 'Şubeler Arası Transfer'
