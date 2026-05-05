from rest_framework import serializers
from apps.marketplace.models import MarketplaceProduct, ProductImage, MarketplaceCategory
from api.serializers.utils import parse_coordinate_dict

class MarketplaceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceCategory
        fields = ['id', 'name', 'icon']

class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'is_primary']

class MarketplaceProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    seller_name = serializers.ReadOnlyField(source='seller.username')
    listing_fee = serializers.SerializerMethodField()
    whatsapp_url = serializers.SerializerMethodField()

    class Meta:
        model = MarketplaceProduct
        fields = [
            'id', 'seller', 'seller_name', 'category', 'name', 'description', 
            'price', 'condition', 'whatsapp_number', 'whatsapp_url',
            'location', 'h3_index', 'status', 'expiry_date', 
            'listing_fee', 'is_fee_paid', 'images', 'created_at'
        ]
        read_only_fields = ['seller', 'status', 'expiry_date', 'is_fee_paid', 'h3_index']

    def get_listing_fee(self, obj):
        return float(obj.calculate_listing_fee())

    def get_whatsapp_url(self, obj):
        # Format: https://wa.me/number?text=I'm interested in your [Product Name]
        import urllib.parse
        clean_number = obj.whatsapp_number.replace('+', '').replace(' ', '')
        text = urllib.parse.quote(f"Hi, I'm interested in your {obj.name} on Mwanachuo.")
        return f"https://wa.me/{clean_number}?text={text}"

    def to_internal_value(self, data):
        """Handle GeoJSON/Point conversion using shared utility."""
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
