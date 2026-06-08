from django.apps import AppConfig

class MarketplaceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.marketplace'

    def ready(self):
        from apps.marketplace.handlers import register_marketplace_handlers
        register_marketplace_handlers()
        from domain.events.demand_handlers import register_demand_handlers
        register_demand_handlers()
        import apps.marketplace.signals  # noqa
