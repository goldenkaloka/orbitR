from apps.users.models import OTPCode, User
from api.serializers.core import UserSerializer
from apps.delivery.models import Driver
from apps.restaurants.models import Restaurant, RestaurantSection, SectionStaff
from django.contrib.gis.geos import Point
from django.db.models import Exists, OuterRef, Subquery
from django.test import TestCase


class UserModelTests(TestCase):
    def test_user_string_prefers_username(self):
        user = User.objects.create_user(
            phone="+255670000003",
            username="amina",
            password="pass",
        )

        self.assertEqual(str(user), "amina (customer)")

    def test_new_otp_code_is_valid(self):
        otp = OTPCode.objects.create(phone="+255670000004", code="123456")

        self.assertTrue(otp.is_valid())


class UserSerializerPerformanceTests(TestCase):
    def test_annotated_user_serializer_avoids_per_user_role_queries(self):
        customer = User.objects.create_user(
            phone="+255670000005",
            username="customer",
            password="pass",
        )
        driver_user = User.objects.create_user(
            phone="+255670000006",
            username="driver",
            password="pass",
            role="driver",
        )
        owner = User.objects.create_user(
            phone="+255670000007",
            username="owner",
            password="pass",
        )
        staff = User.objects.create_user(
            phone="+255670000008",
            username="staff",
            password="pass",
        )
        Driver.objects.create(user=driver_user)
        restaurant = Restaurant.objects.create(
            owner=owner,
            name="Perf Restaurant",
            geo_location=Point(39.2, -6.8),
        )
        section = RestaurantSection.objects.create(restaurant=restaurant, name="Grill")
        SectionStaff.objects.create(user=staff, section=section)

        users = list(User.objects.filter(id__in=[
            customer.id,
            driver_user.id,
            owner.id,
            staff.id,
        ]).annotate(
            has_driver_profile=Exists(Driver.objects.filter(user_id=OuterRef('pk'))),
            driver_profile_id=Subquery(Driver.objects.filter(user_id=OuterRef('pk')).values('id')[:1]),
            owns_restaurant=Exists(Restaurant.objects.filter(owner_id=OuterRef('pk'))),
            has_section_staff=Exists(SectionStaff.objects.filter(user_id=OuterRef('pk'))),
        ))

        with self.assertNumQueries(0):
            data = UserSerializer(users, many=True).data

        by_phone = {row['phone']: row for row in data}
        self.assertTrue(by_phone["+255670000006"]["is_driver"])
        self.assertTrue(by_phone["+255670000007"]["is_restaurant_owner"])
        self.assertEqual(by_phone["+255670000008"]["serialized_role"], "vendor_staff")
        self.assertFalse(by_phone["+255670000008"]["can_be_driver"])
