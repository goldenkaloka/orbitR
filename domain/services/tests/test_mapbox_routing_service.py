import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from domain.entities.route import GeoCoordinate, Route, Stop
from domain.services.mapbox_routing_service import MapboxRoutingService


class MapboxRoutingServiceTests(TestCase):
    """Unit tests for MapboxRoutingService"""

    def test_geo_coordinate_creation(self):
        """Test GeoCoordinate value object"""
        coord = GeoCoordinate(lng=39.2026, lat=-6.7924)

        self.assertEqual(coord.lng, 39.2026)
        self.assertEqual(coord.lat, -6.7924)
        self.assertEqual(coord.to_dict(), {'lng': 39.2026, 'lat': -6.7924})

    def test_stop_creation_pickup(self):
        """Test Stop value object for pickup"""
        coord = GeoCoordinate(lng=39.2026, lat=-6.7924)
        stop = Stop(
            order_id='123',
            stop_type='pickup',
            geo_location=coord,
            estimated_arrival_min=0,
            waypoint_index=0
        )

        self.assertTrue(stop.is_pickup)
        self.assertFalse(stop.is_delivery)
        self.assertEqual(stop.order_id, '123')

    def test_stop_creation_delivery(self):
        """Test Stop value object for delivery"""
        coord = GeoCoordinate(lng=39.2100, lat=-6.8000)
        stop = Stop(
            order_id='123',
            stop_type='delivery',
            geo_location=coord,
            estimated_arrival_min=15,
            waypoint_index=1
        )

        self.assertFalse(stop.is_pickup)
        self.assertTrue(stop.is_delivery)
        self.assertEqual(stop.estimated_arrival_min, 15)

    def test_route_creation_empty(self):
        """Test Route creation with no stops"""
        route = Route(
            total_distance_km=0,
            total_duration_min=0,
            stops=[]
        )

        self.assertTrue(route.is_empty)
        self.assertEqual(route.num_pickups, 0)
        self.assertEqual(route.num_deliveries, 0)
        self.assertEqual(route.total_stops, 0)

    def test_route_creation_with_stops(self):
        """Test Route creation with multiple stops"""
        coord1 = GeoCoordinate(lng=39.2026, lat=-6.7924)
        coord2 = GeoCoordinate(lng=39.2100, lat=-6.8000)

        stops = [
            Stop(order_id='123', stop_type='pickup', geo_location=coord1, estimated_arrival_min=0, waypoint_index=0),
            Stop(order_id='123', stop_type='delivery', geo_location=coord2, estimated_arrival_min=20, waypoint_index=1)
        ]

        route = Route(
            total_distance_km=10.5,
            total_duration_min=25,
            stops=stops
        )

        self.assertFalse(route.is_empty)
        self.assertEqual(route.total_distance_km, 10.5)
        self.assertEqual(route.total_duration_min, 25)
        self.assertEqual(route.num_pickups, 1)
        self.assertEqual(route.num_deliveries, 1)
        self.assertEqual(route.total_stops, 2)

    def test_route_from_dict(self):
        """Test Route creation from dictionary"""
        data = {
            'total_distance_km': 15.4,
            'total_duration_min': 30,
            'stop_sequence': [
                {'order_id': '123', 'type': 'pickup', 'estimated_arrival_min': 0, 'lng': 39.0, 'lat': -6.8},
                {'order_id': '123', 'type': 'delivery', 'estimated_arrival_min': 30, 'lng': 39.1, 'lat': -6.9}
            ],
            'geometry': {'type': 'LineString', 'coordinates': [[39.0, -6.8], [39.1, -6.9]]},
            'coordinates': [[39.0, -6.8], [39.1, -6.9]]
        }

        route = Route.from_dict(data)

        self.assertEqual(route.total_distance_km, 15.4)
        self.assertEqual(route.total_duration_min, 30)
        self.assertEqual(len(route.stops), 2)

    def test_route_to_dict(self):
        """Test Route serialization to dictionary"""
        coord1 = GeoCoordinate(lng=39.2026, lat=-6.7924)
        coord2 = GeoCoordinate(lng=39.2100, lat=-6.8000)

        stops = [
            Stop(order_id='123', stop_type='pickup', geo_location=coord1, estimated_arrival_min=0, waypoint_index=0),
            Stop(order_id='123', stop_type='delivery', geo_location=coord2, estimated_arrival_min=20, waypoint_index=1)
        ]

        route = Route(
            total_distance_km=10.5,
            total_duration_min=25,
            stops=stops,
            geometry={'type': 'LineString', 'coordinates': [[39.2026, -6.7924], [39.2100, -6.8000]]},
            coordinates=[[39.2026, -6.7924], [39.2100, -6.8000]]
        )

        route_dict = route.to_dict()

        self.assertEqual(route_dict['total_distance_km'], 10.5)
        self.assertEqual(route_dict['total_duration_min'], 25)
        self.assertEqual(len(route_dict['stop_sequence']), 2)

    @patch('domain.services.mapbox_routing_service.requests.get')
    def test_get_directions_route_returns_none_without_token(self, mock_get):
        """Test that Directions API returns None without token"""
        import os
        original_token = os.environ.get('MAPBOX_ACCESS_TOKEN')
        if 'MAPBOX_ACCESS_TOKEN' in os.environ:
            del os.environ['MAPBOX_ACCESS_TOKEN']

        try:
            result = MapboxRoutingService.get_directions_route(
                39.2026, -6.7924, 39.2100, -6.8000
            )
            self.assertIsNone(result)
        finally:
            if original_token:
                os.environ['MAPBOX_ACCESS_TOKEN'] = original_token


