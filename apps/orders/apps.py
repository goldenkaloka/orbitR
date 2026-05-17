from django.apps import AppConfig

class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'

    def ready(self):
        from django.db import connection
        from django.core.management import call_command
        try:
            if connection.ensure_connection():
                from domain.services.batching_engine import BatchingEngine
                BatchingEngine.seed_strategies()
        except Exception:
            pass
