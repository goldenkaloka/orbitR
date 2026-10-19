"""Batch dispatch path harness (temporary, run inside the Django container).

Phases: create, pay, kitchen, section, assign, pay_restaurant, finish, inspect.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
REST = "b12988be-fb72-4f49-9ef4-ea6370437048"
FOOD = "8fa5f8bf-c976-4dba-b5d9-2633087b3c84"
PLATFORM_DRIVER_ID = "be9efe62-8784-45b0-ac72-9fddd869fa22"
ORDER_FILE = "/app/.e2e_batch_order_id"


def token(phone):
    from apps.users.models import User
    from rest_framework_simplejwt.tokens import RefreshToken

    return str(RefreshToken.for_user(User.objects.get(phone=phone)).access_token)


def api(method, path, tok, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {tok}",
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


def save(order_id):
    with open(ORDER_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(order_id))


def load():
    with open(ORDER_FILE, encoding="utf-8") as handle:
        return handle.read().strip()


def create():
    payload = {
        "restaurant": REST,
        "items": [{"food": FOOD, "quantity": 1}],
        "fulfillment_type": "DELIVERY",
        "delivery_address": "Mama Amina area, Dar es Salaam",
        "delivery_location": {"latitude": -6.79, "longitude": 39.20},
        "notes": "E2E batch flow test",
    }
    status, raw, seconds = api("POST", "/orders/", token("0748322485"), payload)
    print(f"CREATE status={status} time={seconds:.2f}s")
    print(raw[:600])
    if status in (200, 201):
        save(json.loads(raw)["id"])


def pay():
    from apps.orders.models import Order
    from apps.payments.models import Payment
    from domain.services.payment_services import CompletePaymentService

    order = Order.objects.get(id=load())
    payment, _ = Payment.objects.get_or_create(
        payable_type="order",
        payable_id=str(order.id),
        defaults={"order": order, "amount": order.total_amount, "status": "INITIATED"},
    )
    CompletePaymentService.execute(
        payment_id=payment.id, success=True, provider_reference=f"e2e-batch-{int(time.time())}"
    )
    order.refresh_from_db()
    print("PAID", order.status, order.is_paid, payment.status, "batch", order.batch_id)


def kitchen():
    order_id = load()
    for action in ("start-preparing", "ready"):
        status, raw, seconds = api("POST", f"/orders/{order_id}/{action}/", token("0616622485"))
        print(f"{action} status={status} time={seconds:.2f}s", raw[:160])


def section():
    from apps.orders.models import BatchSectionStatus, Order

    order = Order.objects.get(id=load())
    order.refresh_from_db()
    print("BATCH_BEFORE", order.batch_id, order.batch.status if order.batch else None)
    if not order.batch:
        print("NO BATCH")
        return
    section_id = order.items.first().section_id
    bss = BatchSectionStatus.objects.filter(
        batch=order.batch, section_id=section_id
    ).first()
    print("BSS", bss.id if bss else None, bss.status if bss else None)
    if bss:
        status, raw, seconds = api(
            "POST",
            f"/batch-section-statuses/{bss.id}/mark_ready/",
            token("0616622485"),
        )
        print(f"MARK_READY status={status} time={seconds:.2f}s", raw[:300])
    for _ in range(10):
        order.refresh_from_db()
        print("BATCH_STATUS", order.batch.status, "orders", order.batch.orders.count())
        if order.batch.status in ("PENDING_DRIVER", "PRE_ASSIGNING"):
            break
        time.sleep(2)


def assign():
    from apps.orders.models import Order

    order = Order.objects.get(id=load())
    batch_id = order.batch_id
    status, raw, seconds = api(
        "POST",
        f"/batches/{batch_id}/assign-driver/",
        token("0616622485"),
        {"driver_id": PLATFORM_DRIVER_ID},
    )
    print(f"ASSIGN_BATCH status={status} time={seconds:.2f}s")
    print(raw[:700])
    order.refresh_from_db()
    print("ORDER", order.status, "driver", order.driver_id, "batch", order.batch.status)


def pay_restaurant():
    from apps.users.models import User

    order_id = load()
    status, raw, seconds = api(
        "POST",
        f"/orders/{order_id}/pay-restaurant/",
        token("+255700000098"),
        {"account_number": "0616622485", "network": "mpesa"},
    )
    print(f"PAY_RESTAURANT status={status} time={seconds:.2f}s", raw[:400])


def finish():
    from apps.delivery.models import Driver
    from apps.orders.models import Order
    from apps.payments.models import PickupDisbursement
    from django.contrib.gis.geos import Point
    from django.utils import timezone
    from domain.services.orders.pickup_payment import restaurant_net_share

    order = Order.objects.get(id=load())
    driver = Driver.objects.get(id=PLATFORM_DRIVER_ID)
    driver.location = Point(
        order.restaurant.geo_location.x, order.restaurant.geo_location.y, srid=4326
    )
    driver.save()
    driver.refresh_from_db()

    amount = restaurant_net_share(order)
    PickupDisbursement.objects.update_or_create(
        order=order,
        defaults={
            "driver": driver,
            "restaurant": order.restaurant,
            "amount": amount,
            "account_number": "0616622485",
            "network": "mpesa",
            "status": PickupDisbursement.STATUS_SUCCESS,
            "gateway_reference": f"E2E-BATCH-{order.id}",
            "settled_at": timezone.now(),
        },
    )
    order.pickup_paid_amount = amount
    order.save(update_fields=["pickup_paid_amount", "updated_at"])

    tok = token("+255700000098")
    status, raw, seconds = api(
        "POST", f"/orders/{order.id}/pickup/", tok, {"bypass_geofence": True}
    )
    print(f"PICKUP status={status} time={seconds:.2f}s", raw[:200])

    order.refresh_from_db()
    status, raw, seconds = api(
        "POST",
        f"/orders/{order.id}/deliver/",
        tok,
        {"otp": "0000", "bypass_geofence": True},
    )
    print(f"WRONG_OTP status={status} time={seconds:.2f}s", raw[:200])

    status, raw, seconds = api(
        "POST",
        f"/orders/{order.id}/deliver/",
        tok,
        {"otp": order.delivery_otp, "bypass_geofence": True},
    )
    print(f"DELIVER status={status} time={seconds:.2f}s", raw[:300])

    order.refresh_from_db()
    print(
        "FINAL",
        order.status,
        "paid=",
        order.is_paid,
        "settled=",
        order.is_settled,
        "batch=",
        order.batch.status,
        "driver=",
        order.driver_id,
    )


def inspect():
    from apps.orders.models import Order
    from apps.payments.models import Payment

    order = Order.objects.get(id=load())
    payment = Payment.objects.filter(payable_id=str(order.id)).first()
    print(
        "ORDER",
        order.status,
        "batch=",
        order.batch.status if order.batch else None,
        "driver=",
        order.driver_id,
        "paid=",
        order.is_paid,
        "settled=",
        order.is_settled,
    )
    print("PAYMENT", payment.status if payment else None)
    print(
        "BATCH_ORDERS",
        [
            (str(o.id)[:8], o.status, str(o.driver_id)[:8])
            for o in (order.batch.orders.all() if order.batch else [])
        ],
    )


PHASE = os.environ.get("PHASE")
{
    "create": create,
    "pay": pay,
    "kitchen": kitchen,
    "section": section,
    "assign": assign,
    "pay_restaurant": pay_restaurant,
    "finish": finish,
    "inspect": inspect,
}.get(PHASE, inspect)()
