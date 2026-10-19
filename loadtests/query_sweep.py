"""Single-shot performance sweep over the customer-facing API surface.

Complements ``loadtests/locustfile.py``:
  * Locust measures behaviour *under concurrency*.
  * This script measures *per-endpoint* query counts and latency in one shot, so
    you can spot N+1s and slow views quickly.

It relies on the dev-only QueryCount middleware, which adds an
``X-DjangoQueryCount-Count`` response header. Run the server with ``DEBUG=True``.

Usage (from the backend dir, server already running):

    python loadtests/query_sweep.py
    BASE_URL=http://localhost:8000 python loadtests/query_sweep.py --repeat 5
    CUSTOMER_TOKEN="<jwt>" python loadtests/query_sweep.py --all

Authenticated customer endpoints are only probed when ``CUSTOMER_TOKEN`` is set.
Mint one for a seeded customer with:

    docker exec orbitr-django python -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');django.setup();from rest_framework_simplejwt.tokens import RefreshToken;from apps.users.models import User;u=User.objects.filter(role='customer').first();print(str(RefreshToken.for_user(u).access_token))"
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("CUSTOMER_TOKEN", "").strip()
LAT, LNG = "-6.7924", "39.2083"
QCOUNT_HEADER = "X-DjangoQueryCount-Count"

# Public (anonymous) customer reads — try these with no token.
PUBLIC_ENDPOINTS = [
    "/api/restaurants/?category=all&limit=20&offset=0",
    f"/api/restaurants/?category=all&lat={LAT}&lng={LNG}&limit=20&offset=0",
    f"/api/restaurants/?category=all&lat={LAT}&lng={LNG}&sort_by=recommended&limit=20&offset=0",
    f"/api/restaurants/?category=all&lat={LAT}&lng={LNG}&sort_by=distance&limit=20&offset=0",
    "/api/marketplace/products/",
    f"/api/marketplace/products/?lat={LAT}&lng={LNG}",
    "/api/marketplace/categories/",
    f"/api/marketplace/demands/nearby/?lat={LAT}&lng={LNG}",
    f"/api/marketplace/shops/nearby/?lat={LAT}&lng={LNG}",
    "/api/geo/zones/autocomplete/?q=mikocheni",
    "/api/geo/zones/",
    "/api/app-config/",
]

# Authenticated customer reads — probed only when CUSTOMER_TOKEN is set.
AUTHED_ENDPOINTS = [
    f"/api/search/?q=chips&lat={LAT}&lng={LNG}",
    "/api/auth/me/",
    "/api/orders/?limit=20&offset=0",
    "/api/notifications/",
    "/api/wallets/me/",
    "/api/reliability/",
    "/api/reviews/my/",
    "/api/pools/?limit=20&offset=0",
    f"/api/pools/nearby/?lat={LAT}&lng={LNG}",
    "/api/marketplace/demands/mine/",
    "/api/marketplace/demands/my_offers/",
    "/api/marketplace/products/mine/",
]


def _get(path, token=None, timeout=60):
    url = BASE_URL + path
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = (time.perf_counter() - start) * 1000
            body = resp.read()
            return resp.status, resp.headers.get(QCOUNT_HEADER), elapsed_ms, body
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return exc.code, exc.headers.get(QCOUNT_HEADER), elapsed_ms, exc.read()
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000
        return None, None, elapsed_ms, str(exc).encode()


def _discover_restaurant_id():
    """Best-effort: pull one restaurant id to probe detail endpoints."""
    status, _, _, body = _get("/api/restaurants/?limit=1&offset=0")
    if status != 200:
        return None
    try:
        data = json.loads(body)
        results = data.get("results", data) if isinstance(data, dict) else data
        return results[0]["id"] if results else None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _run(name, path, token, repeat):
    counts, times, statuses = [], [], []
    for _ in range(repeat):
        status, qcount, elapsed_ms, _ = _get(path, token=token)
        statuses.append(status)
        times.append(elapsed_ms)
        if qcount is not None:
            try:
                counts.append(int(qcount))
            except ValueError:
                pass
    return {
        "name": name,
        "path": path,
        "status": statuses[-1] if statuses else None,
        "queries": max(counts) if counts else None,
        "ms_median": statistics.median(times) if times else None,
        "ms_max": max(times) if times else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Customer API performance sweep")
    parser.add_argument("--repeat", type=int, default=3, help="requests per endpoint")
    parser.add_argument("--all", action="store_true", help="include authenticated endpoints")
    parser.add_argument("--filter", default="", help="only paths containing this substring")
    args = parser.parse_args()

    if args.all and not TOKEN:
        print("! --all/authenticated endpoints need CUSTOMER_TOKEN; probing public only.\n")

    targets = list(PUBLIC_ENDPOINTS)
    if args.all and TOKEN:
        targets += AUTHED_ENDPOINTS

    restaurant_id = _discover_restaurant_id()
    if restaurant_id:
        targets += [
            f"/api/restaurants/{restaurant_id}/",
            f"/api/restaurants/{restaurant_id}/delivery-fee/?lat={LAT}&lng={LNG}",
        ]

    if args.filter:
        targets = [t for t in targets if args.filter in t]

    print(f"Base: {BASE_URL}  repeat={args.repeat}  auth={'yes' if TOKEN else 'no'}")
    print(f"{'STATUS':>6}  {'QUERIES':>7}  {'MED ms':>8}  {'MAX ms':>8}  PATH")
    print("-" * 90)

    results = []
    for path in targets:
        # Warm the cache for the first sample so we measure the steady state.
        if args.repeat > 1:
            _get(path, token=TOKEN if path in AUTHED_ENDPOINTS else None)
        token = TOKEN if path in AUTHED_ENDPOINTS else None
        row = _run(path, path, token, args.repeat)
        results.append(row)

    results.sort(key=lambda r: (r["queries"] or 0, r["ms_median"] or 0), reverse=True)
    for r in results:
        q = r["queries"] if r["queries"] is not None else "-"
        med = f"{r['ms_median']:.1f}" if r["ms_median"] is not None else "-"
        mx = f"{r['ms_max']:.1f}" if r["ms_max"] is not None else "-"
        print(f"{str(r['status']):>6}  {str(q):>7}  {med:>8}  {mx:>8}  {r['path']}")

    print("-" * 90)
    print("Sorted by query count (then latency). High QUERIES = likely N+1.")


if __name__ == "__main__":
    sys.exit(main())
