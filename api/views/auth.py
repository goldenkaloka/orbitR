import random
import uuid
import re
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from apps.users.models import User, OTPCode
from domain.events.bus import EventBus
from domain.events.types import AuthOTPRequested, AuthUserVerified, AuthUserRegistered
from django.db import transaction
from django.core.cache import cache

def normalize_phone(phone):
    """
    Normalizes Tanzanian phone numbers to 0XXXXXXXXX format.
    Focuses on the last 9 digits to ensure consistency regardless of prefix.
    """
    if not phone: return None
    
    # Remove all non-digits
    digits = re.sub(r'\D', '', str(phone))
    
    # Tanzanian mobile numbers have 9 core digits (starting with 7 or 6)
    if len(digits) >= 9:
        core_number = digits[-9:]
        if core_number.startswith('7') or core_number.startswith('6'):
            return '0' + core_number
            
    return None # Invalid format

class RequestOTPView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @transaction.atomic
    def post(self, request):
        raw_phone = request.data.get('phone')
        phone = normalize_phone(raw_phone)
        
        if not phone:
            return Response({
                'error': 'Invalid phone number format. Use 07XXXXXXXX or +2557XXXXXXXX'
            }, status=status.HTTP_400_BAD_REQUEST)

        rate_key = f"otp_rate_{phone}"
        # Atomic rate limiting
        try:
            attempts = cache.get(rate_key, 0)
            if attempts >= 3:
                return Response(
                    {'error': 'Too many OTP requests. Please wait a few minutes.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )
            cache.incr(rate_key)
        except (ValueError, TypeError):
            # If key doesn't exist or is not a number, set it
            cache.set(rate_key, 1, 300)

        # Generate OTP
        code = str(random.randint(100000, 999999))
        
        # Save OTP (reset failed_attempts on new OTP generation)
        OTPCode.objects.update_or_create(
            phone=phone,
            defaults={'code': code, 'failed_attempts': 0}
        )
        
        # Publish Event
        EventBus.publish(AuthOTPRequested(
            aggregate_id=uuid.uuid4(),
            payload={'phone': phone, 'otp': code}
        ))
        
        return Response({'message': 'OTP sent successfully'})

class VerifyOTPView(APIView):
    """
    Login-only endpoint. Verifies OTP and returns JWT tokens.
    Rejects phone numbers that are not registered in the system.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    @transaction.atomic
    def post(self, request):
        phone = normalize_phone(request.data.get('phone'))
        code = request.data.get('code')
        
        if not phone or not code:
            return Response({'error': 'Phone and code are required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            # Use select_for_update to lock the record
            otp_record = OTPCode.objects.select_for_update().get(phone=phone)
            if not otp_record.is_valid():
                otp_record.delete()
                return Response({'error': 'OTP expired'}, status=status.HTTP_400_BAD_REQUEST)
            if otp_record.code != code:
                otp_record.failed_attempts += 1
                if otp_record.failed_attempts >= OTPCode.MAX_FAILED_ATTEMPTS:
                    otp_record.delete()
                    return Response({'error': 'Too many failed attempts. Please request a new OTP.'}, status=status.HTTP_400_BAD_REQUEST)
                otp_record.save(update_fields=['failed_attempts'])
                return Response({'error': 'Invalid OTP'}, status=status.HTTP_400_BAD_REQUEST)
                
            # OTP is valid — now check if user exists (LOGIN only, no auto-creation)
            try:
                user = User.objects.get(phone=phone)
            except User.DoesNotExist:
                # Delete OTP so it can't be reused
                otp_record.delete()
                return Response(
                    {'error': 'No account found with this phone number. Please register first.'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Delete OTP record
            otp_record.delete()
            
            # Publish Event
            EventBus.publish(AuthUserVerified(
                aggregate_id=uuid.uuid4(),
                payload={'phone': phone, 'user_id': str(user.id)}
            ))
                
            # Generate JWT
            refresh = RefreshToken.for_user(user)
            
            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': {
                    'id': str(user.id),
                    'phone': user.phone,
                    'username': user.username,
                    'role': user.role
                }
            })
            
        except OTPCode.DoesNotExist:
            return Response({'error': 'Invalid OTP or phone'}, status=status.HTTP_400_BAD_REQUEST)


class RegisterView(APIView):
    """
    Registration-only endpoint. Verifies OTP and creates a new user account.
    Rejects phone numbers that are already registered.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    @transaction.atomic
    def post(self, request):
        phone = normalize_phone(request.data.get('phone'))
        code = request.data.get('code')
        username = request.data.get('username')

        if not phone or not code:
            return Response({'error': 'Phone and code are required'}, status=status.HTTP_400_BAD_REQUEST)

        if not username or not username.strip():
            return Response({'error': 'Username is required for registration'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Use select_for_update to lock the record
            otp_record = OTPCode.objects.select_for_update().get(phone=phone)
            if not otp_record.is_valid():
                otp_record.delete()
                return Response({'error': 'OTP expired'}, status=status.HTTP_400_BAD_REQUEST)
            if otp_record.code != code:
                otp_record.failed_attempts += 1
                if otp_record.failed_attempts >= OTPCode.MAX_FAILED_ATTEMPTS:
                    otp_record.delete()
                    return Response({'error': 'Too many failed attempts. Please request a new OTP.'}, status=status.HTTP_400_BAD_REQUEST)
                otp_record.save(update_fields=['failed_attempts'])
                return Response({'error': 'Invalid OTP'}, status=status.HTTP_400_BAD_REQUEST)

            # OTP is valid — check that user does NOT already exist
            if User.objects.filter(phone=phone).exists():
                otp_record.delete()
                return Response(
                    {'error': 'An account with this phone number already exists. Please login instead.'},
                    status=status.HTTP_409_CONFLICT
                )

            # Create the new user
            user = User.objects.create(
                phone=phone,
                username=username.strip(),
            )

            # Delete OTP record
            otp_record.delete()

            # Publish Event
            EventBus.publish(AuthUserRegistered(
                aggregate_id=uuid.uuid4(),
                payload={'phone': phone, 'username': user.username, 'user_id': str(user.id)}
            ))

            # Generate JWT
            refresh = RefreshToken.for_user(user)

            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': {
                    'id': str(user.id),
                    'phone': user.phone,
                    'username': user.username,
                    'role': user.role
                }
            }, status=status.HTTP_201_CREATED)

        except OTPCode.DoesNotExist:
            return Response({'error': 'Invalid OTP or phone'}, status=status.HTTP_400_BAD_REQUEST)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            'user': {
                'id': str(user.id),
                'phone': user.phone,
                'username': user.username,
                'avatar_url': user.avatar_url,
                'role': user.role
            }
        })

    def patch(self, request):
        user = request.user
        username = request.data.get('username')
        avatar_url = request.data.get('avatar_url')
        onesignal_id = request.data.get('onesignal_id')
        
        if username:
            user.username = username
        if avatar_url:
            user.avatar_url = avatar_url
        if onesignal_id:
            user.onesignal_id = onesignal_id
            
        if username or avatar_url or onesignal_id:
            user.save()
            
        return Response({
            'user': {
                'id': str(user.id),
                'phone': user.phone,
                'username': user.username,
                'avatar_url': user.avatar_url,
                'role': user.role,
                'onesignal_id': user.onesignal_id
            }
        })
