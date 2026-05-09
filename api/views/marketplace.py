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
        queryset = MarketplaceProduct.objects.select_related('seller', 'category').prefetch_related('images').all()
        
        # Filtering
        zone_h3 = self.request.query_params.get('zone')
        if zone_h3:
            from apps.core.geo_utils import get_neighbors
            # Include neighbors to show nearby products across hexagon boundaries
            neighbor_cells = get_neighbors(zone_h3, 1)
            queryset = queryset.filter(h3_index__in=neighbor_cells)
            
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
        Initiates payment for the marketplace listing fee via EasyPay.
        """
        from infrastructure.payment_gateways.factory import get_payment_gateway
        from apps.payments.models import Payment
        
        product = self.get_object()
        if product.status == 'ACTIVE':
            return Response({'message': 'Product is already active'}, status=400)
            
        phone = request.data.get('phone', request.user.phone)
        gateway = get_payment_gateway(phone=phone)
        
        # Create a Pending Payment record
        payment, _ = Payment.objects.get_or_create(
            marketplace_product=product,
            defaults={
                'amount': fee,
                'status': 'INITIATED'
            }
        )
        
        # Initiate via Gateway
        response = gateway.collect(
            amount=fee,
            phone=phone,
            bank_code=request.data.get('bank_code'),
            order_reference=str(product.id),
            firstname=request.user.first_name,
            lastname=request.user.last_name
        )
        
        if response.get('success'):
            payment.provider_reference = response.get('provider_reference')
            payment.save()

            if response.get('instant_success'):
                from domain.services.payment_services import CompletePaymentService
                CompletePaymentService.execute(
                    order_id=str(product.id),
                    provider_reference=payment.provider_reference,
                    success=True
                )
                return Response({
                    'payment_id': payment.id,
                    'message': 'Mock Payment Successful! Product is now ACTIVE.',
                    'status': 'ACTIVE',
                    'instant_success': True
                })

            return Response({
                'payment_id': payment.id,
                'message': 'USSD Push initiated. Please check your phone to confirm payment.',
                'status': 'PENDING'
            })
        else:
            return Response({'error': response.get('error', 'Payment initiation failed')}, status=400)

    @action(detail=True, methods=['POST'])
    def reactivate(self, request, pk=None):
        """
        Initiates payment for reactivation of an expired product.
        """
        from infrastructure.payment_gateways.factory import get_payment_gateway
        from apps.payments.models import Payment

        product = self.get_object()
        if product.status != 'EXPIRED':
             return Response({'message': 'Only expired products can be reactivated'}, status=400)
        
        # Switch listing type to renewal for fee calculation
        product.listing_type = 'RENEWAL'
        fee = product.calculate_listing_fee()
        phone = request.data.get('phone', request.user.phone)
        gateway = get_payment_gateway(phone=phone)

        payment = Payment.objects.create(
            marketplace_product=product,
            amount=fee,
            status='INITIATED'
        )

        response = gateway.collect(
            amount=fee,
            phone=phone,
            bank_code=request.data.get('bank_code'),
            order_reference=str(product.id),
            firstname=request.user.first_name,
            lastname=request.user.last_name
        )

        if response.get('success'):
            payment.provider_reference = response.get('provider_reference')
            payment.save()

            if response.get('instant_success'):
                from domain.services.payment_services import CompletePaymentService
                CompletePaymentService.execute(
                    order_id=str(product.id),
                    provider_reference=payment.provider_reference,
                    success=True
                )
                return Response({
                    'payment_id': payment.id,
                    'message': 'Mock Reactivation Successful! Product is now ACTIVE.',
                    'status': 'ACTIVE',
                    'instant_success': True
                })

            return Response({
                'payment_id': payment.id,
                'message': 'USSD Push initiated for reactivation. Please check your phone.',
                'status': 'PENDING'
            })
        else:
            return Response({'error': response.get('error', 'Reactivation payment failed')}, status=400)
