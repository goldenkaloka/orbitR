import logging
import time
import jwt
import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

CENTRIFUGO_API_TIMEOUT = 5

def _headers():
    return {
        "Authorization": f"apikey {settings.CENTRIFUGO_API_KEY}",
        "Content-Type": "application/json",
    }

def _api_url(path):
    return f"{settings.CENTRIFUGO_API_URL}/api/{path}"

def publish(channel: str, data: dict) -> bool:
    try:
        resp = httpx.post(
            _api_url("publish"),
            json={"channel": channel, "data": data},
            headers=_headers(),
            timeout=CENTRIFUGO_API_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Centrifugo publish to {channel} failed: {e}")
        return False

def broadcast(channels: list, data: dict) -> bool:
    try:
        resp = httpx.post(
            _api_url("broadcast"),
            json={"channels": channels, "data": data},
            headers=_headers(),
            timeout=CENTRIFUGO_API_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Centrifugo broadcast to {channels} failed: {e}")
        return False

def disconnect_user(user_id: str) -> bool:
    try:
        resp = httpx.post(
            _api_url("disconnect"),
            json={"user": user_id},
            headers=_headers(),
            timeout=CENTRIFUGO_API_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Centrifugo disconnect user {user_id} failed: {e}")
        return False

def presence(channel: str) -> dict | None:
    try:
        resp = httpx.post(
            _api_url("presence"),
            json={"channel": channel},
            headers=_headers(),
            timeout=CENTRIFUGO_API_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("result", {})
    except Exception as e:
        logger.warning(f"Centrifugo presence for {channel} failed: {e}")
        return None

def generate_connection_token(user_id: str) -> str:
    payload = {
        "sub": str(user_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, settings.CENTRIFUGO_HMAC_SECRET, algorithm="HS256")

def generate_subscription_token(user_id: str, channel: str) -> str:
    payload = {
        "sub": str(user_id),
        "channel": channel,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, settings.CENTRIFUGO_HMAC_SECRET, algorithm="HS256")
