from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from uuid import UUID


@dataclass(frozen=True)
class GeoCoordinate:
    lng: float
    lat: float

    def to_dict(self) -> Dict[str, float]:
        return {'lng': self.lng, 'lat': self.lat}


@dataclass(frozen=True)
class Stop:
    order_id: str
    stop_type: str
    geo_location: GeoCoordinate
    estimated_arrival_min: int = 0
    waypoint_index: int = 0

    @property
    def is_pickup(self) -> bool:
        return self.stop_type == 'pickup'

    @property
    def is_delivery(self) -> bool:
        return self.stop_type == 'delivery'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'order_id': self.order_id,
            'type': self.stop_type,
            'lng': self.geo_location.lng,
            'lat': self.geo_location.lat,
            'estimated_arrival_min': self.estimated_arrival_min,
            'waypoint_index': self.waypoint_index
        }


@dataclass(frozen=True)
class Route:
    total_distance_km: float
    total_duration_min: int
    stops: List[Stop] = field(default_factory=list)
    geometry: Optional[Dict[str, Any]] = field(default=None, compare=False)
    coordinates: List[List[float]] = field(default_factory=list, compare=False)

    @property
    def is_empty(self) -> bool:
        return len(self.stops) == 0

    @property
    def num_pickups(self) -> int:
        return sum(1 for s in self.stops if s.is_pickup)

    @property
    def num_deliveries(self) -> int:
        return sum(1 for s in self.stops if s.is_delivery)

    @property
    def total_stops(self) -> int:
        return len(self.stops)

    @property
    def has_geometry(self) -> bool:
        return self.geometry is not None

    @classmethod
    def from_mapbox_response(
        cls,
        response: Dict[str, Any],
        coordinates_data: List[Dict[str, Any]]
    ) -> 'Route':
        stop_sequence = response.get('stop_sequence', [])
        stops = []

        for stop_data in stop_sequence:
            coord_idx = stop_data.get('waypoint_index', 0)
            coord_info = coordinates_data[coord_idx] if coord_idx < len(coordinates_data) else {}

            geo = GeoCoordinate(
                lng=coord_info.get('lng', 0),
                lat=coord_info.get('lat', 0)
            )

            stops.append(Stop(
                order_id=stop_data.get('order_id', ''),
                stop_type=stop_data.get('type', 'unknown'),
                geo_location=geo,
                estimated_arrival_min=stop_data.get('estimated_arrival_min', 0),
                waypoint_index=stop_data.get('waypoint_index', 0)
            ))

        return cls(
            total_distance_km=response.get('total_distance_km', 0),
            total_duration_min=response.get('total_duration_min', 0),
            stops=stops,
            geometry=response.get('geometry'),
            coordinates=response.get('coordinates', [])
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'total_distance_km': self.total_distance_km,
            'total_duration_min': self.total_duration_min,
            'stop_sequence': [s.to_dict() for s in self.stops],
            'geometry': self.geometry,
            'coordinates': self.coordinates
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Route':
        stops = []
        for stop_data in data.get('stop_sequence', []):
            geo = GeoCoordinate(
                lng=stop_data.get('lng', 0),
                lat=stop_data.get('lat', 0)
            )
            stops.append(Stop(
                order_id=stop_data.get('order_id', ''),
                stop_type=stop_data.get('type', 'unknown'),
                geo_location=geo,
                estimated_arrival_min=stop_data.get('estimated_arrival_min', 0),
                waypoint_index=stop_data.get('waypoint_index', 0)
            ))

        return cls(
            total_distance_km=data.get('total_distance_km', 0),
            total_duration_min=data.get('total_duration_min', 0),
            stops=stops,
            geometry=data.get('geometry'),
            coordinates=data.get('coordinates', [])
        )