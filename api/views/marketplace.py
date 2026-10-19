import logging

from api.serializers.marketplace import (
    MarketplaceCategorySerializer,
    MarketplaceProductListSerializer,
    MarketplaceProductSerializer,
    PostingTokenBundleOptionSerializer,
    PostingTokenBundlePurchaseSerializer,
)
from apps.core.cache_keys import stable_cache_key
from apps.marketplace.models import (
    MarketplaceCategory,
    MarketplaceProduct,
    ProductImage,
    PostingTokenBundleOption,
    PostingTokenBundlePurchase,
)
from django.core.cache import cache
from django.db import transaction
from django.db.models import Prefetch, Q
from domain.services.marketplace_services import MarketplaceService
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle


logger = logging.getLogger(__name__)


class MarketplaceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MarketplaceCategory.objects.all()
    serializer_class = MarketplaceCategorySerializer

    #: Categories are fetched by every storefront page and revalidated every
    #: 60s from the single `web` container IP — the default `anon` bucket
    #: (100/hour, keyed by that IP) would 429 the storefront into rendering
    #: an empty catalogue. Share the catalogue budget with product reads.
    def get_throttles(self):
        self.throttle_scope = 'catalog'
        return [ScopedRateThrottle()]


class MarketplaceProductViewSet(viewsets.ModelViewSet):
    serializer_class = MarketplaceProductSerializer

    #: Read actions are public so the marketplace storefront can browse without
    #: a JWT. Everything that spends tokens, moves money or touches a seller's
    #: own listings stays authenticated.
    PUBLIC_ACTIONS = {'list', 'retrieve'}

    def get_permissions(self):
        if self.action in self.PUBLIC_ACTIONS:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    #: Catalogue reads are high-volume and anonymous (every visitor to the
    #: storefront is `anon`), so they get their own budget instead of being
    #: pinned to the strict anon rate used by auth and write endpoints.
    #: Money-moving actions get a tighter budget than the blanket `user` rate.
    throttle_scope_map = {
        'list': 'catalog',
        'retrieve': 'catalog',
        'pay': 'payments',
        'reactivate': 'payments',
    }

    def get_throttles(self):
        scope = self.throttle_scope_map.get(self.action)
        if scope:
            # ScopedRateThrottle reads view.throttle_scope; it takes no kwargs.
            self.throttle_scope = scope
            return [ScopedRateThrottle()]
        return super().get_throttles()

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
        ).prefetch_related(
            Prefetch(
                'images',
                queryset=ProductImage.objects.only('id', 'product_id', 'image', 'is_primary'),
                to_attr='prefetched_images',
            )
        ).only(
            'seller__id', 'seller__username', 'seller__avatar', 'seller__avatar_url',
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
            from apps.core.geo_utils import latlng_to_h3, get_neighbors
            
            user_location = Point(float(lng), float(lat), srid=4326)
            user_h3 = latlng_to_h3(float(lat), float(lng))
            
            if user_h3:
                # Filter to immediate neighbors (k=2 rings) to limit calculations
                neighbor_cells = get_neighbors(user_h3, 2)
                # If there are products in neighbor cells, restrict. Otherwise keep full to guarantee fallback.
                if queryset.filter(h3_index__in=neighbor_cells).exists():
                    queryset = queryset.filter(h3_index__in=neighbor_cells)
            
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
                neighbor_cells = get_neighbors(zone_h3, 2)
                queryset = queryset.filter(h3_index__in=neighbor_cells)

        category_id = self.request.query_params.get('category')
        if category_id:
            queryset = queryset.filter(category_id=category_id)

        search = (self.request.query_params.get('q') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(description__icontains=search)
            )

        # Ordering must be deterministic — limit/offset pagination repeats or
        # skips rows when the queryset has no stable order.
        sort = (self.request.query_params.get('sort') or '').strip().lower()
        has_coords = bool(lat and lng)

        if sort == 'price_asc':
            queryset = queryset.order_by('price')
        elif sort == 'price_desc':
            queryset = queryset.order_by('-price')
        elif has_coords:
            # `distance` is annotated above; 'nearest' and the default both
            # fall back to proximity when coordinates are supplied.
            queryset = queryset.order_by('distance')
        else:
            queryset = queryset.order_by('-created_at')

        if self.action == 'list':
            return queryset.filter(status='ACTIVE')

        if self.action == 'mine':
            return queryset.filter(seller=self.request.user)

        if self.action in ('update', 'partial_update', 'destroy', 'pay', 'reactivate'):
            return queryset.filter(seller=self.request.user)

        if self.action == 'retrieve':
            # Q(seller=request.user) with an AnonymousUser raises
            # "Field 'id' expected a number but got AnonymousUser" — a 500 on
            # every public product page. Branch before building the query.
            if self.request.user.is_authenticated:
                return queryset.filter(
                    Q(status='ACTIVE') | Q(seller=self.request.user)
                )
            return queryset.filter(status='ACTIVE')

        return queryset

    @action(detail=False, methods=['GET'])
    def mine(self, request):
        queryset = self.get_queryset()
        # Paginate — a power seller with thousands of listings would otherwise
        # get one multi-megabyte response with no cursor.
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def invalidate_list_cache(self):
        # Delegate so the cross-service storefront purge fires on every path,
        # not just the ones that go through the domain service.
        from domain.services.marketplace_services import invalidate_product_list_cache
        invalidate_product_list_cache()

    def list(self, request, *args, **kwargs):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        category = request.query_params.get('category', 'all')
        zone = request.query_params.get('zone', 'none')
        limit = request.query_params.get('limit')
        offset = request.query_params.get('offset')
        # Both of these change the result set, so both belong in the cache key.
        # Omitting them serves one search or sort order's rows for another's.
        search = (request.query_params.get('q') or '').strip().lower()
        sort = (request.query_params.get('sort') or '').strip().lower()

        try:
            version = cache.get('marketplace_list_cache_version') or 0
        except Exception:
            version = 0

        # Resolve spatial index cell for cache hashing to maximize cache hit rate
        user_h3 = "none"
        if lat and lng:
            from apps.core.geo_utils import latlng_to_h3
            user_h3 = latlng_to_h3(float(lat), float(lng)) or "none"

        cache_key = stable_cache_key(
            f"marketplace_list:v{version}",
            {
                "h3": user_h3,
                "category": category,
                "zone": zone,
                "limit": limit or "default",
                "offset": offset or "0",
                "q": search,
                "sort": sort,
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
        try:
            product = MarketplaceService.create_product(
                validated_data={**serializer.validated_data, 'seller': self.request.user},
                images=images
            )
        except ValueError as e:
            raise ValidationError({'detail': str(e)})
        serializer.instance = product
        self.invalidate_list_cache()

    def perform_update(self, serializer):
        if serializer.instance.seller_id != self.request.user.id and not self.request.user.is_staff:
            raise ValidationError({'detail': 'Only the seller can update this product.'})
        serializer.save()
        self.invalidate_list_cache()

    def perform_destroy(self, instance):
        if instance.seller_id != self.request.user.id and not self.request.user.is_staff:
            raise ValidationError({'detail': 'Only the seller can delete this product.'})
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

        # Auto-initiate payment if payment fields are present.
        # Skip it when the product is already ACTIVE (a posting token was
        # deducted in perform_create) — otherwise the seller would be charged
        # the listing fee on top of the token they just spent.
        payment_phone = request.data.get('payment_phone')
        if payment_phone and product.status != 'ACTIVE':
            from domain.services.payment_services import InitiatePaymentService
            try:
                result = InitiatePaymentService.execute(
                    payable_type='marketplace_product',
                    payable_id=product.id,
                    phone=payment_phone,
                    bank_code=request.data.get('bank_code'),
                    actor=request.user,
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

            except Exception:
                response_data = serializer.data
                response_data['payment'] = {
                    'status': 'FAILED',
                    'error': 'Payment initiation failed.',
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
            
        use_posting_token = request.data.get('use_posting_token', False)
        if use_posting_token:
            from apps.marketplace.models import PostingTokenConfig, PostingTokenTransaction
            with transaction.atomic():
                user_locked = request.user.__class__.objects.select_for_update().get(id=request.user.id)
                config = PostingTokenConfig.objects.first()
                deduction = config.token_deduction_per_listing if config else 1
                
                if user_locked.posting_tokens_balance < deduction:
                    return Response({'error': 'You do not have enough posting tokens left. Please buy more.'}, status=400)
                    
                user_locked.posting_tokens_balance -= deduction
                user_locked.save(update_fields=['posting_tokens_balance'])
                
                # Record transaction ledger
                PostingTokenTransaction.objects.create(
                    user=user_locked,
                    transaction_type='LISTING_DEDUCTION',
                    amount=-deduction,
                    balance_after=user_locked.posting_tokens_balance,
                    description=f"Deducted {deduction} token(s) for paying listing: {product.name}"
                )
                
                MarketplaceService.activate_product(product_id=str(product.id), actor=user_locked)
                
            self.invalidate_list_cache()
            return Response({
                'message': 'Product paid and activated successfully using a posting token!',
                'status': 'ACTIVE',
                'instant_success': True
            })

        phone = request.data.get('phone', request.user.phone)
        from domain.services.payment_services import InitiatePaymentService
        try:
            result = InitiatePaymentService.execute(
                payable_type='marketplace_product',
                payable_id=product.id,
                phone=phone,
                bank_code=request.data.get('bank_code'),
                actor=request.user,
            )
        except Exception:
            return Response({'error': 'Payment initiation failed.'}, status=400)
        
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
            return Response({'error': 'Payment initiation failed'}, status=400)

    @action(detail=True, methods=['POST'])
    def reactivate(self, request, pk=None):
        """
        Initiates payment for reactivation of an expired product.
        """
        product = self.get_object()
        if product.status != 'EXPIRED':
             return Response({'message': 'Only expired products can be reactivated'}, status=400)
        use_posting_token = request.data.get('use_posting_token', False)
        if not use_posting_token:
            return Response({'error': 'Reactivation requires a posting token. Please purchase a token bundle.'}, status=400)
            
        from apps.marketplace.models import PostingTokenConfig, PostingTokenTransaction
        with transaction.atomic():
            user_locked = request.user.__class__.objects.select_for_update().get(id=request.user.id)
            config = PostingTokenConfig.objects.first()
            deduction = config.token_deduction_per_listing if config else 1
            
            if user_locked.posting_tokens_balance < deduction:
                return Response({'error': 'You do not have enough posting tokens left. Please buy more.'}, status=400)
                
            user_locked.posting_tokens_balance -= deduction
            user_locked.save(update_fields=['posting_tokens_balance'])
            
            # Ensure product is marked as renewal
            product.listing_type = 'RENEWAL'
            product.save(update_fields=['listing_type'])
            
            # Record transaction ledger
            PostingTokenTransaction.objects.create(
                user=user_locked,
                transaction_type='LISTING_DEDUCTION',
                amount=-deduction,
                balance_after=user_locked.posting_tokens_balance,
                description=f"Deducted {deduction} token(s) for reactivating listing: {product.name}"
            )
            
            MarketplaceService.activate_product(product_id=str(product.id), actor=user_locked)
            
        self.invalidate_list_cache()
        return Response({
            'message': 'Product reactivated successfully using a posting token!',
            'status': 'ACTIVE',
            'instant_success': True
        })


class PostingTokenBundleViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        """List active posting token bundle options from the database."""
        options = PostingTokenBundleOption.objects.filter(is_active=True).order_by('price')
        serializer = PostingTokenBundleOptionSerializer(options, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['POST'])
    def purchase(self, request):
        """Initiate payment to purchase a posting token bundle."""
        bundle_id = request.data.get('bundle_id')
        phone = request.data.get('phone', request.user.phone)
        bank_code = request.data.get('bank_code')

        try:
            bundle = PostingTokenBundleOption.objects.get(id=bundle_id, is_active=True)
        except PostingTokenBundleOption.DoesNotExist:
            return Response({'error': 'Invalid or inactive bundle_id'}, status=status.HTTP_400_BAD_REQUEST)

        from domain.services.payment_services import InitiatePaymentService

        # Create the purchase log first, then initiate payment OUTSIDE any
        # transaction — InitiatePaymentService talks to the gateway and a
        # stall there must not hold DB locks (purchase stays is_paid=False
        # until the completion handler marks it).
        purchase = PostingTokenBundlePurchase.objects.create(
            user=request.user,
            bundle=bundle,
            amount=bundle.price,
            tokens_granted=bundle.tokens_granted
        )

        # Initiate payment via ClickPesa
        try:
            result = InitiatePaymentService.execute(
                payable_type='posting_token_bundle',
                payable_id=purchase.id,
                phone=phone,
                bank_code=bank_code,
                actor=request.user
            )
        except Exception:
            logger.exception("Posting token bundle payment initiation failed")
            return Response({
                'success': False,
                'error': 'Payment initiation failed. Please try again.',
                'purchase_id': str(purchase.id),
            }, status=status.HTTP_400_BAD_REQUEST)

        payment = result['payment']
        gateway_data = result.get('gateway_response', {})

        push_sent = gateway_data.get('success', False)

        base_response = {
            "success": push_sent,
            "purchase_id": str(purchase.id),
            "payment_id": str(payment.id),
            "status": payment.status,
            "is_push": result.get('is_push', False),
        }

        if push_sent:
            base_response["provider_reference"] = gateway_data.get('provider_reference')
            base_response["message"] = gateway_data.get('message')
            base_response["instant_success"] = gateway_data.get('instant_success')
            if gateway_data.get('params'):
                base_response["params"] = gateway_data['params']
            if gateway_data.get('endpoint'):
                base_response["endpoint"] = gateway_data['endpoint']
        else:
            base_response["error"] = gateway_data.get('error') or 'Payment initiation failed'

        return Response(base_response)
