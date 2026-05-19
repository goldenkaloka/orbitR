"""
DEPRECATED — BatchingService is no longer active.

This file is kept only for migration safety. All batching logic has been
consolidated into BatchPipeline (domain/services/batch_pipeline.py).

Problems with this service that led to its deprecation:
  - Created batches with no pricing, no routing, and no dispatch call.
  - Orphan batches sat in CREATED status permanently.
  - Competed with BatchingEngine creating double-batch race conditions.

DO NOT add new callers. Use BatchPipeline.run(order) instead.
"""
import logging

logger = logging.getLogger(__name__)


class BatchingService:
    """
    DEPRECATED. Redirects to BatchPipeline for backwards compatibility.
    Will be removed in a future cleanup pass.
    """

    @staticmethod
    def attempt_batching(restaurant_id, h3_index):
        logger.warning(
            "[BatchingService] DEPRECATED — BatchingService.attempt_batching() called. "
            "This service no longer creates batches. Use BatchPipeline.run(order) instead. "
            f"restaurant_id={restaurant_id}, h3_index={h3_index}"
        )
        return None

