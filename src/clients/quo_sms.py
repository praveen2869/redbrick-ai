"""
Red Bricks Property Management - Quo (OpenPhone) SMS Client
Integrates with Quo REST API (https://api.openphone.com/v1) for sending SMS reminders
and reading tenant conversation history.
"""

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from src.config import settings
from src.models import CommunicationMessage, Channel, MessageDirection
from src.database import db

logger = logging.getLogger("QuoSMS")

class QuoSMSClient:
    """
    Client for Quo (formerly OpenPhone) REST API.
    Handles programmatic delivery of SMS reminders and retrieves conversation history
    for the AI rules engine to detect payment promises.
    """

    def __init__(self, api_url: Optional[str] = None, api_key: Optional[str] = None, mock_mode: Optional[bool] = None):
        self.api_url = (api_url or settings.QUO_API_URL).rstrip("/")
        self.api_key = api_key or settings.QUO_API_KEY
        self.phone_number_id = settings.QUO_DEFAULT_PHONE_NUMBER_ID
        self.mock_mode = settings.QUO_MOCK_MODE if mock_mode is None else mock_mode

    def send_sms(self, tenant_id: str, to_number: str, text: str, force_failure: bool = False) -> Dict[str, Any]:
        """
        Dispatches an outbound SMS reminder to the tenant via Quo.
        """
        msg_id = f"quo_msg_{int(datetime.utcnow().timestamp() * 1000)}"
        now_iso = datetime.utcnow().isoformat()

        if force_failure:
            logger.error(f"Quo SMS simulated API failure sending to {to_number}")
            raise RuntimeError("Quo API error 503: Carrier gateway temporary timeout")

        # TESTING PHASE SAFEGUARD: Never transmit live SMS to real phone numbers
        # Always suppress external carrier dispatch and record reminder as simulated/needed
        logger.info(f"[TESTING PHASE - SMS SUPPRESSED] Reminder needed for {to_number} ({tenant_id}): \"{text}\"")

        # Record message in persistent audit log with TESTING status
        outbound = CommunicationMessage(
            id=msg_id,
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.OUTBOUND,
            sender="+1 (667) 281-3879",
            recipient=to_number,
            content=text,
            timestamp=now_iso,
            status="REMINDER_NEEDED (TESTING_MODE)",
            metadata={
                "source": "Quo REST API",
                "provider": "OpenPhone",
                "testing_mode": True,
                "sms_dispatched": False,
                "note": "Outbound SMS transmission suppressed during testing phase"
            }
        )
        db.save_message(outbound)

        return {
            "success": True,
            "message_id": msg_id,
            "timestamp": now_iso,
            "recipient": to_number,
            "status": "REMINDER_NEEDED_SUPPRESSED"
        }

    def get_conversation_history(self, tenant_id: str, phone_number: str, days: int = 7, reference_time: Optional[datetime] = None) -> List[CommunicationMessage]:
        """
        Retrieves the last `days` days of SMS conversation with this tenant.
        """
        messages = db.get_messages_for_tenant(tenant_id)
        # Filter to SMS and within time window
        now = reference_time or datetime.utcnow()
        cutoff = now - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        sms_history = [
            m for m in messages
            if m.channel == Channel.SMS and m.timestamp >= cutoff_iso
        ]
        return sms_history

    def receive_inbound_sms(self, tenant_id: str, from_number: str, text: str) -> CommunicationMessage:
        """
        Simulates / records an inbound SMS response from a tenant via Quo webhook.
        """
        msg_id = f"quo_in_{int(datetime.utcnow().timestamp() * 1000)}"
        msg = CommunicationMessage(
            id=msg_id,
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.INBOUND,
            sender=from_number,
            recipient="+1-800-REDBRICKS",
            content=text,
            timestamp=datetime.utcnow().isoformat(),
            status="DELIVERED",
            metadata={"webhook": "quo.incoming.sms"}
        )
        db.save_message(msg)
        return msg
