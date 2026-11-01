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

def normalize_phone_e164(phone: Optional[str]) -> Optional[str]:
    """Normalizes phone numbers like (862) 220-5006 or 862-220-5006 to E.164 (+18622205006)."""
    if not phone:
        return None
    digits = "".join([c for c in str(phone) if c.isdigit()])
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    elif len(digits) > 7:
        return f"+{digits}"
    return None

def phone_to_10digit(phone: Optional[str]) -> Optional[str]:
    """Extracts trailing 10 digits for invariant matching."""
    if not phone:
        return None
    digits = "".join([c for c in str(phone) if c.isdigit()])
    return digits[-10:] if len(digits) >= 10 else (digits if digits else None)

class QuoApiError(Exception):
    """Raised when Quo/OpenPhone API retrieval fails."""
    pass

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

    def send_sms(
        self,
        tenant_id: str,
        to_number: str,
        text: str,
        recipient_type: str = "tenant",
        sender_phone_number_id: Optional[str] = None,
        force_failure: bool = False
    ) -> Dict[str, Any]:
        """
        Dispatches an outbound SMS reminder via Quo using the appropriate sender phone line:
          - Tenants & Vendors: (980) 300-7559 [PNnMjdTFVO] for tenant rent reminders & follow-ups
          - Landlords: (980) 300-1196 [PNTDARdwBh] for landlord communications & updates
          - Primary: (667) 281-3879 [PNz3t43PKv] isolated general line
        """
        msg_id = f"quo_msg_{int(datetime.utcnow().timestamp() * 1000)}"
        now_iso = datetime.utcnow().isoformat()

        if force_failure:
            logger.error(f"Quo SMS simulated API failure sending to {to_number}")
            raise RuntimeError("Quo API error 503: Carrier gateway temporary timeout")

        # Resolve correct sender phone number and inbox based on recipient type
        if sender_phone_number_id:
            if sender_phone_number_id == settings.QUO_PHONE_LANDLORDS_ID:
                phone_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender("landlord")
            elif sender_phone_number_id == settings.QUO_PHONE_PRIMARY_ID:
                phone_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender("primary")
            else:
                phone_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender("tenant")
        else:
            phone_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender(recipient_type)

        # TESTING PHASE SAFEGUARD: Never transmit live SMS to real phone numbers
        # Always suppress external carrier dispatch and record reminder as simulated/needed
        logger.info(
            f"[TESTING PHASE - SMS SUPPRESSED] [{inbox_name} | {sender_fmt}] "
            f"Reminder needed for {to_number} ({tenant_id}): \"{text}\""
        )

        # Record message in persistent audit log with TESTING status and correct sender
        outbound = CommunicationMessage(
            id=msg_id,
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.OUTBOUND,
            sender=sender_fmt,
            recipient=to_number,
            content=text,
            timestamp=now_iso,
            status="REMINDER_NEEDED (TESTING_MODE)",
            metadata={
                "source": "Quo REST API",
                "provider": "OpenPhone",
                "inbox": inbox_name,
                "sender_number": sender_fmt,
                "sender_e164": sender_e164,
                "sender_phone_id": phone_id,
                "recipient_type": recipient_type,
                "testing_mode": True,
                "sms_dispatched": False,
                "note": f"Outbound SMS transmission suppressed during testing phase (Sender: {sender_fmt} - {inbox_name})"
            }
        )
        db.save_message(outbound)

        return {
            "success": True,
            "message_id": msg_id,
            "timestamp": now_iso,
            "recipient": to_number,
            "sender": sender_fmt,
            "sender_phone_id": phone_id,
            "inbox": inbox_name,
            "status": "REMINDER_NEEDED_SUPPRESSED"
        }


    def get_conversation_history(
        self,
        tenant_id: str,
        phone_number: str,
        days: int = 7,
        reference_time: Optional[datetime] = None,
        phone_number_id: Optional[str] = None
    ) -> List[CommunicationMessage]:
        """
        Retrieves the last `days` days of SMS conversation with this tenant.
        When live mode is enabled, queries OpenPhone REST API for participant messages,
        persists them to the local database, and returns the chronological history.
        """
        # Fetch live OpenPhone messages when live mode is active
        if not self.mock_mode and self.api_key and phone_number:
            e164 = normalize_phone_e164(phone_number)
            if e164:
                import urllib.parse
                auth = self.api_key if self.api_key.startswith("Bearer ") else f"Bearer {self.api_key}"
                headers = {
                    "Authorization": auth,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                target_pid = phone_number_id or settings.QUO_PHONE_TENANTS_ID
                encoded_p = urllib.parse.quote(e164)
                url = f"{self.api_url}/messages?phoneNumberId={target_pid}&participants[]={encoded_p}&maxResults=20"
                
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        raw_msgs = data.get("data", []) or []
                        for rm in raw_msgs:
                            msg_id = rm.get("id") or f"openphone_{int(datetime.utcnow().timestamp()*1000)}"
                            is_inc = (rm.get("direction") == "incoming")
                            content = rm.get("text") or rm.get("body") or ""
                            created_ts = rm.get("createdAt") or datetime.utcnow().isoformat()
                            status = (rm.get("status") or "delivered").upper()
                            sender_val = rm.get("from") or ""
                            to_list = rm.get("to") or []
                            recipient_val = to_list[0] if to_list else ""
                            
                            c_msg = CommunicationMessage(
                                id=msg_id,
                                tenant_id=tenant_id,
                                channel=Channel.SMS,
                                direction=MessageDirection.INBOUND if is_inc else MessageDirection.OUTBOUND,
                                sender=sender_val,
                                recipient=recipient_val,
                                content=content,
                                timestamp=created_ts,
                                status=status,
                                metadata={
                                    "source": "OpenPhone REST API",
                                    "phoneNumberId": rm.get("phoneNumberId"),
                                    "conversationId": rm.get("conversationId"),
                                    "userId": rm.get("userId")
                                }
                            )
                            db.save_message(c_msg)
                except urllib.error.HTTPError as he:
                    logger.error(f"[Quo] HTTP error {he.code} fetching messages for {e164}: {he}")
                    raise QuoApiError(f"Quo API error {he.code}: {he}")
                except Exception as e:
                    logger.error(f"[Quo] Network error fetching messages for {e164}: {e}")
                    raise QuoApiError(f"Quo API retrieval failed: {e}")

        # Retrieve messages for this tenant or matching phone
        p10 = phone_to_10digit(phone_number)
        messages = db.get_messages_for_tenant_or_phone(tenant_id, p10)

        # Filter to SMS and within time window
        now = reference_time or datetime.utcnow()
        cutoff = now - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        sms_history = [
            m for m in messages
            if m.channel == Channel.SMS and m.timestamp >= cutoff_iso
        ]
        # Sort chronologically
        sms_history.sort(key=lambda m: m.timestamp)
        return sms_history

    def receive_inbound_sms(self, tenant_id: str, from_number: str, text: str, recipient_type: str = "tenant") -> CommunicationMessage:
        """
        Simulates / records an inbound SMS response from a tenant or landlord via Quo webhook.
        """
        _, _, sender_fmt, inbox_name = settings.get_quo_sender(recipient_type)
        msg_id = f"quo_in_{int(datetime.utcnow().timestamp() * 1000)}"
        msg = CommunicationMessage(
            id=msg_id,
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.INBOUND,
            sender=from_number,
            recipient=sender_fmt,
            content=text,
            timestamp=datetime.utcnow().isoformat(),
            status="DELIVERED",
            metadata={"webhook": "quo.incoming.sms", "inbox": inbox_name, "recipient_line": sender_fmt}
        )
        db.save_message(msg)
        return msg
