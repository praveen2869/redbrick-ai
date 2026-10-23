"""
Red Bricks Property Management - Retry Manager
Manages failed reminder dispatches with exponential backoff and up to 3 retry attempts.
"""

import logging
from typing import List, Dict, Any
from src.database import db
from src.clients.quo_sms import QuoSMSClient
from src.models import RetryQueueItem

logger = logging.getLogger("RetryManager")

class RetryManager:
    def __init__(self, sms_client: QuoSMSClient):
        self.sms_client = sms_client

    def process_pending_retries(self) -> Dict[str, Any]:
        """
        Pulls items due for retry from the retry_queue table, attempts re-dispatch,
        and marks them as SUCCESS or schedules next backoff / FAILED.
        """
        pending_items: List[RetryQueueItem] = db.get_pending_retries()
        results = {
            "processed": len(pending_items),
            "succeeded": 0,
            "failed": 0
        }

        for item in pending_items:
            payload = item.payload
            tenant_id = item.tenant_id
            to_number = payload.get("to_number")
            text = payload.get("text")

            try:
                self.sms_client.send_sms(tenant_id, to_number, text)
                db.update_retry_item(item.id, "SUCCESS")
                results["succeeded"] += 1
                logger.info(f"Retry successful for tenant {tenant_id} on attempt {item.attempt_count + 1}")
            except Exception as e:
                err_msg = str(e)
                db.update_retry_item(item.id, "FAILED", err_msg)
                results["failed"] += 1
                logger.warning(f"Retry attempt {item.attempt_count + 1} failed for tenant {tenant_id}: {err_msg}")

        return results
