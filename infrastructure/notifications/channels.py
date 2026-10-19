"""
Pluggable notification channels.

Each channel implements the NotificationChannel ABC, allowing push, SMS,
WebSocket, and admin broadcasts to be swapped independently.
"""

from abc import ABC, abstractmethod
from typing import Any


class NotificationChannel(ABC):
    @abstractmethod
    def send_push(self, user_id: str, title: str, body: str, data: dict | None = None) -> None:
        ...

    @abstractmethod
    def send_sms(self, phone: str, message: str) -> None:
        ...

    @abstractmethod
    def notify_admins(self, title: str, body: str, data: dict | None = None, sms: bool = False) -> None:
        ...


class PushNotificationChannel(NotificationChannel):
    """Standard push + SMS via OneSignal / SMS gateway."""

    def send_push(self, user_id: str, title: str, body: str, data: dict | None = None) -> None:
        from infrastructure.notifications.service import NotificationService
        NotificationService.send_push(user_id=user_id, title=title, body=body, data=data or {})

    def send_sms(self, phone: str, message: str) -> None:
        from infrastructure.notifications.service import NotificationService
        NotificationService.send_sms(phone, message)

    def notify_admins(self, title: str, body: str, data: dict | None = None, sms: bool = False) -> None:
        from infrastructure.notifications.service import NotificationService
        NotificationService.notify_admins(title=title, body=body, data=data or {}, sms=sms)


class LogOnlyChannel(NotificationChannel):
    """Logs all notifications without sending — useful for tests or dry-run."""

    def send_push(self, user_id: str, title: str, body: str, data: dict | None = None) -> None:
        import logging
        logging.getLogger(__name__).info(f"[LogOnly] push to {user_id}: {title} — {body}")

    def send_sms(self, phone: str, message: str) -> None:
        import logging
        logging.getLogger(__name__).info(f"[LogOnly] sms to {phone}: {message}")

    def notify_admins(self, title: str, body: str, data: dict | None = None, sms: bool = False) -> None:
        import logging
        logging.getLogger(__name__).info(f"[LogOnly] admin alert: {title} — {body}")


# Module-level default — swap this at startup for different environments
default_channel: NotificationChannel = PushNotificationChannel()
