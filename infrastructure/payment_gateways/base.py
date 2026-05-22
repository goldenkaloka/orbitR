from abc import ABC, abstractmethod
from typing import List, Dict, Any

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
    def split_settlement(self, airpay_id: str, rrn: str, splits: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Release escrowed funds via split settlement.
        Sends the full transaction amount to the platform's primary account.
        Internal distribution to vendors/drivers is handled via wallet cashouts.
        """
        pass
