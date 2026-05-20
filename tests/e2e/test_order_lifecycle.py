import uuid
from decimal import Decimal
from unittest.mock import patch
from django.urls import reverse
from django.test import override_settings

from rest_framework.test import APITransactionTestCase
from rest_framework import status

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
from apps.orders.models import Order, OrderItem
from apps.delivery.models import Driver
from apps.payments.models import Payment, Transaction
from apps.pricing.models import PricingPolicy

class OrderLifecycleE2ETest(APITransactionTestCase):


    def setUp(self):
        # 1. Setup Users
        self.customer = User.objects.create_user(
            username='customer', 
            phone='+255700000001', 
            password='password123',
            role='customer'
        )
        self.restaurant_owner = User.objects.create_user(
            username='owner', 
            phone='+255700000002', 
            password='password123',
            role='vendor'
        )
        self.driver_user = User.objects.create_user(
            username='driver', 
            phone='+255700000003', 
            password='password123',
            role='driver'
        )


        # 2. Setup Driver Profile
        from django.contrib.gis.geos import Point
        self.driver = Driver.objects.create(
            user=self.driver_user,
            status='AVAILABLE',
            location=Point(39.2083, -6.7924)
        )


        # 3. Setup Restaurant & Product — with geo_location for distance calculation
        self.restaurant = Restaurant.objects.create(
            owner=self.restaurant_owner,
            name="Mwanachuo Cafe",
            location="University Main Hall",
            geo_location=Point(39.2083, -6.7924),
            max_delivery_radius_km=10.0
        )
        self.food = Food.objects.create(
            vendor=self.restaurant,
            name="Beef Burger",
            price=Decimal('5000.00')
        )

        # 4. Setup OperationalZone covering the delivery area
        from django.contrib.gis.geos import Polygon, MultiPolygon
        from apps.geo.models import OperationalZone, ZoneH3Index
        import h3
        poly = Polygon((
            (39.19, -6.80), (39.23, -6.80), (39.23, -6.78),
            (39.19, -6.78), (39.19, -6.80)
        ))
        self.op_zone = OperationalZone.objects.create(
            name="Test Zone",
            boundary=MultiPolygon(poly),
            is_active=True
        )
        from apps.core.geo_utils import ZONE_RESOLUTION
        ZoneH3Index.objects.create(
            zone=self.op_zone,
            h3_index=h3.latlng_to_cell(-6.7930, 39.2100, ZONE_RESOLUTION),
            resolution=ZONE_RESOLUTION
        )
        ZoneH3Index.objects.create(
            zone=self.op_zone,
            h3_index=h3.latlng_to_cell(-6.7924, 39.2083, ZONE_RESOLUTION),
            resolution=ZONE_RESOLUTION
        )

        # 5. Setup PricingPolicy
        self.policy = PricingPolicy.objects.create(
            name="Test Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00')
        )

        # Authenticate as customer
        self.client.force_authenticate(user=self.customer)

    @patch('infrastructure.payment_gateways.factory.AirpayGateway')
    def test_full_order_to_disbursement_flow(self, mock_sdk_class):

        """
        Tests the entire flow:
        Order -> Pay -> Webhook -> Confirm -> Deliver -> Disbursement
        """
        # 1. Mock the SDK instance and its methods
        mock_sdk_instance = mock_sdk_class.return_value
        provider_ref = "AIRPAY_12345"
        
        # Mock mobile_checkout (Step 1/2)
        mock_sdk_instance.collect.return_value = {
            'success': True,
            'transactionId': provider_ref,
            'provider_ref': provider_ref
        }
        
        # Mock split_settlement (Step 4 - Disbursement)
        mock_sdk_instance.split_settlement.return_value = {
            'success': True,
            'status': 'Success',
            'transactionId': 'DISB_999'
        }


        # --- STEP 1: CREATE ORDER ---
        from django.contrib.gis.geos import Point
        order_url = '/api/orders/'
        order_data = {
            'restaurant': self.restaurant.id,
            'items': [
                {'food': self.food.id, 'quantity': 2}
            ],
            'delivery_address': 'Hostel Block A',
            'delivery_location': {'type': 'Point', 'coordinates': [39.2100, -6.7930]}
        }
        response = self.client.post(order_url, order_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order_id = response.data['id']
        
        # Verify initial totals — fee is dynamically calculated now
        self.assertEqual(Decimal(response.data['items_subtotal']), Decimal('10000.00'))
        # Delivery fee is dynamic — just check it's a positive number
        delivery_fee = Decimal(response.data['delivery_fee'])
        service_fee = Decimal(response.data['service_fee'])
        self.assertGreater(delivery_fee, Decimal('0'))
        self.assertGreater(service_fee, Decimal('0'))
        self.assertEqual(
            Decimal(response.data['total_amount']),
            Decimal('10000.00') + delivery_fee + service_fee
        )

        
        # --- STEP 2: INITIATE PAYMENT ---
        pay_url = f'/api/orders/{order_id}/pay/'
        response = self.client.post(pay_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        
        # Verify Payment record exists
        payment = Payment.objects.get(order_id=order_id)
        self.assertEqual(payment.status, 'INITIATED')
        self.assertEqual(payment.provider_reference, provider_ref)

        # --- STEP 3: MOCK WEBHOOK CALLBACK FROM AIRPAY ---
        webhook_url = '/api/payments/airpay/callback/'
        # Airpay expects an encrypted string 'response'. We'll mock the decryptor instead.
        
        with patch('api.views.payments.AirpayTransformer.decrypt') as mock_decrypt:
            mock_decrypt.return_value = {
                'orderid': str(order_id),
                'ap_transactionid': provider_ref,
                'transaction_payment_status': 'SUCCESS',
                'rrn': '123456789',
                'ap_SecureHash': 'test_hash'
            }
            # Also patch verify_secure_hash if it runs
            with patch('api.views.payments.AirpayTransformer.verify_secure_hash', return_value=True):
                self.client.force_authenticate(user=None)
                response = self.client.post(webhook_url, {'response': 'ENCRYPTED_MOCK_PAYLOAD'}, format='json')
                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


        # Verify Order is now CONFIRMED and Payment is COLLECTED
        order = Order.objects.get(id=order_id)
        self.assertEqual(order.status, 'CONFIRMED')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'COLLECTED')

        # Verify Ledger: Customer -> Platform
        self.assertTrue(Transaction.objects.filter(
            payment=payment,
            transaction_type='customer_to_platform'
        ).exists())

        # --- STEP 4: LOGISTICS WORKFLOW ---
        
        # 1. Vendor Starts Preparing
        self.client.force_authenticate(user=self.restaurant_owner)
        prep_url = f'/api/orders/{order_id}/start-preparing/'
        response = self.client.post(prep_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 2. Vendor Marks as Ready
        ready_url = f'/api/orders/{order_id}/ready/'
        response = self.client.post(ready_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 3. Assign Driver (Creates DeliveryAssignment)
        # Using a superuser/admin context if needed, but here vendor owner is fine for test
        assign_url = f'/api/orders/{order_id}/assign-driver/'
        response = self.client.post(assign_url, {'driver_id': str(self.driver.id)}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 4. Driver Pickups Order
        self.client.force_authenticate(user=self.driver_user)
        pickup_url = f'/api/orders/{order_id}/pickup/'
        response = self.client.post(pickup_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


        # 3. Transition to DELIVERED
        deliver_url = f'/api/orders/{order_id}/deliver/'
        # No need to patch disburse anymore, it's covered by mock_sdk_instance.post
        
        response = self.client.post(deliver_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


        # --- STEP 5: VERIFY AUTOMATIC DISBURSEMENT ---
        # Wait for event bus (in tests, EventBus.publish uses transaction.on_commit which is simulated)
        # Actually, APITransactionTestCase runs outside of a transaction managed by TestCase,
        # so on_commit actions will fire.
        
        payment.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(payment.status, 'DISBURSED')


        
        # Verify final Transaction records:
        # 1. Platform -> Restaurant (90% of items_subtotal)
        self.assertTrue(Transaction.objects.filter(
            payment=payment,
            transaction_type='platform_to_restaurant',
            amount=Decimal('9000.00')  # 90% of 10000
        ).exists())

        # 2. Platform -> Driver (100% of delivery_fee)
        self.assertTrue(Transaction.objects.filter(
            payment=payment,
            transaction_type='platform_to_driver',
            amount=delivery_fee  # dynamically calculated delivery fee
        ).exists())


