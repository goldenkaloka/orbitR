from abc import ABC, abstractmethod


class NotifyStrategy(ABC):
    """Pluggable notification strategy for batch dispatch."""

    @abstractmethod
    def notify_restaurant_pending(
        self, batch_id: str, restaurant_id: str, restaurant_owner_id: str, order_count: int
    ) -> None:
        ...

    @abstractmethod
    def notify_batch_dispatched(
        self, batch_id: str, restaurant_id: str, user_ids: list[str]
    ) -> None:
        """Called when driver has been assigned and batch is dispatched."""
        ...
