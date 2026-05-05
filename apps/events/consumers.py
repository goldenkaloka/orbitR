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
        
        # Security: Check if user has permission to watch this order
        is_authorized = await self._check_order_authorization(user, self.order_id)
        if not is_authorized:
            await self.close()
            return

        self.room_group_name = f'order_{self.order_id}'
        self._notified_nearby = False

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    @database_sync_to_async
    def _check_order_authorization(self, user, order_id):
        from apps.orders.models import Order
        try:
            order = Order.objects.get(id=order_id)
            # Authorization: Customer, Driver, or Restaurant Owner
            if order.customer == user:
                return True
            if order.driver and order.driver.user == user:
                return True
            if order.restaurant and order.restaurant.owner == user:
                return True
            if user.is_staff:
                return True
            return False
        except Order.DoesNotExist:
            return False

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
                    import h3
                    from apps.core.geo_utils import PROXIMITY_RESOLUTION
                    # Use central proximity resolution (usually 11 ~25m)
                    rider_cell = h3.latlng_to_cell(lat, lng, PROXIMITY_RESOLUTION)
                    dest_cell = h3.latlng_to_cell(dest['lat'], dest['lng'], PROXIMITY_RESOLUTION)
                    
                    if rider_cell == dest_cell or dest_cell in h3.grid_disk(rider_cell, 1):
                        self._notified_nearby = True
                        await self.channel_layer.group_send(
                            self.room_group_name,
                            {
                                'type': 'rider_nearby',
                                'order_id': self.order_id,
                                'distance': 0, # Keeping field for frontend compatibility
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

class OrderPoolConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not user or user.is_anonymous:
            await self.close()
            return

        self.pool_id = self.scope['url_route']['kwargs']['pool_id']
        
        # Security: Check if user is a member of this pool
        is_authorized = await self._check_pool_authorization(user, self.pool_id)
        if not is_authorized:
            await self.close()
            return

        self.room_group_name = f'pool_{self.pool_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    @database_sync_to_async
    def _check_pool_authorization(self, user, pool_id):
        from apps.orders.models import OrderPool
        try:
            pool = OrderPool.objects.get(id=pool_id)
            return pool.members.filter(user=user).exists() or user.is_staff
        except OrderPool.DoesNotExist:
            return False

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def pool_update(self, event):
        await self.send(text_data=json.dumps(event))
