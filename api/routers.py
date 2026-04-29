from api.views.core import (
    UserViewSet, RestaurantViewSet, OrderViewSet, 
    FoodViewSet, NotificationViewSet, DisputeViewSet
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'restaurants', RestaurantViewSet, basename='restaurant')
router.register(r'products', FoodViewSet, basename='product')
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'disputes', DisputeViewSet, basename='dispute')
router.register(r'drivers', DriverViewSet, basename='driver')
router.register(r'delivery-proofs', DeliveryProofViewSet, basename='delivery-proof')
router.register(r'wallets', WalletViewSet, basename='wallet')
router.register(r'admin', AdminViewSet, basename='admin')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'reviews', ReviewViewSet, basename='review')

from api.views.auth import RequestOTPView, VerifyOTPView, RegisterView, MeView

urlpatterns = router.urls + [
    path('auth/request-otp/', RequestOTPView.as_view(), name='request-otp'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/me/', MeView.as_view(), name='auth-me'),
    path('payments/airpay/callback/', AirpayWebhookView.as_view(), name='airpay-callback'),
]


