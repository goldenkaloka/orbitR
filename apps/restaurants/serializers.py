from .models import Restaurant, Food, Orders, OrderDetail, Additives
from rest_framework import serializers



class RestaurantSerializers(serializers.ModelSerializer):
    model = Restaurant
    fields = [
        'name',
        'image'
    ]