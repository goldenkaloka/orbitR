import logging
import threading
import requests
import json
import base64
from django.conf import settings

logger = logging.getLogger(__name__)

class NotificationService:
    """
    Unified Notification Service
    SMS: Beem Africa
    Push: OneSignal
    """

    @staticmethod
    def send_sms(phone, message):
        """
        Send SMS via Beem Africa
        Ref: https://docs.beem.africa/
        """
        logger.info(f"📲 SENDING SMS TO {phone} via Beem: {message}")
        
        try:
            if getattr(settings, 'MOCK_SMS', True):
                print(f"\n======== MOCK BEEM SMS ========")
                print(f"To: {phone}")
                print(f"Message: {message}")
                print(f"===============================\n")
                return True
                
            # Beem requires 255XXXXXXXXX format
            clean_phone = phone.strip().replace('+', '')
            if clean_phone.startswith('0'):
                clean_phone = '255' + clean_phone[1:]
            elif not clean_phone.startswith('255'):
                clean_phone = '255' + clean_phone

            config = settings.BEEM_CONFIG
            api_key = config.get('API_KEY')
            secret_key = config.get('SECRET_KEY')
            sender_id = config.get('SENDER_ID', 'INFO')

            if not api_key or not secret_key:
                logger.warning("Beem API credentials not found. Skipping SMS.")
                return False

            url = "https://api.beem.africa/public/v1/messaging"
            
            payload = {
                "source_addr": sender_id,
                "schedule_time": "",
                "message": message,
                "recipients": [
                    {
                        "recipient_id": 1,
                        "dest_addr": clean_phone
                    }
                ]
            }

            auth_str = f"{api_key}:{secret_key}"
            auth_base64 = base64.b64encode(auth_str.encode()).decode()

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth_base64}"
            }

            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code in [200, 201]:
                logger.info(f"✅ Beem SMS Sent successfully to {phone}")
                return True
            else:
                logger.error(f"❌ Beem SMS Failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending Beem SMS: {str(e)}")
            return False

    @staticmethod
    def send_push(user_id, title, body, data=None):
        """
        Send push notification via OneSignal
        """
        logger.info(f"🔔 SENDING PUSH TO USER {user_id}: {title} - {body}")
        
        # Save to database
        try:
            from apps.core.models import Notification
            Notification.objects.create(
                user_id=user_id,
                title=title,
                body=body,
                data=data
            )
        except Exception as e:
            logger.error(f"Failed to save notification to DB: {str(e)}")

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
                        "data": data
                    }
                    
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
