"""Deterministic per-endpoint performance sweep over the customer API surface.

Unlike the middleware-based tools (Silk / django-querycount), this runs the
request through the real view + serializers and counts application queries with
``CaptureQueriesContext``, so the numbers are exact and reproducible — usable
locally and in CI.

It also normalises SQL and reports duplicate-query counts, which is the signal
that exposes N+1s.

Usage:

    python manage.py perf_sweep
    python manage.py perf_sweep --repeat 5 --only restaurants
    python manage.py perf_sweep --json > perf.json
    python manage.py perf_sweep --min-queries 10      # only show offenders
    python manage.py perf_sweep --duplicates          # print duplicated SQL

Authenticated customer endpoints are probed as the first seeded ``role='customer'``
user (override with --user <phone>).
"""

import json
import re
import time
from collections import Counter
from unittest import mock

from apps.users.models import User
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

# ── SQL normalisation (to detect duplicates that only differ by literal) ──────
_UUID_RE = re.compile(r"'[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}'")
_IN_LIST_RE = re.compile(r"\bIN \([^)]*\)")
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")


def normalize_sql(sql: str) -> str:
    s = _UUID_RE.sub("'?'", sql)
    s = _IN_LIST_RE.sub("IN (?)", s)
    s = _NUM_RE.sub("?", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# (name, path, needs_auth)
PUBLIC_ENDPOINTS = [
    ("restaurants.list", "/api/restaurants/?category=all&limit=20&offset=0", False),
    ("restaurants.list.geo", "/api/restaurants/?category=all&lat=-6.7924&lng=39.2083&limit=20&offset=0", False),
    ("restaurants.list.recommended", "/api/restaurants/?category=all&lat=-6.7924&lng=39.2083&sort_by=recommended&limit=20&offset=0", False),
    ("restaurants.list.distance", "/api/restaurants/?category=all&lat=-6.7924&lng=39.2083&sort_by=distance&limit=20&offset=0", False),
    ("marketplace.products", "/api/marketplace/products/", False),
    ("marketplace.products.geo", "/api/marketplace/products/?lat=-6.7924&lng=39.2083", False),
    ("marketplace.categories", "/api/marketplace/categories/", False),
    ("geo.zones", "/api/geo/zones/", False),
    ("geo.zones.autocomplete", "/api/geo/zones/autocomplete/?q=mikocheni", False),
    ("app-config", "/api/app-config/", False),
]

AUTHED_ENDPOINTS = [
    ("search", "/api/search/?q=chips&lat=-6.7924&lng=39.2083", True),
    ("auth.me", "/api/auth/me/", True),
    ("orders.list", "/api/orders/?limit=20&offset=0", True),
    ("orders.active", "/api/orders/?active_only=true", True),
    ("notifications.list", "/api/notifications/", True),
    ("wallets.me", "/api/wallets/me/", True),
    ("reliability", "/api/reliability/", True),
    ("reviews.my", "/api/reviews/my/", True),
    ("pools.list", "/api/pools/?limit=20&offset=0", True),
    ("pools.nearby", "/api/pools/nearby/?lat=-6.7924&lng=39.2083", True),
    ("marketplace.demands.mine", "/api/marketplace/demands/mine/", True),
    ("marketplace.demands.nearby", "/api/marketplace/demands/nearby/?lat=-6.7924&lng=39.2083", True),
    ("marketplace.shops.nearby", "/api/marketplace/shops/nearby/?lat=-6.7924&lng=39.2083", True),
    ("marketplace.offers.mine", "/api/marketplace/demands/my_offers/", True),
    ("marketplace.listings.mine", "/api/marketplace/products/mine/", True),
]


class Command(BaseCommand):
    help = "Measure query counts + latency for customer-facing API endpoints."

    def add_arguments(self, parser):
        parser.add_argument("--repeat", type=int, default=3)
        parser.add_argument("--only", default="", help="substring filter on name")
        parser.add_argument("--user", default="", help="phone of the customer to auth as")
        parser.add_argument("--public-only", action="store_true")
        parser.add_argument("--min-queries", type=int, default=0)
        parser.add_argument("--duplicates", action="store_true", help="print duplicated SQL")
        parser.add_argument("--print-sql", action="store_true", help="print every captured query")
        parser.add_argument("--json", action="store_true", help="emit JSON")

    def handle(self, *args, **opts):
        client = APIClient()
        endpoints = list(PUBLIC_ENDPOINTS)

        customer = None
        if not opts["public_only"]:
            qs = User.objects.filter(role="customer")
            customer = qs.filter(phone=opts["user"]).first() if opts["user"] else qs.first()
            if customer is None:
                self.stderr.write("! No customer user found; probing public endpoints only.")
            else:
                endpoints += AUTHED_ENDPOINTS
                self.stdout.write(f"Auth as customer: {customer.phone} ({customer.id})")

        # Strip the profiling middleware for the run: Silk persists a row per
        # request and QueryCount mutates state, both of which would be counted
        # as application queries. Throttling is neutralised separately (its rate
        # table is bound on the throttle class at import, so settings override
        # alone does not lift it and repeated samples would 429).
        clean_middleware = [
            m for m in settings.MIDDLEWARE
            if not any(k in m for k in ("silk", "querycount", "nplusone"))
        ]
        sweep_settings = override_settings(
            MIDDLEWARE=clean_middleware,
            REST_FRAMEWORK={
                **settings.REST_FRAMEWORK,
                "DEFAULT_THROTTLE_CLASSES": [],
                "DEFAULT_THROTTLE_RATES": {},
            },
        )

        with sweep_settings, mock.patch.object(
            SimpleRateThrottle, "allow_request", lambda self, request, view: True
        ):
            rows = self._run_sweep(client, customer, endpoints, opts)

        if opts["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
            return

        self.stdout.write("")
        self.stdout.write(f"{'STATUS':>6}  {'QUERIES':>7}  {'MED ms':>8}  {'MAX ms':>8}  {'DUPS':>5}  ENDPOINT")
        self.stdout.write("-" * 100)
        for r in rows:
            self.stdout.write(
                f"{str(r['status']):>6}  {str(r['queries']):>7}  "
                f"{r['ms']:>8.1f}  {r['max_ms']:>8.1f}  {r['duplicates']:>5}  "
                f"{r['name']}  ({r['path']})"
            )
            if opts["duplicates"] and r["top_duplicates"]:
                for sql, cnt in r["top_duplicates"]:
                    self.stdout.write(f"        x{cnt}  {sql[:150]}")
            if opts["print_sql"]:
                for sql in r["all_sql"]:
                    self.stdout.write(f"        -  {sql[:170]}")
        self.stdout.write("-" * 100)
        self.stdout.write("Sorted by query count then latency. DUPS = queries executed more than once.")

    def _run_sweep(self, client, customer, endpoints, opts):
        # Discover a restaurant id to probe detail + delivery-fee.
        rid = self._first_restaurant_id(client)
        if rid:
            endpoints = endpoints + [
                ("restaurants.detail", f"/api/restaurants/{rid}/", False),
                ("restaurants.delivery_fee", f"/api/restaurants/{rid}/delivery-fee/?lat=-6.7924&lng=39.2083", False),
            ]

        if opts["only"]:
            endpoints = [e for e in endpoints if opts["only"] in e[0]]

        rows = []
        for name, path, needs_auth in endpoints:
            rows.append(self._measure(client, customer, name, path, needs_auth, opts))

        rows = [r for r in rows if (r["queries"] or 0) >= opts["min_queries"]]
        rows.sort(key=lambda r: (r["queries"] or 0, r["ms"] or 0), reverse=True)
        return rows

    def _first_restaurant_id(self, client):
        try:
            resp = client.get("/api/restaurants/?limit=1&offset=0")
            data = resp.json()
            results = data.get("results", data) if isinstance(data, dict) else data
            return results[0]["id"] if results else None
        except Exception:  # noqa: BLE001
            return None

    def _measure(self, client, customer, name, path, needs_auth, opts):
        def do_get():
            if needs_auth and customer is not None:
                client.force_authenticate(user=customer)
            else:
                client.force_authenticate(user=None)
            return client.get(path)

        if opts["repeat"] > 1:
            do_get()  # warm cache so we measure steady state

        times, statuses = [], []
        captured = []
        for _ in range(opts["repeat"]):
            with CaptureQueriesContext(connection) as ctx:
                start = time.perf_counter()
                resp = do_get()
                elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
            statuses.append(resp.status_code)
            captured = ctx.captured_queries  # keep the last sample's queries

        counts = Counter(normalize_sql(q["sql"]) for q in captured)
        dups = sum(c for c in counts.values() if c > 1)
        top = [(sql, c) for sql, c in counts.most_common(3) if c > 1]

        return {
            "name": name,
            "path": path,
            "status": statuses[-1] if statuses else None,
            "queries": len(captured),
            "ms": sorted(times)[len(times) // 2] if times else 0,
            "max_ms": max(times) if times else 0,
            "duplicates": dups,
            "top_duplicates": top,
            "all_sql": list(counts.elements()),
        }
