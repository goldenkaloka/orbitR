import json
import math
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

class OrderConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not user or user.is_anonymous:
            await self.close()
            return

        self.order_id = self.scope['url_route']['kwargs']['order_id']
        self.room_group_name = f'order_{self.order_id}'
        self._notified_nearby = False

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        if data.get('type') == 'location_update':
            lat = data.get('lat')
            lng = data.get('lng')

            # Broadcast location to the group (customer & vendor)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'location_update',
                    'order_id': self.order_id,
                    'lat': lat,
                    'lng': lng,
                }
            )

            # Geofence check: is rider near destination?
            if not self._notified_nearby:
                dest = await self._get_delivery_coords()
                if dest:
                    distance = self._haversine(lat, lng, dest['lat'], dest['lng'])
                    if distance < 100:  # 100 meters
                        self._notified_nearby = True
                        await self.channel_layer.group_send(
                            self.room_group_name,
                            {
                                'type': 'rider_nearby',
                                'order_id': self.order_id,
                                'distance': round(distance, 1),
                                'message': 'Your rider is almost here! 📍',
                            }
                        )

    @database_sync_to_async
    def _get_delivery_coords(self):
        from apps.orders.models import Order
        try:
            order = Order.objects.get(id=self.order_id)
            if order.delivery_latitude and order.delivery_longitude:
                return {'lat': float(order.delivery_latitude), 'lng': float(order.delivery_longitude)}
        except Order.DoesNotExist:
            pass
        return None

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        """Calculate distance in meters between two GPS coordinates."""
        R = 6371000  # Earth radius in meters
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    async def order_status_update(self, event):
        await self.send(text_data=json.dumps(event))

    async def location_update(self, event):
        await self.send(text_data=json.dumps(event))

    async def rider_nearby(self, event):
        await self.send(text_data=json.dumps(event))

class VendorConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        self.vendor_id = self.scope['url_route']['kwargs']['vendor_id']
        
        # Security: User must be authenticated and match the vendor_id
        if not user or user.is_anonymous or str(user.id) != self.vendor_id:
            await self.close()
            return

        self.room_group_name = f'vendor_{self.vendor_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def order_status_update(self, event):
        await self.send(text_data=json.dumps(event))

class RiderConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        self.rider_id = self.scope['url_route']['kwargs']['rider_id']
        
        # Security: User must be authenticated and match the rider_id
        if not user or user.is_anonymous or str(user.id) != self.rider_id:
            await self.close()
            return

        self.room_group_name = f'rider_{self.rider_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def order_status_update(self, event):
        await self.send(text_data=json.dumps(event))

class UserConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        self.user_id = self.scope['url_route']['kwargs']['user_id']
        
        # Security: User must be authenticated and match the user_id
        if not user or user.is_anonymous or str(user.id) != self.user_id:
            await self.close()
            return

        self.room_group_name = f'user_{self.user_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def order_status_update(self, event):
        await self.send(text_data=json.dumps(event))

class RidersPoolConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not user or user.is_anonymous or user.role != 'driver':
            await self.close()
            return

        self.room_group_name = 'riders'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def new_order_ping(self, event):
        await self.send(text_data=json.dumps(event))
