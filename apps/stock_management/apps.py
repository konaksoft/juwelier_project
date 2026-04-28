from django.apps import AppConfig


class StockManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.stock_management'
    verbose_name = 'Stok Yonetimi (Ledger)'

    def ready(self):
        pass
