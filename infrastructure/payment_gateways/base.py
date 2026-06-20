from abc import ABC, abstractmethod
from typing import Any, Dict, List


class PaymentGateway(ABC):
    @abstractmethod
    def collect(self, amount, phone, order_reference, **kwargs):
        """Initiate payment collection from customer"""
        pass

    @abstractmethod
    def disburse(self, amount, account_info, reference, **kwargs):
        """Disburse funds to a driver or restaurant"""
        pass

    @abstractmethod
    def get_status(self, provider_reference):
        """Query status of a transaction"""
        pass

    @abstractmethod
    def split_settlement(self, airpay_id: str, rrn: str, *args, **kwargs) -> Dict[str, Any]:
        """
        Release escrowed funds via split settlement.
        """
        pass
