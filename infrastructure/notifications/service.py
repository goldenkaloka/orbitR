import logging
import threading

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

class NotificationService:
    """
    Unified Notification Service
    SMS: RafikiSMS
    Push: OneSignal
    """

    @staticmethod
    def send_sms(phone, message):
        """
        Send SMS via RafikiSMS API
        Ref: https://api.rafikisms.com
        """
        logger.info(f"📲 SENDING SMS TO {phone} via RafikiSMS: {message}")

        try:
            mock_sms = getattr(settings, 'MOCK_SMS', True)
            logger.info(f"DEBUG: MOCK_SMS = {mock_sms} (type: {type(mock_sms).__name__})")
            if mock_sms:
                print(f"\n======== MOCK RAFIKISMS SMS ========")
                print(f"To: {phone}")
                print(f"Message: {message}")
                print(f"====================================\n")
                return True

            # RafikiSMS requires 255XXXXXXXXX format (international)
            clean_phone = phone.strip().replace('+', '')
            if clean_phone.startswith('0'):
                clean_phone = '255' + clean_phone[1:]
            elif not clean_phone.startswith('255'):
                clean_phone = '255' + clean_phone

            config = settings.RAFIKI_CONFIG
            api_key = config.get('API_KEY')
            sender = config.get('SENDER_NAME')

            if not api_key:
                logger.warning("RafikiSMS API key not found. Skipping SMS.")
                return False

            url = f"{config.get('BASE_URL', 'https://api.rafikisms.com')}/v1/vendor/send-sms"

            payload = {
                "phone": clean_phone,
                "message": message
            }
            if sender:
                payload["sender_id"] = sender
                payload["sender"] = sender

            headers = {
                "Content-Type": "application/json",
                "X-API-Key": api_key
            }

            response = requests.post(url, json=payload, headers=headers, timeout=10)
            logger.info(f"RafikiSMS API Response: status={response.status_code}, body={response.text}")

            if response.status_code in [200, 201]:
                # Try to parse JSON and check success flag if present
                try:
                    res_json = response.json()
                    if isinstance(res_json, dict) and res_json.get('success') is False:
                        logger.error(f"❌ RafikiSMS API reported failure: {res_json}")
                        return False
                except Exception:
                    pass
                logger.info(f"✅ RafikiSMS sent successfully to {phone}")
                return True
            else:
                logger.error(f"❌ RafikiSMS failed: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error sending RafikiSMS: {str(e)}")
            return False

    @staticmethod
    def send_push(user_id, title, body, data=None):
        """
        Send push notification via OneSignal.

        Args:
            user_id:    The recipient user's UUID.
            title:      Notification title.
            body:       Notification body text.
            data:       Optional dict of custom data payload. Must include a 'type' key
                        for action-button determination. Known types:
                        - 'batch_offer' / 'new_order'  → Accept / Decline buttons
                        - 'vendor_order'               → Accept Order / Start Preparing buttons
                        - all other types              → informational (no buttons)
        """
        logger.info(f"🔔 SENDING PUSH TO USER {user_id}: {title} - {body}")

        # Save to database
        notification_id = None
        try:
            from apps.core.models import Notification
            notification = Notification.objects.create(
                user_id=user_id,
                title=title,
                body=body,
                data=data
            )
            notification_id = str(notification.id)
        except Exception as e:
            logger.error(f"Failed to save notification to DB: {str(e)}")

        # Broadcast notification event to user's Centrifugo channel for real-time badge update
        try:
            from apps.events.tasks import centrifugo_publish
            centrifugo_publish.delay(
                f'users:{user_id}',
                {
                    'type': 'notification',
                    'id': notification_id,
                    'title': title,
                    'body': body,
                    'data': data or {},
                }
            )
        except Exception as e:
            logger.error(f"Failed to broadcast notification to WS: {str(e)}")

        try:
            from apps.users.models import User
            user = User.objects.get(id=user_id)
            if not user.onesignal_id:
                logger.warning(f"User {user_id} has no onesignal_id. Skipping push.")
                return False

            def _send():
                try:
                    payload = {
                        "app_id": settings.ONESIGNAL_APP_ID,
                        "include_player_ids": [user.onesignal_id],
                        "headings": {"en": title},
                        "contents": {"en": body},
                        "data": data or {},
                    }

                    # Auto-detect action buttons from notification type
                    notif_type = (data or {}).get('type')
                    if notif_type in ('batch_offer', 'new_order'):
                        payload["buttons"] = [
                            {"id": "accept", "text": "Accept"},
                            {"id": "decline", "text": "Decline"},
                        ]
                    elif notif_type == 'vendor_order':
                        payload["buttons"] = [
                            {"id": "confirm", "text": "Accept Order"},
                            {"id": "start_preparing", "text": "Start Preparing"},
                        ]

                    headers = {
                        "Authorization": f"Basic {settings.ONESIGNAL_REST_API_KEY}",
                        "Content-Type": "application/json; charset=utf-8"
                    }

                    response = requests.post(
                        "https://onesignal.com/api/v1/notifications",
                        json=payload,
                        headers=headers,
                        timeout=5
                    )

                    if response.status_code == 200:
                        logger.info(f"✅ OneSignal Push Sent successfully to {user_id}")
                    else:
                        logger.error(f"❌ OneSignal Push Failed: {response.status_code} - {response.text}")
                except Exception as e:
                    logger.error(f"Error sending OneSignal push: {str(e)}")

            # Background thread
            threading.Thread(target=_send, daemon=True).start()
            return True

        except Exception as e:
            logger.error(f"Error initiating OneSignal push: {str(e)}")
            return False

    @staticmethod
    def notify_admins(title, body, data=None):
        """
        Send push notification to all admins
        """
        from apps.users.models import User
        admins = User.objects.filter(role='admin')
        for admin in admins:
            NotificationService.send_push(
                user_id=str(admin.id),
                title=title,
                body=body,
                data=data
            )
