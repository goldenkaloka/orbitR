"""Temporary end-to-end order-flow harness (run inside the Django container).

Usage:
    docker exec -e PHASE=create orbitr-django python manage.py shell \
        -c "exec(open('/app/e2e_flow.py').read())"

Phases: create, pay, kitchen, assign, pickup, deliver, inspect.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
REST = "51080a8f-2955-4f90-a92e-f43a01f469bd"
FOOD = "bb60b5be-158b-4eee-bbf7-f6b83a617283"

CTOK = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzkyODY1MTA1LCJpYXQiOjE3OTIyNjAzMDUsImp0aSI6IjVkMzIzNmZhNzRjOTRjODhiMTA5Zjg1OTYwNzIwZGVhIiwidXNlcl9pZCI6Ijk2ZmRmYjFlLTg0NTItNGNmOS1hZjc5LTJhY2IyN2I1YTA5OSJ9."
    "d3NrtajAGODzQGdSLBgqJSJje1t2SUmM2Ps_vexelgI"
)
ATOK = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzkyODY1MTA1LCJpYXQiOjE3OTIyNjAzMDUsImp0aSI6IjRhMzkwZWZmNTYyYTRjMGI4MmEyNDRiN2ZlMDliZTNhIiwidXNlcl9pZCI6ImVkZmJiYjQ0LTA0ZTUtNDQwMy04ZWM3LThhYmM4YjFhMjU0NyJ9."
    "Rx8zpobi7kdQKGSKeZ5ka8h-jPS-fcR_e7LVPfUiIIM"
)
DTOK = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzkyODY1NDk5LCJpYXQiOjE3OTIyNjA2OTksImp0aSI6IjAxOGVhY2RlMzZiZjQzYWI4NGQ4NjBkN2RlYTBkNGVjIiwidXNlcl9pZCI6ImNjNzdhMDI5LWIyYTItNDdlOC1hZDVhLWJlOTBhNzBhMWQxZSJ9."
    "3525j5sxnRHz3TgkZFQ0njpoI40rHIDwB19NmJPsm-w"
)

DRIVER_USER_ID = "cc77a029-b2a2-47e8-ad5a-be90a70a1d1e"
ORDER_FILE = "/app/.e2e_order_id"


def api(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode(), time.perf_counter() - started
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), time.perf_counter() - started


def save_order(order_id):
    with open(ORDER_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(order_id))


def load_order_id():
    with open(ORDER_FILE, encoding="utf-8") as handle:
        return handle.read().strip()


def create():
    payload = {
        "restaurant": REST,
        "items": [{"food": FOOD, "quantity": 2}],
        "fulfillment_type": "DELIVERY",
        "delivery_address": "Ardhi University, Dar es Salaam",
        "delivery_location": {"latitude": -6.78, "longitude": 39.21},
        "notes": "E2E flow test",
    }
    status, raw, seconds = api("POST", "/orders/", CTOK, payload)
    print(f"CREATE status={status} time={seconds:.2f}s")
    print(raw[:1500])
    if status in (200, 201):
        save_order(json.loads(raw)["id"])


def pay():
    from apps.orders.models import Order
    from apps.payments.models import Payment
    from apps.wallets.models import Wallet
    from domain.services.payment_services import CompletePaymentService

    order = Order.objects.get(id=load_order_id())
    print("BEFORE", order.status, order.is_paid, order.total_amount, order.delivery_otp)

    payment = Payment.objects.filter(
        payable_type="order", payable_id=str(order.id)
    ).first()
    if payment is None:
        payment = Payment.objects.create(
            payable_type="order",
            payable_id=str(order.id),
            order=order,
            amount=order.total_amount,
            status="INITIATED",
        )
    print("PAYMENT", payment.id, payment.status, payment.amount)

    CompletePaymentService.execute(
        payment_id=payment.id,
        success=True,
        provider_reference=f"e2e-{int(time.time())}",
    )

    order.refresh_from_db()
    payment.refresh_from_db()
    print("AFTER", order.status, order.is_paid, payment.status, order.delivery_otp)
    print(
        "PAYMENT_TX",
        list(payment.transactions.values("transaction_type", "amount", "status")),
    )
    print(
        "WALLETS",
        list(
            Wallet.objects.filter(
                user__in=[order.customer_id, order.restaurant.owner_id]
            ).values("user__phone", "balance")
        ),
    )


def kitchen():
    order_id = load_order_id()
    for action in ("start-preparing", "ready"):
        status, raw, seconds = api(
            "POST", f"/orders/{order_id}/{action}/", ATOK
        )
        print(f"{action} status={status} time={seconds:.2f}s")
        print(raw[:500])


def assign():
    order_id = load_order_id()

    # Driver self-claim is expected to be refused for admin-dispatch restaurants.
    status, raw, seconds = api(
        "POST", f"/orders/{order_id}/claim-job/", DTOK
    )
    print(f"DRIVER_SELF_CLAIM status={status} time={seconds:.2f}s")
    print(raw[:400])

    status, raw, seconds = api(
        "POST",
        f"/orders/{order_id}/assign-temp-driver/",
        ATOK,
        {"user_id": DRIVER_USER_ID},
    )
    print(f"ADMIN_ASSIGN status={status} time={seconds:.2f}s")
    print(raw[:800])


def arrive():
    from apps.delivery.models import Driver
    from apps.restaurants.models import Restaurant
    from django.contrib.gis.geos import Point

    driver = Driver.objects.get(user_id=DRIVER_USER_ID)
    restaurant = Restaurant.objects.get(id=REST)
    driver.location = Point(
        restaurant.geo_location.x, restaurant.geo_location.y, srid=4326
    )
    driver.save()
    driver.refresh_from_db()
    print("DRIVER_LOCATION", driver.location, "h3=", driver.h3_index)


def pay_restaurant():
    order_id = load_order_id()
    status, raw, seconds = api(
        "POST",
        f"/orders/{order_id}/pay-restaurant/",
        DTOK,
        {"account_number": "0616622485", "network": "mpesa"},
    )
    print(f"PAY_RESTAURANT status={status} time={seconds:.2f}s")
    print(raw[:800])


def simulate_pickup_payment():
    """Mimic a successful gateway callback so the rest of the flow can run."""
    from apps.orders.models import Order
    from apps.payments.models import PickupDisbursement
    from django.utils import timezone
    from domain.services.orders.pickup_payment import restaurant_net_share

    order = Order.objects.get(id=load_order_id())
    amount = restaurant_net_share(order)
    disbursement, _ = PickupDisbursement.objects.update_or_create(
        order=order,
        defaults={
            "batch": order.batch,
            "driver": order.driver,
            "restaurant": order.restaurant,
            "amount": amount,
            "account_number": "0616622485",
            "network": "mpesa",
            "status": PickupDisbursement.STATUS_SUCCESS,
            "gateway_reference": f"E2E-{order.id}",
            "settled_at": timezone.now(),
        },
    )
    order.pickup_paid_amount = amount
    order.save(update_fields=["pickup_paid_amount", "updated_at"])
    print("SIMULATED_PICKUP_PAYMENT", disbursement.id, disbursement.status, amount)


def pickup():
    order_id = load_order_id()
    status, raw, seconds = api(
        "POST",
        f"/orders/{order_id}/pickup/",
        DTOK,
        {"bypass_geofence": True},
    )
    print(f"PICKUP status={status} time={seconds:.2f}s")
    print(raw[:800])


def deliver():
    from apps.orders.models import Order
    from apps.payments.models import Payment
    from apps.wallets.models import Wallet

    order_id = load_order_id()
    order = Order.objects.get(id=order_id)
    status, raw, seconds = api(
        "POST",
        f"/orders/{order_id}/deliver/",
        DTOK,
        {"otp": order.delivery_otp, "bypass_geofence": True},
    )
    print(f"DELIVER status={status} time={seconds:.2f}s")
    print(raw[:1200])

    order.refresh_from_db()
    print(
        "FINAL",
        order.status,
        "paid=",
        order.is_paid,
        "settled=",
        order.is_settled,
        "delivered_at=",
        order.delivered_at,
    )
    payment = Payment.objects.filter(payable_id=str(order.id)).first()
    print("PAYMENT", payment.status if payment else None)
    print(
        "WALLETS",
        list(
            Wallet.objects.filter(
                user__in=[order.customer_id, order.restaurant.owner_id]
            ).values("user__phone", "balance")
        ),
    )


def inspect():
    from apps.orders.models import Order
    from apps.payments.models import Payment
    from apps.wallets.models import Wallet

    order = Order.objects.get(id=load_order_id())
    print("ORDER", order.id)
    print(
        "STATE",
        "status=",
        order.status,
        "paid=",
        order.is_paid,
        "settled=",
        order.is_settled,
        "driver=",
        order.driver_id,
        "batch=",
        order.batch_id,
        "otp=",
        order.delivery_otp,
    )
    print(
        "TIMES",
        "created=",
        order.created_at,
        "confirmed=",
        order.confirmed_at,
        "preparing=",
        order.preparing_at,
        "ready=",
        order.ready_at,
        "picked=",
        order.picked_up_at,
        "delivered=",
        order.delivered_at,
    )
    print(
        "TOTALS",
        order.items_subtotal,
        order.delivery_fee,
        order.service_fee,
        order.total_amount,
    )
    payment = Payment.objects.filter(payable_id=str(order.id)).first()
    if payment:
        print("PAYMENT", payment.id, payment.status, payment.amount)
        print(
            "LEDGER",
            list(
                payment.ledger_entries.values(
                    "entry_type", "amount", "debit_wallet__user__phone", "credit_wallet__user__phone"
                )
            ),
        )
    print(
        "WALLETS",
        list(
            Wallet.objects.filter(
                user__in=[order.customer_id, order.restaurant.owner_id]
            ).values("user__phone", "balance", "pending_balance")
        ),
    )


PHASE = os.environ.get("PHASE")
{
    "create": create,
    "pay": pay,
    "kitchen": kitchen,
    "assign": assign,
    "arrive": arrive,
    "pay_restaurant": pay_restaurant,
    "simulate_pickup_payment": simulate_pickup_payment,
    "pickup": pickup,
    "deliver": deliver,
    "inspect": inspect,
}.get(PHASE, inspect)()
