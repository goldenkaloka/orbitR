import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

import requests
from domain.entities.route import Route
from domain.events.bus import EventBus
from domain.events.types import RouteCalculated, RouteOptimizationFailed

logger = logging.getLogger(__name__)

class MapboxRoutingService:
    """Mapbox routing service with DDD value objects and event emission."""
    
    _event_bus: Optional[EventBus] = None

    @classmethod
    def _get_event_bus(cls) -> EventBus:
        if cls._event_bus is None:
            try:
                from domain.events.bus import EventBus
                cls._event_bus = EventBus.get_instance()
            except Exception:
                pass
        return cls._event_bus

    @classmethod
    def _emit_event(cls, event_type: str, batch_id: Optional[UUID], payload: Dict[str, Any]) -> None:
        """Emit domain event for route calculation."""
        bus = cls._get_event_bus()
        if bus and batch_id:
            try:
                if event_type == 'success':
                    event = RouteCalculated(
                        aggregate_id=batch_id,
                        payload=payload
                    )
                else:
                    event = RouteOptimizationFailed(
                        aggregate_id=batch_id,
                        payload=payload
                    )
                bus.publish(event)
            except Exception as e:
                logger.warning(f"Failed to emit route event: {e}")

    @staticmethod
    def _get_token() -> Optional[str]:
        import os
        return os.environ.get('MAPBOX_TOKEN') or os.environ.get('MAPBOX_ACCESS_TOKEN')

    @staticmethod
    def get_directions_route(
        origin_lng: float,
        origin_lat: float,
        destination_lng: float,
        destination_lat: float,
        origin_info: Optional[Dict[str, str]] = None,
        destination_info: Optional[Dict[str, str]] = None
    ) -> Optional[Route]:
        """Get route for single destination using Mapbox Directions API."""
        token = MapboxRoutingService._get_token()
        if not token:
            logger.warning("Mapbox token not configured")
            return None

        url = (
            f"https://api.mapbox.com/directions/v5/mapbox/walking/"
            f"{origin_lng},{origin_lat};{destination_lng},{destination_lat}"
        )
        params = {
            'geometries': 'geojson',
            'overview': 'full',
            'access_token': token
        }

        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                logger.warning(f"Mapbox Directions API error: {response.status_code}")
                return None

            data = response.json()
            if 'routes' not in data or len(data['routes']) == 0:
                return None

            route_data = data['routes'][0]
            distance_km = route_data['distance'] / 1000
            duration_min = route_data['duration'] / 60

            stop_sequence = []
            if origin_info:
                stop_sequence.append({
                    'order_id': origin_info.get('order_id'),
                    'restaurant_id': origin_info.get('restaurant_id'),
                    'type': origin_info.get('type'),
                    'lat': origin_info.get('lat'),
                    'lng': origin_info.get('lng'),
                    'estimated_arrival_min': 0
                })
            if destination_info:
                stop_sequence.append({
                    'order_id': destination_info.get('order_id'),
                    'restaurant_id': destination_info.get('restaurant_id'),
                    'type': destination_info.get('type'),
                    'lat': destination_info.get('lat'),
                    'lng': destination_info.get('lng'),
                    'estimated_arrival_min': int(duration_min)
                })

            return Route.from_dict({
                'total_distance_km': round(distance_km, 2),
                'total_duration_min': int(duration_min),
                'stop_sequence': stop_sequence,
                'geometry': route_data['geometry'],
                'coordinates': route_data['geometry']['coordinates']
            })

        except Exception as e:
            logger.error(f"Mapbox Directions API failed: {str(e)}")
            return None

    @staticmethod
    def get_optimized_route(
        coordinates: List[Dict[str, Any]],
        profile: str = 'walking'
    ) -> Optional[Route]:
        """Get optimized route for multiple stops using Mapbox Optimization API."""
        token = MapboxRoutingService._get_token()
        if not token:
            logger.warning("Mapbox token not configured")
            return None

        if len(coordinates) < 2:
            logger.warning("Need at least 2 coordinates for optimization")
            return None

        coord_string = ';'.join([f"{c['lng']},{c['lat']}" for c in coordinates])

        url = f"https://api.mapbox.com/optimized-trips/v1/mapbox/{profile}/{coord_string}"
        params = {
            'geometries': 'geojson',
            'overview': 'full',
            'steps': 'false',
            'access_token': token,
            'roundtrip': 'false',
            'source': 'first',
            'destination': 'last'
        }

        try:
            response = requests.get(url, params=params, timeout=8)

            if response.status_code == 429:
                logger.warning("Mapbox optimization rate limited, falling back")
                return None

            if response.status_code != 200:
                logger.warning(
                    f"Mapbox Optimization API error: {response.status_code}"
                )
                return None

            data = response.json()

            if 'waypoints' not in data or 'route' not in data:
                logger.warning("Mapbox optimization response missing data")
                return None

            route_data = data['route']
            waypoints = data['waypoints']

            total_distance_km = route_data['distance'] / 1000
            total_duration_min = route_data['duration'] / 60

            stop_sequence = []
            cumulative_time = 0
            for wp in waypoints:
                waypoint_index = wp.get('waypoint_index', 0)
                coord = coordinates[waypoint_index] if waypoint_index < len(coordinates) else {}
                cumulative_time += wp.get('time', 0) / 60

                stop_sequence.append({
                    'order_id': coord.get('order_id'),
                    'restaurant_id': coord.get('restaurant_id'),
                    'type': coord.get('type'),
                    'lat': coord.get('lat'),
                    'lng': coord.get('lng'),
                    'waypoint_index': waypoint_index,
                    'estimated_arrival_min': int(cumulative_time)
                })

            return Route.from_dict({
                'total_distance_km': round(total_distance_km, 2),
                'total_duration_min': int(total_duration_min),
                'stop_sequence': stop_sequence,
                'geometry': route_data['geometry'],
                'coordinates': route_data['geometry']['coordinates']
            })

        except requests.exceptions.Timeout:
            logger.error("Mapbox Optimization API timed out")
            return None
        except Exception as e:
            logger.error(f"Mapbox Optimization failed: {str(e)}")
            return None

    @classmethod
    def get_route_for_batch(
        cls,
        orders,
        batch_id: Optional[UUID] = None
    ) -> Optional[Route]:
        """Get optimized route for a batch of orders with event emission."""
        if not orders:
            return None

        coordinates = []

        for order in orders:
            if order.restaurant and order.restaurant.geo_location:
                coordinates.append({
                    'lng': float(order.restaurant.geo_location.x),
                    'lat': float(order.restaurant.geo_location.y),
                    'order_id': str(order.id),
                    'restaurant_id': str(order.restaurant_id),
                    'type': 'pickup',
                    'name': order.restaurant.name
                })

            if order.delivery_location:
                coordinates.append({
                    'lng': float(order.delivery_location.x),
                    'lat': float(order.delivery_location.y),
                    'order_id': str(order.id),
                    'restaurant_id': str(order.restaurant_id),
                    'type': 'delivery',
                    'address': order.delivery_address
                })

        result = cls.get_optimized_route(coordinates)

        if result:
            cls._emit_event('success', batch_id, result.to_dict())
            return result

        logger.warning("Mapbox optimization failed, falling back to Directions API")

        if len(coordinates) >= 2:
            origin = coordinates[0]
            destination = coordinates[-1]
            result = cls.get_directions_route(
                origin['lng'], origin['lat'],
                destination['lng'], destination['lat'],
                origin_info=origin,
                destination_info=destination
            )
            if result:
                cls._emit_event('success', batch_id, result.to_dict())
            return result

        cls._emit_event('failed', batch_id, {'error': 'No coordinates available'})
        return None
