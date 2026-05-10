from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customers"

    def ready(self):
        # FAZ 27 — Hata 3B: CustomerLedger post_save signal'ı ile
        # Customer.receivable_hs / payable_hs legacy stored alanları
        # canlı balance_hs property'sinden senkronlanır. Reconciliation
        # banner drift'inin kök nedenini ortadan kaldırır.
        from apps.customers import signals  # noqa: F401
