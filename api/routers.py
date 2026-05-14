from django.urls import path
from rest_framework.routers import DefaultRouter
from api.views.core import (
    UserViewSet, RestaurantViewSet, OrderViewSet, 
    FoodViewSet, NotificationViewSet, DisputeViewSet,
    UniversalSearchView
)
from api.views.driver import DriverViewSet, DeliveryProofViewSet
from api.views.wallets import WalletViewSet
from api.views.admin import AdminViewSet
from api.views.reviews import ReviewViewSet
from api.views.pools import PoolOrderViewSet
from api.views.payments import AirpayWebhookView, InitiatePaymentView
from api.views.auth import RequestOTPView, VerifyOTPView, RegisterView, MeView
from api.views.marketplace import MarketplaceProductViewSet, MarketplaceCategoryViewSet
from api.views.geo import OperationalZoneViewSet
from api.views.pricing import PricingPolicyViewSet, ZonePricingTierViewSet, DeliveryFeeLogViewSet

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'restaurants', RestaurantViewSet, basename='restaurant')
router.register(r'products', FoodViewSet, basename='product')
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'pools', PoolOrderViewSet, basename='pool')
router.register(r'disputes', DisputeViewSet, basename='dispute')
router.register(r'drivers', DriverViewSet, basename='driver')
router.register(r'delivery-proofs', DeliveryProofViewSet, basename='delivery-proof')
router.register(r'wallets', WalletViewSet, basename='wallet')
router.register(r'admin', AdminViewSet, basename='admin')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'reviews', ReviewViewSet, basename='review')
router.register(r'marketplace/products', MarketplaceProductViewSet, basename='marketplace-product')
router.register(r'marketplace/categories', MarketplaceCategoryViewSet, basename='marketplace-category')
router.register(r'geo/zones', OperationalZoneViewSet, basename='geo-zone')
router.register(r'pricing/policies', PricingPolicyViewSet, basename='pricing-policy')
router.register(r'pricing/tiers', ZonePricingTierViewSet, basename='pricing-tier')
router.register(r'pricing/logs', DeliveryFeeLogViewSet, basename='pricing-log')

urlpatterns = router.urls + [
    path('auth/request-otp/', RequestOTPView.as_view(), name='request-otp'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/me/', MeView.as_view(), name='auth-me'),
    path('payments/airpay/callback/', AirpayWebhookView.as_view(), name='airpay-callback'),
    path('payments/initiate/', InitiatePaymentView.as_view(), name='payment-initiate'),
    path('search/', UniversalSearchView.as_view(), name='universal-search'),
]
