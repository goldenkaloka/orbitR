from api.views.admin import AdminViewSet
from api.views.auth import (
    MeView,
    RegisterView,
    RequestOTPView,
    VerifyOTPView,
    WsTicketView,
)
from api.views.batches import BatchViewSet
from api.views.core import (
    AppConfigView,
    DisputeViewSet,
    FoodViewSet,
    NotificationViewSet,
    OrderViewSet,
    RestaurantViewSet,
    UniversalSearchView,
    UserViewSet,
)
from api.views.demands import BuyerDemandViewSet, ShopViewSet
from api.views.driver import DriverViewSet
from api.views.geo import OperationalZoneViewSet
from api.views.marketplace import MarketplaceCategoryViewSet, MarketplaceProductViewSet, PostingTokenBundleViewSet
from api.views.payments import AirpayWebhookView, ClickpesaWebhookView, InitiatePaymentView, PlatformRevenueView
from api.views.pools import PoolOrderViewSet
from api.views.pricing import (
    DeliveryFeeLogViewSet,
    PricingPolicyViewSet,
    ZonePricingTierViewSet,
)
from api.views.reviews import ReviewViewSet
from api.views.sections import BatchSectionStatusViewSet, SectionViewSet
from api.views.subscribe_check import ws_subscribe_check
from api.views.reliability import ReliabilityView
from api.views.wallets import WalletViewSet
from django.urls import path
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'restaurants', RestaurantViewSet, basename='restaurant')
router.register(r'products', FoodViewSet, basename='product')
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'pools', PoolOrderViewSet, basename='pool')
router.register(r'disputes', DisputeViewSet, basename='dispute')
router.register(r'drivers', DriverViewSet, basename='driver')
router.register(r'wallets', WalletViewSet, basename='wallet')
router.register(r'admin', AdminViewSet, basename='admin')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'reviews', ReviewViewSet, basename='review')
router.register(r'marketplace/products', MarketplaceProductViewSet, basename='marketplace-product')
router.register(r'marketplace/posting-tokens', PostingTokenBundleViewSet, basename='posting-token')
router.register(r'marketplace/categories', MarketplaceCategoryViewSet, basename='marketplace-category')
router.register(r'marketplace/shops', ShopViewSet, basename='shop')
router.register(r'marketplace/demands', BuyerDemandViewSet, basename='demand')
router.register(r'geo/zones', OperationalZoneViewSet, basename='geo-zone')
router.register(r'pricing/policies', PricingPolicyViewSet, basename='pricing-policy')
router.register(r'pricing/tiers', ZonePricingTierViewSet, basename='pricing-tier')
router.register(r'pricing/logs', DeliveryFeeLogViewSet, basename='pricing-log')
router.register(r'batches', BatchViewSet, basename='batch')
router.register(r'sections', SectionViewSet, basename='section')
router.register(r'batch-section-statuses', BatchSectionStatusViewSet, basename='batch-section-status')

urlpatterns = router.urls + [
    path('auth/request-otp/', RequestOTPView.as_view(), name='request-otp'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/me/', MeView.as_view(), name='auth-me'),
    path('auth/ws-ticket/', WsTicketView.as_view(), name='ws-ticket'),
    path('payments/airpay/callback/', AirpayWebhookView.as_view(), name='airpay-callback'),
    path('payments/airpay/callback', AirpayWebhookView.as_view(), name='airpay-callback-no-slash'),
    path('payments/clickpesa/callback/', ClickpesaWebhookView.as_view(), name='clickpesa-callback'),
    path('payments/clickpesa/callback', ClickpesaWebhookView.as_view(), name='clickpesa-callback-no-slash'),
    path('payments/initiate/', InitiatePaymentView.as_view(), name='payment-initiate'),
    path('payments/revenue/', PlatformRevenueView.as_view(), name='platform-revenue'),
    path('search/', UniversalSearchView.as_view(), name='universal-search'),
    path('ws-subscribe-check/', ws_subscribe_check, name='ws-subscribe-check'),
    path('reliability/', ReliabilityView.as_view(), name='reliability-me'),
    path('app-config/', AppConfigView.as_view(), name='app-config'),
]
