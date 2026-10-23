import logging
import re
import secrets
import time
import uuid

from apps.users.models import OTPCode, User
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from domain.events.bus import EventBus
from domain.events.types import AuthOTPRequested, AuthUserRegistered, AuthUserVerified
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

logger = logging.getLogger(__name__)


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


def find_user_by_phone(phone):
    """Find a user regardless of whether the stored phone uses 0…, +255… or 255….

    Accounts created by the admin/seed scripts are sometimes stored in E.164
    form while the OTP flow normalises to the local 0… form. A direct
    ``User.objects.get(phone=phone)`` therefore rejected valid logins.
    """
    if not phone:
        return None
    core = phone[1:] if phone.startswith('0') else phone
    variants = {phone, f'+255{core}', f'255{core}', core}
    return User.objects.filter(phone__in=variants).first()


def _review_phone():
    """Normalised phone configured for store-review logins, or None."""
    if not getattr(settings, 'REVIEW_LOGIN_ENABLED', False):
        return None
    return normalize_phone(getattr(settings, 'REVIEW_LOGIN_PHONE', ''))


def _is_review_login(phone, code):
    """True only for the single configured reviewer phone + fixed OTP."""
    review_phone = _review_phone()
    return bool(
        review_phone
        and phone == review_phone
        and code is not None
        and str(code).strip() == str(getattr(settings, 'REVIEW_LOGIN_OTP', ''))
    )


def _unique_username(base):
    """Return a username that isn't already taken."""
    base = (base or 'App Reviewer').strip() or 'App Reviewer'
    if not User.objects.filter(username=base).exists():
        return base
    suffix = 1
    while User.objects.filter(username=f"{base} {suffix}").exists():
        suffix += 1
    return f"{base} {suffix}"


def _auth_payload(user, status_code=status.HTTP_200_OK):
    refresh = RefreshToken.for_user(user)
    return Response({
        'refresh': str(refresh),
        'access': str(refresh.access_token),
        'user': {
            'id': str(user.id),
            'phone': user.phone,
            'username': user.username,
            'role': user.serialized_role,
        },
    }, status=status_code)


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

        # Short resend cooldown: stops double-taps and rapid SMS spam without
        # burning the per-phone/IP rate limits.
        now = time.time()
        cooldown_key = f"otp_cooldown_{phone}"
        last_request = cache.get(cooldown_key)
        cooldown = int(getattr(settings, 'OTP_RESEND_COOLDOWN', 60))
        if last_request and cooldown > 0:
            elapsed = now - last_request
            if elapsed < cooldown:
                retry_after = max(1, int(cooldown - elapsed))
                return Response(
                    {
                        'error': f'Please wait {retry_after}s before requesting another code.',
                        'retry_after': retry_after,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )

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

        # Generate OTP. The store-review phone always uses the configured fixed
        # code so the reviewer can log in without reading the SMS.
        code = f"{secrets.randbelow(900000) + 100000}"
        if phone == _review_phone():
            code = str(settings.REVIEW_LOGIN_OTP)
            logger.warning("[REVIEW LOGIN] Issuing fixed OTP for %s", phone)

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

        if cooldown > 0:
            cache.set(cooldown_key, now, cooldown)

        return Response({
            'message': 'OTP sent successfully',
            'resend_after': cooldown,
        })

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

        # Store-review bypass: fixed OTP, ignores expiry, auto-provisions the
        # reviewer's customer account so login works on a clean install.
        if _is_review_login(phone, code):
            logger.warning("[REVIEW LOGIN] Fixed-OTP login for %s", phone)
            user = find_user_by_phone(phone)
            if user is None:
                user = User.objects.create_user(
                    phone=phone,
                    username=_unique_username(getattr(settings, 'REVIEW_LOGIN_USERNAME', 'App Reviewer')),
                )
            OTPCode.objects.filter(phone=phone).delete()
            EventBus.publish(AuthUserVerified(
                aggregate_id=uuid.uuid4(),
                payload={'phone': phone, 'user_id': str(user.id)}
            ))
            return _auth_payload(user)

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
            user = find_user_by_phone(phone)
            if user is None:
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
            return _auth_payload(user)
            
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

        review_login = _is_review_login(phone, code)

        if not review_login and (not username or not username.strip()):
            return Response({'error': 'Username is required for registration'}, status=status.HTTP_400_BAD_REQUEST)

        # Store-review bypass: fixed OTP, ignores expiry. If the reviewer's
        # account already exists, log them in instead of returning a conflict.
        if review_login:
            logger.warning("[REVIEW LOGIN] Fixed-OTP registration for %s", phone)
            existing = find_user_by_phone(phone)
            OTPCode.objects.filter(phone=phone).delete()
            if existing is not None:
                return _auth_payload(existing)
            user = User.objects.create_user(
                phone=phone,
                username=_unique_username(
                    username or getattr(settings, 'REVIEW_LOGIN_USERNAME', 'App Reviewer')
                ),
            )
            EventBus.publish(AuthUserRegistered(
                aggregate_id=uuid.uuid4(),
                payload={'phone': phone, 'username': user.username, 'user_id': str(user.id)}
            ))
            return _auth_payload(user, status_code=status.HTTP_201_CREATED)

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
            if find_user_by_phone(phone) is not None:
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
            return _auth_payload(user, status_code=status.HTTP_201_CREATED)

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
