"""
Red Bricks Property Management - Business Rules Engine (Deterministic)
Evaluates rental balances, grace periods, deduplication cooldowns, exclusion lists,
and active payment promise windows.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
from src.config import settings
from src.models import (
    Tenant, RentVineRecord, DecisionOutcome, WorkflowDecision
)
from src.database import db

logger = logging.getLogger("RulesEngine")

class BusinessRulesEngine:
    def __init__(
        self,
        grace_period_days: Optional[int] = None,
        dedup_window_hours: Optional[int] = None,
        promise_expiry_days: Optional[int] = None
    ):
        self.grace_period_days = grace_period_days or settings.GRACE_PERIOD_DAYS
        self.dedup_window_hours = dedup_window_hours or settings.DEDUPLICATION_WINDOW_HOURS
        self.promise_expiry_days = promise_expiry_days or settings.PROMISE_EXPIRY_DAYS

    def check_paid_status(self, record: Optional[RentVineRecord]) -> Tuple[bool, str]:
        """
        Rule 1: If balance <= $0.00, rent is paid.
        Any positive outstanding balance (even $1.00) is considered unpaid and eligible for reminder.
        """
        if not record:
            return False, "No RentVine record found"

        if record.balance <= 0.0:
            return True, f"Rent balance is $0.00 (Last payment: ${record.last_payment_amount:,.2f} on {record.last_payment_date})"

        return False, f"Outstanding balance: ${record.balance:,.2f}"

    def check_exclusion_list(self, tenant: Tenant) -> Tuple[bool, Optional[str]]:
        """
        Rule 2: Tenant flagged for manual handling or special legal arrangement.
        """
        if tenant.is_excluded:
            reason = tenant.exclusion_reason or "Account flagged for manual property manager handling"
            return True, reason
        return False, None

    def check_grace_period(self, record: Optional[RentVineRecord], current_date: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        Rule 3: Check if the tenant is still within the monthly grace period.
        E.g., Due on Oct 1 + 3 days grace period = reminders only sent from Oct 4 onward.
        """
        if not record or not record.due_date:
            return False, "No due date on record"

        now = current_date or datetime.utcnow()
        try:
            due_dt = datetime.strptime(record.due_date, "%Y-%m-%d")
        except Exception:
            return False, "Invalid due date format"

        grace_cutoff = due_dt + timedelta(days=self.grace_period_days)

        if now.date() <= grace_cutoff.date():
            days_left = (grace_cutoff.date() - now.date()).days
            return True, f"Within grace period until {grace_cutoff.strftime('%Y-%m-%d')} ({days_left} days remaining)"

        return False, f"Grace period expired on {grace_cutoff.strftime('%Y-%m-%d')}"

    def check_recent_reminder_deduplication(
        self, tenant_id: str, current_time: Optional[datetime] = None
    ) -> Tuple[bool, Optional[WorkflowDecision], str]:
        """
        Rule 4: Prevent duplicate SMS reminders sent within the deduplication window (e.g. 24 hours).
        """
        now = current_time or datetime.utcnow()
        last_reminder = db.get_last_successful_reminder(tenant_id)

        if not last_reminder:
            return False, None, "No previous reminders on record"

        try:
            last_ts = datetime.fromisoformat(last_reminder.timestamp)
        except Exception:
            return False, None, "Could not parse previous timestamp"

        elapsed = now - last_ts
        hours_elapsed = abs(elapsed.total_seconds()) / 3600.0

        if hours_elapsed < self.dedup_window_hours:
            remaining_hours = self.dedup_window_hours - hours_elapsed
            return (
                True,
                last_reminder,
                f"Reminder already sent {hours_elapsed:.1f}h ago (Deduplication lock active for {remaining_hours:.1f}h)"
            )

        return False, last_reminder, f"Last reminder was {hours_elapsed:.1f}h ago (outside 24h window)"

    def check_existing_promise_in_notes(
        self, record: Optional[RentVineRecord], current_time: Optional[datetime] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Rule 5: Check RentVine notes for a logged payment promise that hasn't expired.
        Format in notes: [AI PROMISE ON FILE: Will pay by YYYY-MM-DD]
        """
        if not record or not record.notes:
            return False, None, "No active payment promise recorded in notes"

        now = (current_time or datetime.utcnow()).date()
        date_pattern = re.compile(r"will pay by\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)

        for note in record.notes:
            match = date_pattern.search(note.text)
            if match:
                promise_date_str = match.group(1)
                try:
                    p_date = datetime.strptime(promise_date_str, "%Y-%m-%d").date()
                    if p_date >= now:
                        return (
                            True,
                            promise_date_str,
                            f"Active promise on file in RentVine notes to pay by {promise_date_str} (Author: {note.author})"
                        )
                    else:
                        return (
                            False,
                            promise_date_str,
                            f"Prior payment promise expired on {promise_date_str}"
                        )
                except Exception:
                    continue

        return False, None, "No active payment promise recorded in notes"
