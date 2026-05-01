import logging
import threading

logger = logging.getLogger(__name__)

class NotificationService:
    @staticmethod
    def send_sms(phone, message):
        """
        Send SMS via Africa's Talking
        """
        logger.info(f"📲 SENDING SMS TO {phone} via Africa's Talking: {message}")
        
        try:
            from django.conf import settings
            
            if getattr(settings, 'MOCK_SMS', True):
                print(f"\n======== MOCK SMS GATEWAY ========")
                print(f"To: {phone}")
                print(f"Message: {message}")
                print(f"==================================\n")
                return True
                
            import requests
            
            # Format phone to include + (e.g., +255XXXXXXXXX)
            if phone.startswith('0'):
                phone = '+255' + phone[1:]
            elif not phone.startswith('+'):
                phone = '+' + phone

            username = getattr(settings, 'AFRICASTALKING_USERNAME', 'sandbox')
            api_key = getattr(settings, 'AFRICASTALKING_API_KEY', '')
            sender_id = getattr(settings, 'AFRICASTALKING_SENDER_ID', '')

            if not api_key:
                logger.warning("Africa's Talking API Key not found. Skipping real SMS.")
                return False

            if username == 'sandbox':
                url = "https://api.sandbox.africastalking.com/version1/messaging/bulk"
            else:
                url = "https://api.africastalking.com/version1/messaging/bulk"

            payload = {
                "username": username,
                "to": phone, # In Africa's Talking JSON payload it's "phoneNumbers" instead of "to" based on curl example wait let's use phoneNumbers
                "phoneNumbers": [phone],
                "message": message,
            }
            if sender_id:
                payload["senderId"] = sender_id

            headers = {
                "apiKey": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code in [200, 201]:
                logger.info(f"✅ SMS Sent successfully to {phone}")
                return True
            else:
                logger.error(f"❌ SMS Failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending SMS: {str(e)}")
            return False

    @staticmethod
    def send_push(user_id, title, body, data=None):
        """
        Send push notification via OneSignal and save to database
        """
        logger.info(f"🔔 SENDING PUSH TO USER {user_id}: {title} - {body}")
        
        # Save to database first
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
            import requests
            from django.conf import settings
            from apps.users.models import User
            
            user = User.objects.get(id=user_id)
            if not user.onesignal_id:
                logger.warning(f"User {user_id} has no onesignal_id. Skipping push notification.")
                return False

            def _send():
                try:
                    payload = {
                        "app_id": getattr(settings, 'ONESIGNAL_APP_ID', ''),
                        "include_player_ids": [user.onesignal_id],
                        "headings": {"en": title},
                        "contents": {"en": body},
                        "data": data
                    }
                    
                    headers = {
                        "Authorization": f"Basic {getattr(settings, 'ONESIGNAL_REST_API_KEY', '')}",
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

            # Fire in background thread — never block the request
            thread = threading.Thread(target=_send, daemon=True)
            thread.start()
            return True
                
        except Exception as e:
            logger.error(f"Error sending OneSignal push: {str(e)}")
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
