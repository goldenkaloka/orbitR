from rest_framework import serializers

from .models import Additives, Food, OrderDetail, Orders, Restaurant


class RestaurantSerializers(serializers.ModelSerializer):
    model = Restaurant
    fields = [
        'name',
        'image'
    ]