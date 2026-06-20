"""
Handlers — delegates to domain-specific handler modules.

Each sub-module exports a register_handlers() function that subscribes
its handlers to the EventBus. This file coordinates them all.
"""

import logging

logger = logging.getLogger(__name__)


def register_all_handlers():
    from . import (
        auth_handlers,
        dispute_handlers,
        order_handlers,
        payment_handlers,
        pool_handlers,
    )

    order_handlers.register_handlers()
    payment_handlers.register_handlers()
    pool_handlers.register_handlers()
    dispute_handlers.register_handlers()
    auth_handlers.register_handlers()

    logger.info(f"All event handlers registered (%d modules)", 5)
