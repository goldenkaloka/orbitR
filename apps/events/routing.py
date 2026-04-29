from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/orders/(?P<order_id>[0-9a-f-]+)/$', consumers.OrderConsumer.as_asgi()),
    re_path(r'ws/vendor/(?P<vendor_id>[0-9a-f-]+)/$', consumers.VendorConsumer.as_asgi()),
    re_path(r'ws/rider/(?P<rider_id>[0-9a-f-]+)/$', consumers.RiderConsumer.as_asgi()),
    re_path(r'ws/user/(?P<user_id>[0-9a-f-]+)/$', consumers.UserConsumer.as_asgi()),
    re_path(r'ws/riders/$', consumers.RidersPoolConsumer.as_asgi()),
]
