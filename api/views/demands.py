from api.serializers.demands import (
    BuyerDemandListSerializer,
    BuyerDemandSerializer,
    CreateOfferSerializer,
    SellerOfferSerializer,
    ShopSerializer,
)
from apps.marketplace.models import BuyerDemand, SellerOffer, Shop
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db.models import Count, Min
from domain.services.demand_services import DemandService
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response


class ShopViewSet(viewsets.ModelViewSet):
    serializer_class = ShopSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Shop.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
        user = self.request.user
        if user.role != 'vendor':
            user.role = 'vendor'
            user.save(update_fields=['role'])

    @action(detail=False, methods=['GET'])
    def nearby(self, request):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        if not lat or not lng:
            return Response({'error': 'lat and lng required'}, status=400)

        radius = request.query_params.get('radius_km', '20')
        try:
            radius = float(radius)
        except ValueError:
            radius = 20.0

        point = Point(float(lng), float(lat), srid=4326)
        queryset = Shop.objects.filter(
            location__dwithin=(point, D(km=radius)),
            is_active=True,
        ).annotate(
            distance=Distance('location', point),
        ).order_by('distance')

        serializer = ShopSerializer(queryset, many=True)
        return Response(serializer.data)


class BuyerDemandViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            return BuyerDemandListSerializer
        return BuyerDemandSerializer

    def get_queryset(self):
        user = self.request.user
        role = self.request.query_params.get('role')

        queryset = BuyerDemand.objects.select_related('buyer', 'accepted_offer').prefetch_related(
            'offers__seller', 'offers__shop'
        )

        if role == 'seller' or self.action == 'nearby':
            lat = self.request.query_params.get('lat')
            lng = self.request.query_params.get('lng')
            if lat and lng:
                point = Point(float(lng), float(lat), srid=4326)
                queryset = queryset.filter(
                    location__dwithin=(point, D(km=20)),
                    status='OPEN',
                ).annotate(
                    annotated_distance_km=Distance('location', point),
                    annotated_offer_count=Count('offers'),
                    annotated_lowest_offer=Min('offers__price'),
                ).order_by('annotated_distance_km')
            else:
                queryset = queryset.filter(status='OPEN').annotate(
                    annotated_offer_count=Count('offers'),
                    annotated_lowest_offer=Min('offers__price'),
                )
        elif self.action == 'mine':
            queryset = queryset.filter(buyer=user)
        elif self.action == 'offer':
            queryset = BuyerDemand.objects.filter(status='OPEN')
            if self.request.query_params.get('lat') and self.request.query_params.get('lng'):
                point = Point(float(lng), float(lat), srid=4326)
                queryset = queryset.annotate(
                    annotated_distance_km=Distance('location', point),
                )
        else:
            queryset = queryset.filter(buyer=user)

        return queryset

    def perform_create(self, serializer):
        data = serializer.validated_data
        lat = data['location'].y
        lng = data['location'].x
        demand = DemandService.create_demand(
            buyer_id=self.request.user.id,
            title=data['title'],
            lat=lat,
            lng=lng,
            radius_km=data.get('radius_km', 5),
            description=data.get('description'),
            category_ids=data.get('category_ids'),
        )
        serializer.instance = demand

    @action(detail=False, methods=['GET'])
    def mine(self, request):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['GET'])
    def nearby(self, request):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        if not lat or not lng:
            try:
                shop = Shop.objects.filter(owner=request.user, is_active=True).first()
                if shop and shop.location:
                    lat = str(shop.location.y)
                    lng = str(shop.location.x)
            except Exception:
                pass
        if not lat or not lng:
            return Response({'error': 'lat and lng required'}, status=400)

        radius = request.query_params.get('radius_km', '20')
        try:
            radius = float(radius)
        except ValueError:
            radius = 20.0

        point = Point(float(lng), float(lat), srid=4326)
        queryset = BuyerDemand.objects.filter(
            location__dwithin=(point, D(km=radius)),
            status='OPEN',
        ).annotate(
            annotated_distance_km=Distance('location', point),
            annotated_offer_count=Count('offers'),
            annotated_lowest_offer=Min('offers__price'),
        ).order_by('annotated_distance_km')

        serializer = BuyerDemandListSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['POST'])
    def offer(self, request, pk=None):
        demand = self.get_object()
        serializer = CreateOfferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        shop_id = serializer.validated_data.get('shop_id')
        if shop_id:
            shop = Shop.objects.filter(id=shop_id, owner=request.user).first()
            if not shop:
                return Response(
                    {'error': 'Shop not found or does not belong to you'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            offer = DemandService.create_offer(
                demand_id=demand.id,
                seller_id=request.user.id,
                price=serializer.validated_data['price'],
                shop_id=shop_id,
                availability=serializer.validated_data.get('availability', True),
                message=serializer.validated_data.get('message'),
            )
            return Response(
                SellerOfferSerializer(offer).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def accept(self, request, pk=None):
        offer_id = request.data.get('offer_id')
        if not offer_id:
            return Response({'error': 'offer_id required'}, status=400)

        try:
            result = DemandService.accept_offer(
                demand_id=pk,
                offer_id=offer_id,
                buyer_id=request.user.id,
            )
            return Response({
                'demand': BuyerDemandSerializer(result['demand']).data,
                'offer': SellerOfferSerializer(result['offer']).data,
                'buyer_info': result['buyer_info'],
                'seller_info': result['seller_info'],
            })
        except ValueError as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def cancel(self, request, pk=None):
        try:
            DemandService.cancel_demand(
                demand_id=pk,
                buyer_id=request.user.id,
            )
            return Response({'status': 'cancelled'})
        except ValueError as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=False, methods=['GET'])
    def my_offers(self, request):
        offers = SellerOffer.objects.filter(
            seller=request.user,
        ).select_related('demand', 'shop').order_by('-created_at')
        serializer = SellerOfferSerializer(offers, many=True)
        return Response(serializer.data)
