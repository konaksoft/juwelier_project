from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.products"

    def ready(self):
        """
        FAZ B2 / ONARIM FAZI 1: Urun sinyallerini kaydet.
        Products.post_save -> WatchDetail/DiamondDetail otomatik olusturma.
        """
        try:
            import apps.products.signals  # noqa: F401
        except ImportError:
            # Signals modulu eksikse uygulama calismaya devam etsin.
            pass
