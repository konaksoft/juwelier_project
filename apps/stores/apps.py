# apps/stores/apps.py
from django.apps import AppConfig

class StoresConfig(AppConfig):
    name = 'apps.stores'
    def ready(self):
        import apps.settings.signals  # noqa