"""
Dishes carry the serving window they belong to.

A menu is not one flat list all day: vendors tag each dish as breakfast,
lunch, dinner or all-day, and the API lets clients ask for a single meal.
All-day dishes must always be included, otherwise a restaurant that only
serves one meal would look empty at every other hour.
"""

from decimal import Decimal

from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)


class FoodMealPeriodTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner_menu', phone='+255700002100',
            password='password123', role='vendor',
        )
        self.customer = User.objects.create_user(
            username='cust_menu', phone='+255700002101',
            password='password123', role='customer',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='Meal Tag Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=20,
        )
        # The Restaurant post-save signal creates a default section; reuse it.
        self.section = self.restaurant.sections.first()
        self.breakfast = Food.objects.create(
            section=self.section, name='Mandazi', price=Decimal('1000.00'),
            meal_period=Food.BREAKFAST,
        )
        self.lunch = Food.objects.create(
            section=self.section, name='Ugali & Fish', price=Decimal('6000.00'),
            meal_period=Food.LUNCH,
        )
        self.dinner = Food.objects.create(
            section=self.section, name='Grilled Chicken', price=Decimal('9000.00'),
            meal_period=Food.DINNER,
        )
        self.all_day = Food.objects.create(
            section=self.section, name='Bottled Water', price=Decimal('500.00'),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.customer)

    def _names(self, response):
        payload = response.json()
        rows = payload['results'] if isinstance(payload, dict) else payload
        return {row['name'] for row in rows}

    def test_defaults_to_all_day(self):
        self.assertEqual(self.all_day.meal_period, Food.ALL_DAY)

    def test_meal_filter_includes_all_day_dishes(self):
        response = self.client.get('/api/products/', {'meal_period': 'LUNCH'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._names(response), {'Ugali & Fish', 'Bottled Water'})

    def test_all_day_filter_returns_only_always_available_dishes(self):
        response = self.client.get('/api/products/', {'meal_period': 'ALL_DAY'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._names(response), {'Bottled Water'})

    def test_filter_is_case_insensitive(self):
        response = self.client.get('/api/products/', {'meal_period': 'breakfast'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._names(response), {'Mandazi', 'Bottled Water'})

    def test_invalid_meal_period_is_rejected(self):
        response = self.client.get('/api/products/', {'meal_period': 'BRUNCH'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('meal_period', response.json())

    def test_restaurant_detail_exposes_meal_period(self):
        response = self.client.get(f'/api/restaurants/{self.restaurant.id}/')
        self.assertEqual(response.status_code, 200)
        by_name = {food['name']: food for food in response.json()['foods']}
        self.assertEqual(by_name['Mandazi']['meal_period'], Food.BREAKFAST)
        self.assertEqual(by_name['Bottled Water']['meal_period'], Food.ALL_DAY)

    def test_vendor_can_tag_a_new_dish(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            '/api/products/',
            {
                'name': 'Chapati & Beans',
                'price': '2500.00',
                'category': 'MAIN',
                'meal_period': Food.BREAKFAST,
                'section': str(self.section.id),
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['meal_period'], Food.BREAKFAST)

    def test_vendor_can_move_a_dish_to_another_meal(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(
            f'/api/products/{self.lunch.id}/',
            {'meal_period': Food.DINNER},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.lunch.refresh_from_db()
        self.assertEqual(self.lunch.meal_period, Food.DINNER)

    def test_vendor_create_without_meal_period_defaults_to_all_day(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            '/api/products/',
            {
                'name': 'Mango Juice',
                'price': '2000.00',
                'category': 'DRINKS',
                'section': str(self.section.id),
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['meal_period'], Food.ALL_DAY)

    def test_popular_is_a_flag_not_a_category(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            '/api/products/',
            {
                'name': 'Chef Special Pilau',
                'price': '8000.00',
                'category': 'MAIN',
                'is_popular': True,
                'section': str(self.section.id),
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertEqual(payload['category'], 'MAIN')
        self.assertTrue(payload['is_popular'])

    def test_legacy_popular_category_is_still_accepted(self):
        # Old app builds still send category=POPULAR; the API must not break.
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            '/api/products/',
            {
                'name': 'Legacy Combo',
                'price': '5500.00',
                'category': 'POPULAR',
                'section': str(self.section.id),
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['category'], 'POPULAR')

    def test_vendor_can_toggle_popular(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(
            f'/api/products/{self.lunch.id}/',
            {'is_popular': True},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.lunch.refresh_from_db()
        self.assertTrue(self.lunch.is_popular)

    def test_dishes_default_to_not_popular(self):
        self.assertFalse(self.lunch.is_popular)

    def test_restaurant_detail_exposes_is_popular(self):
        self.lunch.is_popular = True
        self.lunch.save(update_fields=['is_popular'])
        response = self.client.get(f'/api/restaurants/{self.restaurant.id}/')
        self.assertEqual(response.status_code, 200)
        by_name = {food['name']: food for food in response.json()['foods']}
        self.assertTrue(by_name['Ugali & Fish']['is_popular'])
        self.assertFalse(by_name['Mandazi']['is_popular'])
