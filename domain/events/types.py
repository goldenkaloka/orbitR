from dataclasses import dataclass, field
from typing import Dict, Any
from uuid import UUID

@dataclass
class DomainEvent:
    aggregate_id: UUID
    aggregate_type: str = field(init=False)
    event_type: str = field(init=False)
    payload: Dict[str, Any]

@dataclass
class OrderCreated(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderCreated'

@dataclass
class OrderConfirmed(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderConfirmed'

@dataclass
class FoodReady(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'FoodReady'

@dataclass
class DriverAssigned(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'DriverAssigned'

@dataclass
class OrderPicked(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderPicked'

@dataclass
class OrderDelivered(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderDelivered'

@dataclass
class PaymentCollected(DomainEvent):
    aggregate_type = 'Payment'
    event_type = 'PaymentCollected'

@dataclass
class PaymentReleaseRequested(DomainEvent):
    aggregate_type = 'Payment'
    event_type = 'PaymentReleaseRequested'

@dataclass
class PaymentInitiated(DomainEvent):
    aggregate_type = 'Payment'
    event_type = 'PaymentInitiated'

@dataclass
class PaymentDisbursed(DomainEvent):
    aggregate_type = 'Payment'
    event_type = 'PaymentDisbursed'

@dataclass
class PaymentRefunded(DomainEvent):
    aggregate_type = 'Payment'
    event_type = 'PaymentRefunded'

@dataclass
class AuthOTPRequested(DomainEvent):
    aggregate_type = 'Auth'
    event_type = 'AuthOTPRequested'

@dataclass
class AuthUserVerified(DomainEvent):
    aggregate_type = 'Auth'
    event_type = 'AuthUserVerified'

@dataclass
class AuthUserRegistered(DomainEvent):
    aggregate_type = 'Auth'
    event_type = 'AuthUserRegistered'

@dataclass
class UserPromotedToVendor(DomainEvent):
    aggregate_type = 'User'
    event_type = 'UserPromotedToVendor'

@dataclass
class UserPromotedToDriver(DomainEvent):
    aggregate_type = 'User'
    event_type = 'UserPromotedToDriver'

@dataclass
class TempDriverRegistered(DomainEvent):
    aggregate_type = 'Driver'
    event_type = 'TempDriverRegistered'

@dataclass
class OrderPreparing(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderPreparing'

@dataclass
class OrderCancelled(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderCancelled'

@dataclass
class OrderDisputed(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderDisputed'

@dataclass
class OrderResolved(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'OrderResolved'

@dataclass
class FoodCreated(DomainEvent):
    aggregate_type = 'Product'
    event_type = 'FoodCreated'

@dataclass
class FoodDeleted(DomainEvent):
    aggregate_type = 'Product'
    event_type = 'FoodDeleted'

@dataclass
class DispatchFailed(DomainEvent):
    aggregate_type = 'Order'
    event_type = 'DispatchFailed'

@dataclass
class MarketplaceProductCreated(DomainEvent):
    aggregate_type = 'MarketplaceProduct'
    event_type = 'MarketplaceProductCreated'

@dataclass
class MarketplaceProductActivated(DomainEvent):
    aggregate_type = 'MarketplaceProduct'
    event_type = 'MarketplaceProductActivated'

@dataclass
class MarketplaceProductExpired(DomainEvent):
    aggregate_type = 'MarketplaceProduct'
    event_type = 'MarketplaceProductExpired'


@dataclass
class BuyerDemandCreated(DomainEvent):
    aggregate_type = 'BuyerDemand'
    event_type = 'BuyerDemandCreated'


@dataclass
class BuyerDemandExpired(DomainEvent):
    aggregate_type = 'BuyerDemand'
    event_type = 'BuyerDemandExpired'


@dataclass
class BuyerDemandAccepted(DomainEvent):
    aggregate_type = 'BuyerDemand'
    event_type = 'BuyerDemandAccepted'


@dataclass
class SellerOfferCreated(DomainEvent):
    aggregate_type = 'SellerOffer'
    event_type = 'SellerOfferCreated'


@dataclass
class BuyerDemandCancelled(DomainEvent):
    aggregate_type = 'BuyerDemand'
    event_type = 'BuyerDemandCancelled'


@dataclass
class PaymentFailed(DomainEvent):
    aggregate_type = 'Payment'
    event_type = 'PaymentFailed'

@dataclass
class RouteCalculated(DomainEvent):
    aggregate_type = 'DeliveryBatch'
    event_type = 'RouteCalculated'

    def __post_init__(self):
        self.payload.setdefault('distance_km', 0)
        self.payload.setdefault('duration_min', 0)
        self.payload.setdefault('stop_sequence', [])

@dataclass
class RouteOptimizationFailed(DomainEvent):
    aggregate_type = 'DeliveryBatch'
    event_type = 'RouteOptimizationFailed'

@dataclass
class PoolLocked(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolLocked'

@dataclass
class PoolPaid(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolPaid'

    def __post_init__(self):
        self.payload.setdefault('all_paid', False)

@dataclass
class PoolReady(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolReady'

@dataclass
class PoolBatchStarted(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolBatchStarted'

@dataclass
class PoolCompleted(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolCompleted'

@dataclass
class PoolCancelled(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolCancelled'

@dataclass
class PoolExpired(DomainEvent):
    aggregate_type = 'OrderPool'
    event_type = 'PoolExpired'
