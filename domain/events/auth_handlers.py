import logging

from domain.events.bus import EventBus
from domain.events.types import AuthOTPRequested, AuthUserVerified, AuthUserRegistered
from infrastructure.notifications.service import NotificationService

logger = logging.getLogger(__name__)


def on_auth_otp_requested(event):
    phone = event.payload.get('phone')
    otp = event.payload.get('otp')
    message = f"Your Orbit verification code is {otp}. It will expire in 5 minutes. Do not share this code with anyone."
    NotificationService.send_sms(phone, message)
    logger.info(f"OTP request processed for {phone}")


def on_auth_user_verified(event):
    logger.info(f"User verified: {event.payload.get('phone')}")


def on_auth_user_registered(event):
    logger.info(f"New User registered: {event.payload.get('phone')} with username {event.payload.get('username')}")


def register_handlers():
    EventBus.subscribe(AuthOTPRequested, on_auth_otp_requested)
    EventBus.subscribe(AuthUserVerified, on_auth_user_verified)
    EventBus.subscribe(AuthUserRegistered, on_auth_user_registered)
