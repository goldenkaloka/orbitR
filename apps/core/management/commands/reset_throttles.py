"""Clear DRF throttle counters from the cache.

Load tests blow through the per-scope throttle budgets almost immediately, so
every subsequent request 429s and the run measures nothing. This wipes the
``throttle_*`` counters (DRF's ``SimpleRateThrottle`` cache keys) without
touching the rest of the app cache. Dev/load-test helper — safe to run anytime
since throttle windows simply restart.
"""

from django.core.cache import cache
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Clear DRF throttle counters (throttle_* keys) from the cache."

    def add_arguments(self, parser):
        parser.add_argument("--pattern", default="*throttle_*")

    def handle(self, *args, **opts):
        client = cache.client.get_client(write=True)
        keys = list(client.keys(opts["pattern"]))
        if keys:
            client.delete(*keys)
        self.stdout.write(self.style.SUCCESS(f"Cleared {len(keys)} throttle key(s)."))
