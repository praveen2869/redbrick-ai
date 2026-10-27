"""
Red Bricks Property Management - Gmail Client
Monitors secondary communication channel for tenant email replies, payment confirmations,
and dispute emails via Google Workspace Gmail API (OAuth 2.0).
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from src.config import settings
from src.models import CommunicationMessage, Channel, MessageDirection
from src.database import db

logger = logging.getLogger("GmailClient")

class GmailClient:
    """
    Client for Gmail API monitoring tenant email replies.
    Operates with read-only OAuth scopes to inspect inbound messages
    sent in response to prior notices or rent invoices.
    """

    def __init__(self, mock_mode: Optional[bool] = None):
        self.mock_mode = settings.GMAIL_MOCK_MODE if mock_mode is None else mock_mode
        self.client_id = settings.GMAIL_CLIENT_ID

    def get_recent_email_replies(self, tenant_id: str, email_address: str, hours: int = 48, reference_time: Optional[datetime] = None) -> List[CommunicationMessage]:
        """
        Retrieves recent email messages from this tenant in the last `hours` hours.
        """
        now = reference_time or datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()

        all_messages = db.get_messages_for_tenant(tenant_id)
        email_messages = [
            m for m in all_messages
            if m.channel == Channel.EMAIL and m.direction == MessageDirection.INBOUND and m.timestamp >= cutoff_iso
        ]
        return email_messages

    def record_incoming_email(self, tenant_id: str, from_email: str, subject: str, body: str) -> CommunicationMessage:
        """
        Records an inbound email from a tenant (via Gmail Pub/Sub or polling).
        """
        msg_id = f"gmail_msg_{int(datetime.utcnow().timestamp() * 1000)}"
        msg = CommunicationMessage(
            id=msg_id,
            tenant_id=tenant_id,
            channel=Channel.EMAIL,
            direction=MessageDirection.INBOUND,
            sender=from_email,
            recipient="rentals@redbrickpm.com",
            content=f"Subject: {subject}\n\n{body}",
            timestamp=datetime.utcnow().isoformat(),
            status="DELIVERED",
            metadata={"source": "Gmail API", "subject": subject}
        )
        db.save_message(msg)
        return msg
