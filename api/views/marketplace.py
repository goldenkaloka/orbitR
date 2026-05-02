from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.marketplace.models import MarketplaceProduct, MarketplaceCategory
from api.serializers.marketplace import MarketplaceProductSerializer, MarketplaceCategorySerializer
from domain.services.marketplace_services import MarketplaceService

class MarketplaceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MarketplaceCategory.objects.all()
    serializer_class = MarketplaceCategorySerializer

class MarketplaceProductViewSet(viewsets.ModelViewSet):
    serializer_class = MarketplaceProductSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """
        Filter by zone, category, and status.
        Only show ACTIVE products to general users.
        """
        user = self.request.user
        queryset = MarketplaceProduct.objects.select_related('seller', 'category', 'zone').prefetch_related('images').all()
        
        # Filtering
        zone_id = self.request.query_params.get('zone')
        if zone_id:
            queryset = queryset.filter(zone_id=zone_id)
            
        category_id = self.request.query_params.get('category')
        if category_id:
            queryset = queryset.filter(category_id=category_id)

        # Visibility logic
        if self.action == 'list':
            # Public listings must be active
            return queryset.filter(status='ACTIVE')
        
        if self.action == 'mine':
            return queryset.filter(seller=user)

        return queryset

    @action(detail=False, methods=['GET'])
    def mine(self, request):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def perform_create(self, serializer):
        # Use Domain Service to create product and handle images
        images = self.request.FILES.getlist('images')
        product = MarketplaceService.create_product(
            validated_data={**serializer.validated_data, 'seller': self.request.user},
            images=images
        )
        serializer.instance = product

    @action(detail=True, methods=['POST'])
    def pay(self, request, pk=None):
        """
        Initiates payment for the marketplace listing fee via Airpay.
        """
        from infrastructure.payment_gateways.airpay import AirpayGateway
        from apps.payments.models import Payment
        
        product = self.get_object()
        if product.status == 'ACTIVE':
            return Response({'message': 'Product is already active'}, status=400)
            
        fee = product.calculate_listing_fee()
        gateway = AirpayGateway()
        
        # Create a Pending Payment record
        payment, _ = Payment.objects.get_or_create(
            marketplace_product=product,
            defaults={
                'amount': fee,
                'status': 'INITIATED'
            }
        )
        
        # Initiate via Airpay
        response = gateway.collect(
            amount=fee,
            phone=request.user.phone,
            order_reference=str(product.id), # For listings, we use product id as ref
            firstname=request.user.first_name,
            lastname=request.user.last_name
        )
        
        if response.get('success'):
            return Response({
                'payment_id': payment.id,
                'payment_url': response.get('endpoint'),
                'params': response.get('params')
            })
        else:
            return Response({'error': response.get('error', 'Payment initiation failed')}, status=400)

    @action(detail=True, methods=['POST'])
    def reactivate(self, request, pk=None):
        """
        Initiates payment for reactivation of an expired product.
        """
        from infrastructure.payment_gateways.airpay import AirpayGateway
        from apps.payments.models import Payment

        product = self.get_object()
        if product.status != 'EXPIRED':
             return Response({'message': 'Only expired products can be reactivated'}, status=400)
        
        # Switch listing type to renewal for fee calculation
        product.listing_type = 'RENEWAL'
        fee = product.calculate_listing_fee()
        gateway = AirpayGateway()

        payment = Payment.objects.create(
            marketplace_product=product,
            amount=fee,
            status='INITIATED'
        )

        response = gateway.collect(
            amount=fee,
            phone=request.user.phone,
            order_reference=str(product.id),
            firstname=request.user.first_name,
            lastname=request.user.last_name
        )

        if response.get('success'):
            return Response({
                'payment_id': payment.id,
                'payment_url': response.get('endpoint'),
                'params': response.get('params')
            })
        else:
            return Response({'error': response.get('error', 'Reactivation payment failed')}, status=400)