class MapboxRoutingServiceIntegrationTests(TestCase):
    """Integration tests for MapboxRoutingService (with mocked responses)"""

    @patch('domain.services.mapbox_routing_service.requests.get')
    def test_get_directions_route_parses_response(self, mock_get):
        """Test that Directions API parses response correctly"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'routes': [{
                'distance': 5000,
                'duration': 900,
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[39.2026, -6.7924], [39.2100, -6.8000]]
                }
            }]
        }
        mock_get.return_value = mock_response

        result = MapboxRoutingService.get_directions_route(
            39.2026, -6.7924, 39.2100, -6.8000,
            origin_info={'order_id': '123', 'type': 'pickup'},
            destination_info={'order_id': '123', 'type': 'delivery'}
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.total_distance_km, 5.0)
        self.assertEqual(result.total_duration_min, 15)
        self.assertEqual(len(result.stops), 2)

    @patch('domain.services.mapbox_routing_service.requests.get')
    def test_get_optimized_route_returns_none_for_rate_limit(self, mock_get):
        """Test that optimization returns None on rate limit (429)"""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        coordinates = [
            {'lng': 39.2026, 'lat': -6.7924, 'order_id': '1', 'type': 'pickup'},
            {'lng': 39.2100, 'lat': -6.8000, 'order_id': '1', 'type': 'delivery'}
        ]

        result = MapboxRoutingService.get_optimized_route(coordinates)

        self.assertIsNone(result)

    @patch('domain.services.mapbox_routing_service.requests.get')
    def test_get_optimized_route_parses_waypoints(self, mock_get):
        """Test that Optimization API parses waypoints correctly"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'waypoints': [
                {'waypoint_index': 0, 'time': 0},
                {'waypoint_index': 1, 'time': 600},
                {'waypoint_index': 2, 'time': 900},
                {'waypoint_index': 3, 'time': 1500}
            ],
            'route': {
                'distance': 10000,
                'duration': 1800,
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[39.2026, -6.7924], [39.2050, -6.7950], [39.2100, -6.8000]]
                }
            }
        }
        mock_get.return_value = mock_response

        coordinates = [
            {'lng': 39.2026, 'lat': -6.7924, 'order_id': '1', 'type': 'pickup'},
            {'lng': 39.2050, 'lat': -6.7950, 'order_id': '2', 'type': 'pickup'},
            {'lng': 39.2100, 'lat': -6.8000, 'order_id': '1', 'type': 'delivery'},
            {'lng': 39.2100, 'lat': -6.8000, 'order_id': '2', 'type': 'delivery'}
        ]

        result = MapboxRoutingService.get_optimized_route(coordinates)

        self.assertIsNotNone(result)
        self.assertEqual(result.total_distance_km, 10.0)
        self.assertEqual(result.total_duration_min, 30)
        self.assertEqual(len(result.stops), 4)


class RouteValueObjectTests(TestCase):
    """Tests for Route value object behavior"""

    def test_route_has_geometry_returns_true_when_geometry_set(self):
        """Test has_geometry property"""
        coord = GeoCoordinate(lng=39.2026, lat=-6.7924)
        stop = Stop(order_id='123', stop_type='pickup', geo_location=coord)

        route = Route(
            total_distance_km=5.0,
            total_duration_min=15,
            stops=[stop],
            geometry={'type': 'LineString', 'coordinates': [[39.2026, -6.7924]]}
        )

        self.assertTrue(route.has_geometry)

    def test_route_has_geometry_returns_false_when_none(self):
        """Test has_geometry property when None"""
        route = Route(
            total_distance_km=5.0,
            total_duration_min=15,
            stops=[]
        )

        self.assertFalse(route.has_geometry)

    def test_route_frozen_is_immutable(self):
        """Test that Route is frozen (immutable)"""
        coord = GeoCoordinate(lng=39.2026, lat=-6.7924)
        route = Route(
            total_distance_km=5.0,
            total_duration_min=15,
            stops=[],
            geometry={'type': 'LineString', 'coordinates': [[39.2026, -6.7924]]}
        )

        with self.assertRaises(AttributeError):
            route.total_distance_km = 10.0

    def test_geo_coordinate_is_hashable(self):
        """Test that GeoCoordinate can be hashed"""
        coord1 = GeoCoordinate(lng=39.2026, lat=-6.7924)
        coord2 = GeoCoordinate(lng=39.2026, lat=-6.7924)
        coord3 = GeoCoordinate(lng=39.2100, lat=-6.8000)

        self.assertEqual(hash(coord1), hash(coord2))
        self.assertNotEqual(hash(coord1), hash(coord3))