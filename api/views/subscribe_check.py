import json
import logging
from django.http import JsonResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

def _parse_channel(channel):
    if ':' not in channel:
        return None, None
    namespace, _, key = channel.partition(':')
    return namespace, key

@csrf_exempt
@require_POST
def ws_subscribe_check(request):
    centrifugo_secret = request.headers.get('X-Centrifugo-Proxy-Secret', '')
    if centrifugo_secret != getattr(settings, 'CENTRIFUGO_PROXY_SECRET', ''):
        return JsonResponse(
            {"disconnect": {"code": 4001, "reason": "unauthorized"}},
            status=401,
        )

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"disconnect": {"code": 4000, "reason": "invalid body"}}, status=400)

    user = body.get("user", "")
    channel = body.get("channel", "")
    namespace, key = _parse_channel(channel)

    if not user or not channel or not namespace:
        return JsonResponse({"result": {"allowed": False}}, status=200)

    from apps.users.models import User as UserModel
    try:
        user_obj = UserModel.objects.get(id=user)
    except (UserModel.DoesNotExist, ValueError):
        return JsonResponse({"result": {"allowed": False}}, status=200)

    role = user_obj.role

    if namespace == "orders":
        from apps.orders.models import Order
        try:
            order = Order.objects.get(id=key)
        except Order.DoesNotExist:
            return JsonResponse({"result": {"allowed": False}}, status=200)
        allowed = (
            str(order.customer_id) == user
            or (order.driver and str(order.driver.user_id) == user)
            or (order.restaurant and str(order.restaurant.owner_id) == user)
            or role == "admin"
        )
        return JsonResponse({"result": {"allowed": allowed}}, status=200)

    if namespace == "pools":
        from apps.orders.models import OrderPool
        try:
            pool = OrderPool.objects.get(id=key)
        except OrderPool.DoesNotExist:
            return JsonResponse({"result": {"allowed": False}}, status=200)
        allowed = pool.members.filter(user_id=user).exists() or role == "admin"
        return JsonResponse({"result": {"allowed": allowed}}, status=200)

    if namespace == "users":
        allowed = key == user or role == "admin"
        return JsonResponse({"result": {"allowed": allowed}}, status=200)

    if namespace == "vendors":
        from apps.restaurants.models import Restaurant
        allowed = (
            Restaurant.objects.filter(owner_id=user).exists()
            or role == "admin"
        )
        return JsonResponse({"result": {"allowed": allowed}}, status=200)

    if namespace == "riders":
        allowed = key == user or role == "admin"
        return JsonResponse({"result": {"allowed": allowed}}, status=200)

    if namespace == "riders_global":
        allowed = role == "driver" or role == "admin"
        return JsonResponse({"result": {"allowed": allowed}}, status=200)

    return JsonResponse({"result": {"allowed": False}}, status=200)
