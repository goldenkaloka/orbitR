"""Locust load scenarios for the customer-facing (Flutter) API surface.

Two user classes run by default:

* ``CustomerBrowser``    — anonymous storefront browsing (highest volume).
* ``CustomerAccount``    — authenticated customer flows.

The authenticated class needs a JWT for a real customer. Mint one and pass it
in via env (Locust workers don't share the DB connection, so a token is the
simplest way in):

    docker exec orbitr-django python manage.py shell -c \
      "from rest_framework_simplejwt.tokens import RefreshToken; \
       from apps.users.models import User; \
       u=User.objects.filter(role='customer').first(); \
       print(str(RefreshToken.for_user(u).access_token))"

    set CUSTOMER_TOKEN=<paste>
    locust -f backend/loadtests/locustfile.py --host http://192.168.118.33:8000

If ``CUSTOMER_TOKEN`` is unset the authenticated tasks short-circuit, so a bare
anonymous run still works. Filter classes/tasks with ``--tags``:

    locust -f ... --tags anonymous
    locust -f ... --tags auth
"""

import os

from locust import HttpUser, between, tag, task

LAT = os.getenv("LAT", "-6.7924")
LNG = os.getenv("LNG", "39.2083")
CUSTOMER_TOKEN = os.getenv("CUSTOMER_TOKEN", "").strip()


class CustomerBrowser(HttpUser):
    """Anonymous storefront browsing: the traffic every visitor generates."""

    wait_time = between(1, 3)

    def on_start(self):
        self.restaurant_id = None
        with self.client.get(
            "/api/restaurants/?limit=1&offset=0",
            name="GET /api/restaurants [bootstrap]",
            catch_response=True,
        ) as resp:
            try:
                data = resp.json()
                results = data.get("results", data) if isinstance(data, dict) else data
                if results:
                    self.restaurant_id = results[0]["id"]
            except Exception:  # noqa: BLE001
                resp.failure("bootstrap parse failed")

    @tag("anonymous")
    @task(6)
    def restaurants_list(self):
        self.client.get(
            "/api/restaurants/?category=all&limit=20&offset=0",
            name="GET /api/restaurants",
        )

    @tag("anonymous")
    @task(4)
    def restaurants_list_geo(self):
        self.client.get(
            f"/api/restaurants/?category=all&lat={LAT}&lng={LNG}&limit=20&offset=0",
            name="GET /api/restaurants [geo]",
        )

    @tag("anonymous")
    @task(2)
    def restaurants_list_sorted(self):
        for sort in ("recommended", "distance", "rating"):
            self.client.get(
                f"/api/restaurants/?category=all&lat={LAT}&lng={LNG}&sort_by={sort}&limit=20&offset=0",
                name=f"GET /api/restaurants [sort:{sort}]",
            )

    @tag("anonymous")
    @task(4)
    def restaurant_detail(self):
        if not self.restaurant_id:
            return
        self.client.get(f"/api/restaurants/{self.restaurant_id}/", name="GET /api/restaurants/{id}")

    @tag("anonymous")
    @task(3)
    def restaurant_delivery_fee(self):
        if not self.restaurant_id:
            return
        self.client.get(
            f"/api/restaurants/{self.restaurant_id}/delivery-fee/?lat={LAT}&lng={LNG}",
            name="GET /api/restaurants/{id}/delivery-fee",
        )

    @tag("anonymous")
    @task(3)
    def marketplace_products(self):
        self.client.get("/api/marketplace/products/", name="GET /api/marketplace/products")

    @tag("anonymous")
    @task(2)
    def marketplace_categories(self):
        self.client.get("/api/marketplace/categories/", name="GET /api/marketplace/categories")

    @tag("anonymous")
    @task(2)
    def geo_zones(self):
        self.client.get("/api/geo/zones/", name="GET /api/geo/zones")

    @tag("anonymous")
    @task(3)
    def geo_autocomplete(self):
        self.client.get(
            "/api/geo/zones/autocomplete/?q=mikocheni",
            name="GET /api/geo/zones/autocomplete",
        )

    @tag("anonymous")
    @task(2)
    def app_config(self):
        self.client.get("/api/app-config/", name="GET /api/app-config")


class CustomerAccount(HttpUser):
    """Authenticated customer flows (orders, wallet, notifications)."""

    wait_time = between(1, 4)

    def on_start(self):
        self.enabled = bool(CUSTOMER_TOKEN)
        if self.enabled:
            self.client.headers.update({"Authorization": f"Bearer {CUSTOMER_TOKEN}"})

    def _skip(self):
        return not getattr(self, "enabled", False)

    @tag("auth")
    @task(4)
    def me(self):
        if self._skip():
            return
        self.client.get("/api/auth/me/", name="GET /api/auth/me")

    @tag("auth")
    @task(5)
    def orders_list(self):
        if self._skip():
            return
        self.client.get("/api/orders/?limit=20&offset=0", name="GET /api/orders")

    @tag("auth")
    @task(3)
    def orders_active(self):
        if self._skip():
            return
        self.client.get("/api/orders/?active_only=true", name="GET /api/orders [active]")

    @tag("auth")
    @task(3)
    def search(self):
        if self._skip():
            return
        self.client.get(
            f"/api/search/?q=chips&lat={LAT}&lng={LNG}",
            name="GET /api/search",
        )

    @tag("auth")
    @task(2)
    def notifications(self):
        if self._skip():
            return
        self.client.get("/api/notifications/", name="GET /api/notifications")

    @tag("auth")
    @task(2)
    def wallet(self):
        if self._skip():
            return
        self.client.get("/api/wallets/me/", name="GET /api/wallets/me")

    @tag("auth")
    @task(2)
    def reliability(self):
        if self._skip():
            return
        self.client.get("/api/reliability/", name="GET /api/reliability")

    @tag("auth")
    @task(1)
    def my_reviews(self):
        if self._skip():
            return
        self.client.get("/api/reviews/my/", name="GET /api/reviews/my")

    @tag("auth")
    @task(1)
    def pools(self):
        if self._skip():
            return
        self.client.get("/api/pools/?limit=20&offset=0", name="GET /api/pools")
