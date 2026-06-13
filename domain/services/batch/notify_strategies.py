"""
Concrete notification strategies for batch dispatch.
"""

import logging

from domain.services.batch.interfaces import NotifyStrategy
from infrastructure.notifications.channels import NotificationChannel, default_channel
from infrastructure.notifications.messages import vendor_need_driver

logger = logging.getLogger(__name__)


class PushNotifyStrategy(NotifyStrategy):
    """
    Current default — push notification to restaurant owner only.
    """

    def __init__(self, channel: NotificationChannel | None = None):
        self.channel = channel or default_channel

    def notify_restaurant_pending(
        self, batch_id: str, restaurant_id: str, restaurant_owner_id: str, order_count: int
    ) -> None:
        title, body = vendor_need_driver(order_count)
        self.channel.send_push(
            user_id=restaurant_owner_id,
            title=title,
            body=body,
            data={
                "batch_id": batch_id,
                "restaurant_id": restaurant_id,
                "type": "batch_needs_driver",
            },
        )

    def notify_batch_dispatched(
        self, batch_id: str, restaurant_id: str, user_ids: list[str]
    ) -> None:
        from infrastructure.notifications.messages import batch_dispatched_notification
        title, body = batch_dispatched_notification()
        for uid in user_ids:
            self.channel.send_push(
                user_id=uid,
                title=title,
                body=body,
                data={
                    "batch_id": batch_id,
                    "restaurant_id": restaurant_id,
                    "type": "batch_dispatched",
                },
            )


class LogOnlyNotifyStrategy(NotifyStrategy):
    """Logs notifications only — for testing or dry-run."""

    def notify_restaurant_pending(
        self, batch_id: str, restaurant_id: str, restaurant_owner_id: str, order_count: int
    ) -> None:
        logger.info(
            f"[LogOnly] Batch {batch_id} needs driver — "
            f"would notify owner {restaurant_owner_id} of restaurant {restaurant_id} "
            f"({order_count} orders)"
        )

    def notify_batch_dispatched(
        self, batch_id: str, restaurant_id: str, user_ids: list[str]
    ) -> None:
        logger.info(
            f"[LogOnly] Batch {batch_id} dispatched — "
            f"would notify users {user_ids}"
        )


# Module-level default — swap at startup for different dispatch modes
default_strategy: NotifyStrategy = PushNotifyStrategy()
