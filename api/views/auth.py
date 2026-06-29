import re
import secrets
import uuid

from apps.users.models import OTPCode, User
from django.core.cache import cache
from django.db import transaction
from domain.events.bus import EventBus
from domain.events.types import AuthOTPRequested, AuthUserRegistered, AuthUserVerified
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken


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


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _increment_or_set_cache_counter(key, ttl):
    try:
        attempts = cache.get(key, 0)
        cache.incr(key)
        return attempts + 1
    except (ValueError, TypeError):
        cache.set(key, 1, ttl)
        return 1

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

        ip_attempts = _increment_or_set_cache_counter(f"otp_ip_rate_{_client_ip(request)}", 3600)
        if ip_attempts > 30:
            return Response(
                {'error': 'Too many OTP requests from this IP. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        rate_key = f"otp_rate_{phone}"
        attempts = _increment_or_set_cache_counter(rate_key, 300)
        if attempts > 3:
            return Response(
                {'error': 'Too many OTP requests. Please wait a few minutes.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        # Generate OTP
        code = f"{secrets.randbelow(900000) + 100000}"

        # Delete any existing OTP to get fresh created_at timestamp (fresh 5-min timer)
        OTPCode.objects.filter(phone=phone).delete()

        # Create new OTP with fresh timestamp
        OTPCode.objects.create(
            phone=phone,
            code=code,
            failed_attempts=0
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

        attempts = _increment_or_set_cache_counter(f"verify_rate_{phone}", 300)
        if attempts > 10:
            return Response(
                {'error': 'Too many verify attempts. Please wait a few minutes.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        # IP-based rate limit: max 20 verify attempts per IP per minute
        ip_attempts = _increment_or_set_cache_counter(f"verify_ip_rate_{_client_ip(request)}", 60)
        if ip_attempts > 20:
            return Response(
                {'error': 'Too many requests from this IP. Please slow down.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
            
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
                    'role': user.serialized_role
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

        attempts = _increment_or_set_cache_counter(f"register_rate_{phone}", 600)
        if attempts > 5:
            return Response(
                {'error': 'Too many registration attempts. Please wait a while.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

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
                    'role': user.serialized_role
                }
            }, status=status.HTTP_201_CREATED)

        except OTPCode.DoesNotExist:
            return Response({'error': 'Invalid OTP or phone'}, status=status.HTTP_400_BAD_REQUEST)


class WsTicketView(APIView):
    """
    Issues credentials for WebSocket connections.

    Returns both a legacy Channels ticket (for backward compatibility)
    and a Centrifugo JWT token (for new Centrifugo-powered clients).

    Flow:
        POST /auth/ws-ticket/          (Authorization: Bearer <access_token>)
        ← { "token": "<centrifugo_jwt>", "ticket": "<opaque_32_byte_hex>", "expires_in": 3600 }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.events.centrifugo import generate_connection_token

        token = generate_connection_token(str(request.user.id))
        return Response({
            'token': token,
            'expires_in': 3600,
        })


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            'user': {
                'id': str(user.id),
                'phone': user.phone,
                'username': user.username,
                'avatar_url': user.avatar.url if user.avatar else user.avatar_url,
                'role': user.serialized_role
            }
        })

    def patch(self, request):
        user = request.user
        username = request.data.get('username')
        avatar_url = request.data.get('avatar_url')
        onesignal_id = request.data.get('onesignal_id')
        
        if username:
            user.username = username
        if request.FILES.get('avatar'):
            user.avatar = request.FILES['avatar']
            user.avatar_url = None  # clear old URL in favour of uploaded file
        if avatar_url:
            user.avatar_url = avatar_url
        if onesignal_id:
            user.onesignal_id = onesignal_id
            
        if username or avatar_url or onesignal_id or request.FILES.get('avatar'):
            user.save()
            
        return Response({
            'user': {
                'id': str(user.id),
                'phone': user.phone,
                'username': user.username,
                'avatar_url': user.avatar.url if user.avatar else user.avatar_url,
                'role': user.serialized_role,
                'onesignal_id': user.onesignal_id
            }
        })
