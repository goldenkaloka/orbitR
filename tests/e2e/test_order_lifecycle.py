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

@override_settings(AZAMPAY_CONFIG={
    'CLIENT_ID': 'test_id',
    'CLIENT_SECRET': 'test_secret',
    'API_KEY': 'test_key',
    'APP_NAME': 'Test',
    'SANDBOX': True
})
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
        self.driver = Driver.objects.create(
            user=self.driver_user,
            status='AVAILABLE',
            latitude=-6.7924,
            longitude=39.2083
        )

        # 3. Setup Restaurant & Product
        self.restaurant = Restaurant.objects.create(
            owner=self.restaurant_owner,
            name="Mwanachuo Cafe",
            location="University Main Hall"
        )
        self.food = Food.objects.create(
            vendor=self.restaurant,
            name="Beef Burger",
            price=Decimal('5000.00')
        )

        # Authenticate as customer
        self.client.force_authenticate(user=self.customer)

    @patch('domain.services.payment_services.AirpayGateway')
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
        order_url = '/api/orders/'
        order_data = {
            'restaurant': self.restaurant.id,
            'items': [
                {'food': self.food.id, 'quantity': 2}
            ],
            'delivery_address': 'Hostel Block A'
        }
        response = self.client.post(order_url, order_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order_id = response.data['id']
        
        # Verify initial totals (2 * 5000 = 10000 subtotal + 500 delivery fee)
        self.assertEqual(Decimal(response.data['items_subtotal']), Decimal('10000.00'))
        self.assertEqual(Decimal(response.data['delivery_fee']), Decimal('500.00'))
        self.assertEqual(Decimal(response.data['total_amount']), Decimal('10500.00'))

        
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
        # 1. Platform -> Restaurant (90% of 10000 = 9000)
        self.assertTrue(Transaction.objects.filter(
            payment=payment,
            transaction_type='platform_to_restaurant',
            amount=Decimal('9000.00')
        ).exists())

        # 2. Platform -> Driver (100% of 500 = 500)
        self.assertTrue(Transaction.objects.filter(
            payment=payment,
            transaction_type='platform_to_driver',
            amount=Decimal('500.00')
        ).exists())


