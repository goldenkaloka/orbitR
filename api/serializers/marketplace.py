from api.serializers.utils import parse_coordinate_dict
from apps.marketplace.models import (
    MarketplaceCategory,
    MarketplaceProduct,
    ProductImage,
    PostingTokenBundleOption,
    PostingTokenBundlePurchase,
)
from rest_framework import serializers


class MarketplaceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceCategory
        fields = ['id', 'name', 'icon']


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'is_primary']


class MarketplaceProductListSerializer(serializers.ModelSerializer):
    images = serializers.SerializerMethodField()
    seller_name = serializers.ReadOnlyField(source='seller.username')
    seller_avatar_url = serializers.SerializerMethodField()
    whatsapp_url = serializers.SerializerMethodField()
    zone_name = serializers.SerializerMethodField()

    class Meta:
        model = MarketplaceProduct
        fields = [
            'id', 'seller_name', 'seller_avatar_url', 'category', 'name', 'description',
            'price', 'condition', 'whatsapp_number', 'whatsapp_url',
            'location', 'h3_index', 'zone_name', 'status', 'expiry_date',
            'images', 'created_at'
        ]

    def get_seller_avatar_url(self, obj):
        seller = obj.seller
        if seller.avatar:
            request = self.context.get('request')
            return request.build_absolute_uri(seller.avatar.url) if request else seller.avatar.url
        return seller.avatar_url or None

    def get_images(self, obj):
        images = getattr(obj, 'prefetched_images', None)
        if images is None:
            images = obj.images.all()[:1]
        elif images:
            images = images[:1]
        return ProductImageSerializer(images, many=True, context=self.context).data

    def get_whatsapp_url(self, obj):
        import urllib.parse
        clean_number = obj.whatsapp_number.replace('+', '').replace(' ', '')
        text = urllib.parse.quote(f"Hi, I'm interested in your {obj.name} on orbit.")
        return f"https://wa.me/{clean_number}?text={text}"

    def get_zone_name(self, obj):
        if obj.h3_index:
            return f"Zone {obj.h3_index[:5].upper()}"
        return "Nearby"

    def to_internal_value(self, data):
        point = parse_coordinate_dict(data, 'location')
        if point:
            data['location'] = point
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get('location'):
            raise serializers.ValidationError({
                'location': 'Valid coordinates are required. Send location as {"latitude","longitude"} '
                            'or flat latitude/longitude fields.'
            })
        return attrs


class MarketplaceProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    seller_name = serializers.ReadOnlyField(source='seller.username')
    seller_avatar_url = serializers.SerializerMethodField()
    listing_fee = serializers.SerializerMethodField()
    whatsapp_url = serializers.SerializerMethodField()
    zone_name = serializers.SerializerMethodField()

    class Meta:
        model = MarketplaceProduct
        fields = [
            'id', 'seller', 'seller_name', 'seller_avatar_url', 'category', 'name', 'description', 
            'price', 'condition', 'whatsapp_number', 'whatsapp_url',
            'location', 'h3_index', 'zone_name', 'status', 'expiry_date', 
            'listing_fee', 'is_fee_paid', 'images', 'created_at'
        ]
        read_only_fields = ['seller', 'status', 'expiry_date', 'is_fee_paid', 'h3_index']

    def get_seller_avatar_url(self, obj):
        seller = obj.seller
        if seller.avatar:
            request = self.context.get('request')
            return request.build_absolute_uri(seller.avatar.url) if request else seller.avatar.url
        return seller.avatar_url or None

    def get_listing_fee(self, obj):
        return float(obj.calculate_listing_fee())

    def get_whatsapp_url(self, obj):
        import urllib.parse
        clean_number = obj.whatsapp_number.replace('+', '').replace(' ', '')
        text = urllib.parse.quote(f"Hi, I'm interested in your {obj.name} on orbit.")
        return f"https://wa.me/{clean_number}?text={text}"

    def get_zone_name(self, obj):
        if hasattr(obj, 'annotated_zone_name') and obj.annotated_zone_name:
            return obj.annotated_zone_name
        if not obj.h3_index:
            return "Nearby"
        return f"Zone {obj.h3_index[:5].upper()}"

    def to_internal_value(self, data):
        point = parse_coordinate_dict(data, 'location')
        if point:
            data['location'] = point
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get('location'):
            raise serializers.ValidationError({
                'location': 'Valid coordinates are required. Send location as {"latitude","longitude"} '
                            'or flat latitude/longitude fields.'
            })
        return attrs


class PostingTokenBundleOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostingTokenBundleOption
        fields = ['id', 'name', 'description', 'price', 'tokens_granted', 'is_active']


class PostingTokenBundlePurchaseSerializer(serializers.ModelSerializer):
    bundle_name = serializers.ReadOnlyField(source='bundle.name')

    class Meta:
        model = PostingTokenBundlePurchase
        fields = ['id', 'user', 'bundle', 'bundle_name', 'amount', 'tokens_granted', 'is_paid', 'created_at']
