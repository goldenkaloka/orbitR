import logging
import threading

logger = logging.getLogger(__name__)

class NotificationService:
    @staticmethod
    def send_sms(phone, message):
        """
        Send SMS via Infobip API
        """
        logger.info(f"📲 SENDING SMS TO {phone} via Infobip: {message}")
        
        try:
            from django.conf import settings
            
            if getattr(settings, 'MOCK_SMS', True):
                print(f"\n======== MOCK SMS GATEWAY ========")
                print(f"To: {phone}")
                print(f"Message: {message}")
                print(f"==================================\n")
                return True
                
            import requests
            
            # Format phone to E.164 without the '+' sign (e.g., 255XXXXXXXXX)
            if phone.startswith('0'):
                phone = '255' + phone[1:]
            elif phone.startswith('+'):
                phone = phone[1:]
            elif not phone.startswith('255') and len(phone) == 9:
                phone = '255' + phone

            api_key = getattr(settings, 'INFOBIP_API_KEY', '')
            base_url = getattr(settings, 'INFOBIP_BASE_URL', '')
            sender_id = getattr(settings, 'INFOBIP_SENDER_ID', 'InfoSMS')

            if not api_key or not base_url:
                logger.warning("Infobip credentials not found. Skipping real SMS.")
                return False

            url = f"https://{base_url}/sms/2/text/advanced"

            payload = {
                "messages": [
                    {
                        "destinations": [{"to": phone}],
                        "from": sender_id,
                        "text": message
                    }
                ]
            }

            headers = {
                "Authorization": f"App {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers, timeout=5)
            
            if response.status_code in [200, 202]:
                logger.info(f"✅ SMS Sent successfully to {phone} via Infobip")
                return True
            else:
                logger.error(f"❌ Infobip SMS Failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Error sending Infobip SMS: {str(e)}")
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
