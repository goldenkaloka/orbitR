from api.serializers.marketplace import (
    MarketplaceCategorySerializer,
    MarketplaceProductListSerializer,
    MarketplaceProductSerializer,
)
from apps.core.cache_keys import stable_cache_key
from apps.marketplace.models import MarketplaceCategory, MarketplaceProduct
from django.core.cache import cache
from domain.services.marketplace_services import MarketplaceService
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response


class MarketplaceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MarketplaceCategory.objects.all()
    serializer_class = MarketplaceCategorySerializer


class MarketplaceProductViewSet(viewsets.ModelViewSet):
    serializer_class = MarketplaceProductSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            return MarketplaceProductListSerializer
        return self.serializer_class

    def get_queryset(self):
        """
        Filter by zone, category, and status.
        Supports lat/lng for proximity sorting (nearest first across all zones),
        with zone param as a fallback filter.
        Only show ACTIVE products to general users.
        """
        queryset = MarketplaceProduct.objects.select_related(
            'seller', 'category'
        ).prefetch_related('images').only(
            'seller__id', 'seller__username',
            'category__id', 'category__name',
            'name', 'description', 'price', 'condition',
            'whatsapp_number', 'location', 'h3_index',
            'status', 'expiry_date', 'is_fee_paid', 'created_at',
        )

        # Proximity sorting — nearest first when lat/lng provided
        lat = self.request.query_params.get('lat')
        lng = self.request.query_params.get('lng')
        if lat and lng:
            from django.contrib.gis.db.models.functions import Distance
            from django.contrib.gis.geos import Point
            from django.db.models import Case, FloatField, Value, When
            user_location = Point(float(lng), float(lat), srid=4326)
            queryset = queryset.annotate(
                distance=Case(
                    When(location__isnull=False, then=Distance('location', user_location)),
                    default=Value(1e10),
                    output_field=FloatField(),
                )
            ).order_by('distance')
        else:
            zone_h3 = self.request.query_params.get('zone')
            if zone_h3:
                from apps.core.geo_utils import get_neighbors
                neighbor_cells = get_neighbors(zone_h3, 1)
                queryset = queryset.filter(h3_index__in=neighbor_cells)

        category_id = self.request.query_params.get('category')
        if category_id:
            queryset = queryset.filter(category_id=category_id)

        if self.action == 'list':
            return queryset.filter(status='ACTIVE')

        if self.action == 'mine':
            return queryset.filter(seller=self.request.user)

        return queryset

    @action(detail=False, methods=['GET'])
    def mine(self, request):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def invalidate_list_cache(self):
        try:
            cache.incr('marketplace_list_cache_version')
        except ValueError:
            cache.set('marketplace_list_cache_version', 1)

    def list(self, request, *args, **kwargs):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        category = request.query_params.get('category', 'all')
        zone = request.query_params.get('zone', 'none')
        limit = request.query_params.get('limit')
        offset = request.query_params.get('offset')

        try:
            version = cache.get('marketplace_list_cache_version') or 0
        except Exception:
            version = 0

        cache_key = stable_cache_key(
            f"marketplace_list:v{version}",
            {
                "lat": lat or "none",
                "lng": lng or "none",
                "category": category,
                "zone": zone,
                "limit": limit or "default",
                "offset": offset or "0",
            },
        )
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        response = super().list(request, *args, **kwargs)
        cache.set(cache_key, response.data, 900)
        return response

    MAX_IMAGE_SIZE_MB = 5
    MAX_IMAGES_COUNT = 10
    ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}

    def perform_create(self, serializer):
        images = self.request.FILES.getlist('images')
        if len(images) > self.MAX_IMAGES_COUNT:
            raise ValidationError(
                {'images': f'Maximum {self.MAX_IMAGES_COUNT} images allowed per product.'}
            )
        for img in images:
            if img.size > self.MAX_IMAGE_SIZE_MB * 1024 * 1024:
                raise ValidationError(
                    {'images': f'Each image must be under {self.MAX_IMAGE_SIZE_MB}MB.'}
                )
            if img.content_type not in self.ALLOWED_IMAGE_TYPES:
                raise ValidationError(
                    {'images': f'Only JPEG, PNG, and WebP images are allowed.'}
                )
        product = MarketplaceService.create_product(
            validated_data={**serializer.validated_data, 'seller': self.request.user},
            images=images
        )
        serializer.instance = product
        self.invalidate_list_cache()

    def perform_update(self, serializer):
        serializer.save()
        self.invalidate_list_cache()

    def perform_destroy(self, instance):
        instance.delete()
        self.invalidate_list_cache()

    def create(self, request, *args, **kwargs):
        """
        Override create to auto-initiate payment when payment fields are provided.
        This makes listing creation seamless — create + pay in one step.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        product = serializer.instance

        # Auto-initiate payment if payment fields are present
        payment_phone = request.data.get('payment_phone')
        if payment_phone:
            from domain.services.payment_services import InitiatePaymentService
            try:
                result = InitiatePaymentService.execute(
                    payable_type='marketplace_product',
                    payable_id=product.id,
                    phone=payment_phone,
                    bank_code=request.data.get('bank_code'),
                )
                payment = result['payment']
                gateway_response = result['gateway_response']

                # Re-fetch the product after payment to get updated status
                if gateway_response.get('instant_success'):
                    product.refresh_from_db()

                # Re-serialize with fresh data
                response_data = self.get_serializer(product).data
                response_data['payment'] = {
                    'payment_id': str(payment.id),
                    'status': 'ACTIVE' if gateway_response.get('instant_success') else 'PENDING',
                    'instant_success': gateway_response.get('instant_success', False),
                    'is_push': gateway_response.get('is_push', False),
                    'message': 'Payment successful! Product is now ACTIVE.' if gateway_response.get('instant_success')
                               else 'USSD Push initiated. Check your phone to confirm.',
                }
                if gateway_response.get('params'):
                    response_data['payment']['params'] = gateway_response['params']
                if gateway_response.get('endpoint'):
                    response_data['payment']['endpoint'] = gateway_response['endpoint']

                return Response(response_data, status=status.HTTP_201_CREATED)

            except Exception as e:
                response_data = serializer.data
                response_data['payment'] = {
                    'status': 'FAILED',
                    'error': str(e),
                }
                return Response(response_data, status=status.HTTP_201_CREATED)

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['POST'])
    def pay(self, request, pk=None):
        """
        Initiates payment for the marketplace listing fee.
        """
        product = self.get_object()
        if product.status == 'ACTIVE':
            return Response({'message': 'Product is already active'}, status=400)
            
        phone = request.data.get('phone', request.user.phone)
        from domain.services.payment_services import InitiatePaymentService
        try:
            result = InitiatePaymentService.execute(
                payable_type='marketplace_product',
                payable_id=product.id,
                phone=phone,
                bank_code=request.data.get('bank_code')
            )
        except Exception as e:
            return Response({'error': str(e)}, status=400)
        
        payment = result['payment']
        response = result['gateway_response']

        if response.get('success'):
            if response.get('instant_success'):
                self.invalidate_list_cache()
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
        product = self.get_object()
        if product.status != 'EXPIRED':
             return Response({'message': 'Only expired products can be reactivated'}, status=400)
        
        # Ensure product is marked as renewal for correct fee calculation
        product.listing_type = 'RENEWAL'
        product.save(update_fields=['listing_type'])
        
        phone = request.data.get('phone', request.user.phone)
        from domain.services.payment_services import InitiatePaymentService
        try:
            result = InitiatePaymentService.execute(
                payable_type='marketplace_product',
                payable_id=product.id,
                phone=phone,
                bank_code=request.data.get('bank_code')
            )
        except Exception as e:
            return Response({'error': str(e)}, status=400)

        payment = result['payment']
        response = result['gateway_response']

        if response.get('success'):
            if response.get('instant_success'):
                self.invalidate_list_cache()
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
