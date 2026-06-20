from api.serializers.utils import parse_coordinate_dict
from apps.marketplace.models import BuyerDemand, MarketplaceCategory, SellerOffer, Shop
from rest_framework import serializers


class MarketplaceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceCategory
        fields = ['id', 'name', 'icon']


class MarketplaceCategoryPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return MarketplaceCategory.objects.all()


class ShopSerializer(serializers.ModelSerializer):
    owner_name = serializers.ReadOnlyField(source='owner.username')
    categories = MarketplaceCategoryPrimaryKeyRelatedField(
        many=True, queryset=MarketplaceCategory.objects.none(), required=False,
    )
    categories_detail = MarketplaceCategorySerializer(
        source='categories', many=True, read_only=True,
    )

    class Meta:
        model = Shop
        fields = [
            'id', 'owner', 'owner_name', 'name', 'description',
            'categories', 'categories_detail',
            'location', 'h3_index', 'whatsapp_number', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['owner', 'h3_index']

    def to_internal_value(self, data):
        point = parse_coordinate_dict(data, 'location')
        if point:
            data['location'] = point
        return super().to_internal_value(data)


class SellerOfferSerializer(serializers.ModelSerializer):
    seller_name = serializers.ReadOnlyField(source='seller.username')
    shop_name = serializers.SerializerMethodField()
    shop_location = serializers.SerializerMethodField()

    def get_shop_name(self, obj):
        return obj.shop.name if obj.shop else None

    class Meta:
        model = SellerOffer
        fields = [
            'id', 'demand', 'seller', 'seller_name', 'shop', 'shop_name',
            'shop_location', 'price', 'availability', 'message', 'status',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['seller', 'status']

    def get_shop_location(self, obj):
        if obj.shop and obj.shop.location:
            return {
                'lat': obj.shop.location.y,
                'lng': obj.shop.location.x,
            }
        return None

    def validate(self, attrs):
        from apps.marketplace.models import BuyerDemand
        try:
            demand = BuyerDemand.objects.get(id=attrs['demand'].id)
        except BuyerDemand.DoesNotExist:
            raise serializers.ValidationError({'demand': 'Demand not found'})
        if demand.status != 'OPEN':
            raise serializers.ValidationError({'demand': f'Demand is {demand.status}'})
        if demand.is_expired():
            raise serializers.ValidationError({'demand': 'Demand has expired'})
        return attrs


class BuyerDemandListSerializer(serializers.ModelSerializer):
    buyer_name = serializers.ReadOnlyField(source='buyer.username')
    offer_count = serializers.SerializerMethodField()
    lowest_offer = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = BuyerDemand
        fields = [
            'id', 'buyer', 'buyer_name', 'title', 'description',
            'location', 'h3_index', 'radius_km', 'status',
            'expires_at', 'offer_count', 'lowest_offer',
            'distance_km', 'created_at',
        ]

    def get_offer_count(self, obj):
        return getattr(obj, 'annotated_offer_count', 0)

    def get_lowest_offer(self, obj):
        val = getattr(obj, 'annotated_lowest_offer', None)
        return float(val) if val else None

    def get_distance_km(self, obj):
        d = getattr(obj, 'annotated_distance_km', None)
        return round(d, 1) if d else None


class BuyerDemandSerializer(serializers.ModelSerializer):
    buyer_name = serializers.ReadOnlyField(source='buyer.username')
    offers = SellerOfferSerializer(many=True, read_only=True)
    accepted_offer = SellerOfferSerializer(read_only=True)
    category_ids = serializers.ListField(
        child=serializers.UUIDField(), write_only=True, required=False,
    )

    class Meta:
        model = BuyerDemand
        fields = [
            'id', 'buyer', 'buyer_name', 'title', 'description',
            'location', 'h3_index', 'radius_km', 'status',
            'expires_at', 'offers', 'accepted_offer', 'created_at', 'updated_at',
            'category_ids',
        ]
        read_only_fields = ['buyer', 'h3_index', 'status', 'expires_at', 'accepted_offer']

    def to_internal_value(self, data):
        point = parse_coordinate_dict(data, 'location')
        if point:
            data['location'] = point
        return super().to_internal_value(data)

    def validate(self, attrs):
        if not attrs.get('location'):
            raise serializers.ValidationError({
                'location': 'Valid coordinates are required'
            })
        return attrs


class CreateOfferSerializer(serializers.Serializer):
    price = serializers.DecimalField(max_digits=12, decimal_places=2)
    shop_id = serializers.UUIDField(required=False, allow_null=True)
    availability = serializers.BooleanField(default=True)
    message = serializers.CharField(required=False, allow_blank=True, allow_null=True)
