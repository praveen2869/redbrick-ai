"""
Red Bricks Property Management - Domain Models
Defines all data structures for RentVine, Quo SMS, Gmail, AI decisions, and workflow audit logs.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any

class Channel(str, Enum):
    SMS = "SMS"
    EMAIL = "EMAIL"
    PORTAL = "PORTAL"

class MessageDirection(str, Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"

class IntentType(str, Enum):
    PROMISE_MADE = "PROMISE_MADE"
    ALREADY_PAID = "ALREADY_PAID"
    DISPUTE = "DISPUTE"
    NO_REPLY = "NO_REPLY"
    GENERAL_INQUIRY = "GENERAL_INQUIRY"

class DecisionOutcome(str, Enum):
    SKIP_PAID = "SKIP_PAID"
    HOLD_PROMISE = "HOLD_PROMISE"
    SEND_REMINDER = "SEND_REMINDER"
    SKIP_ALREADY_REMINDED = "SKIP_ALREADY_REMINDED"
    SKIP_GRACE_PERIOD = "SKIP_GRACE_PERIOD"
    FLAG_MANUAL_REVIEW = "FLAG_MANUAL_REVIEW"

@dataclass
class RentVineNote:
    id: str
    created_at: str
    author: str
    text: str
    is_automated: bool = True

@dataclass
class RentVineRecord:
    tenant_id: str
    balance: float
    monthly_rent: float
    due_date: str                 # YYYY-MM-DD
    last_payment_date: Optional[str] = None
    last_payment_amount: float = 0.0
    notes: List[RentVineNote] = field(default_factory=list)
    portal_link: str = "https://redbrick.rentvine.com/portal/pay"
    lease_status: str = "ACTIVE"

@dataclass
class Tenant:
    id: str
    first_name: str
    last_name: str
    phone: str
    email: str
    property_name: str
    unit_number: str
    is_excluded: bool = False
    exclusion_reason: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

@dataclass
class CommunicationMessage:
    id: str
    tenant_id: str
    channel: Channel
    direction: MessageDirection
    sender: str
    recipient: str
    content: str
    timestamp: str                # ISO 8601
    status: str = "DELIVERED"     # DELIVERED, PENDING, FAILED
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ClassificationResult:
    intent: IntentType
    confidence: float
    raw_text: str
    source_channel: Channel
    extracted_date: Optional[str] = None
    extracted_amount: Optional[float] = None
    reasoning: str = ""

@dataclass
class WorkflowDecision:
    tenant_id: str
    tenant_name: str
    property_unit: str
    balance: float
    outcome: DecisionOutcome
    reason: str
    details: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    sms_sent: bool = False
    sms_message_id: Optional[str] = None
    note_written: bool = False
    run_id: Optional[str] = None

@dataclass
class RetryQueueItem:
    id: int
    tenant_id: str
    payload: Dict[str, Any]
    attempt_count: int
    max_attempts: int
    last_error: str
    next_retry_at: str
    status: str  # PENDING, SUCCESS, FAILED

@dataclass
class DailyRunSummary:
    run_id: str
    started_at: str
    completed_at: str
    is_dry_run: bool
    total_tenants: int
    count_paid: int
    count_promised: int
    count_reminded: int
    count_skipped: int
    count_flagged: int
    count_failed: int
    total_balance_unpaid: float
    decisions: List[WorkflowDecision] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d
