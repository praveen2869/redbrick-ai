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

    def check_grace_period(
        self, record: Optional[RentVineRecord], current_date: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """
        Rule 3: Evaluate grace period for rent dues.

        Logic:
          1. 1st–4th of each month: Do not send reminders for rent due in the current month (Grace period).
          2. Starting on the 5th: Begin sending reminders for current-month rent dues to eligible unpaid tenants.
          3. Overdue rent from previous months: Grace period has expired; eligible for overdue reminders
             subject to payment status, promises, and deduplication rules.
          4. Future rent: Not yet due; reminders are suppressed.
        """
        if not record:
            return False, "No RentVine record on file"

        now = current_date or datetime.utcnow()
        now_date = now.date()

        # Parse due date with multi-format resilience (ISO, human-readable, and multi-month lists)
        due_str = (record.due_date or "").strip()
        parsed_dates = []

        if due_str:
            # 1. Match month name date: e.g. "November 1, 2026" or "October 9, 2026"
            for m in re.finditer(
                r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',
                due_str, re.IGNORECASE
            ):
                try:
                    month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
                    dt = datetime.strptime(f"{month_str} {day_str} {year_str}", "%B %d %Y")
                    parsed_dates.append(dt)
                except Exception:
                    pass

            # 2. Match ISO date: e.g. "2026-11-01" or "2026-10-09"
            for m in re.finditer(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b', due_str):
                try:
                    dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    parsed_dates.append(dt)
                except Exception:
                    pass

            # 3. Match US slashes: e.g. "11/01/2026"
            for m in re.finditer(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', due_str):
                try:
                    dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                    parsed_dates.append(dt)
                except Exception:
                    pass

        if parsed_dates:
            # Use earliest date for grace period checking so prior-month overdue balances are not suppressed
            parsed_dates.sort()
            due_dt = parsed_dates[0]
        else:
            # Default to the 1st of the current month if unstated or standard monthly lease
            due_dt = datetime(now_date.year, now_date.month, 1)

        due_date = due_dt.date()

        # ── Case A: Future rent dues (reminders suppressed) ──
        if (due_date.year, due_date.month) > (now_date.year, now_date.month):
            return (
                True,
                f"Rent is for a future month ({due_date.strftime('%B %Y')}, due on {due_date.strftime('%Y-%m-%d')}) — reminders suppressed."
            )

        if (due_date.year, due_date.month) == (now_date.year, now_date.month) and due_date.day > now_date.day:
            days_until_due = (due_date - now_date).days
            return (
                True,
                f"Rent is not yet due (due on {due_date.strftime('%Y-%m-%d')}, {days_until_due} day(s) away) — reminders suppressed."
            )

        # ── Case B: Current-month rent dues ──
        if (due_date.year, due_date.month) == (now_date.year, now_date.month):
            # Standard monthly rent (due on the 1st or within days 1-4)
            if due_date.day <= 4:
                # 1st–4th of the month serves as the monthly grace period
                if now_date.day <= 4:
                    days_left = 5 - now_date.day
                    return (
                        True,
                        f"Within current-month grace period (Days 1–4 of {now_date.strftime('%B %Y')}). "
                        f"Reminders begin on the 5th ({days_left} day(s) until dispatch begins)."
                    )
                else:
                    return (
                        False,
                        f"Current-month grace period expired on {now_date.strftime('%B')} 4th. "
                        f"Overdue reminder eligible starting on the 5th."
                    )
            else:
                # Mid-month cycle rent (e.g., due on the 15th): 4-day grace window starting on due date
                grace_cutoff = due_date + timedelta(days=3)
                if now_date <= grace_cutoff:
                    days_left = (grace_cutoff - now_date).days + 1
                    return (
                        True,
                        f"Within cycle grace period until {grace_cutoff.strftime('%Y-%m-%d')} ({days_left} day(s) remaining)."
                    )
                else:
                    return (
                        False,
                        f"Cycle grace period expired on {grace_cutoff.strftime('%Y-%m-%d')}."
                    )

        # ── Case C: Overdue rent from PREVIOUS months ──
        # Past-month balances are already overdue and NOT suppressed by the current month's grace period
        return (
            False,
            f"Overdue rent from previous month ({due_date.strftime('%B %Y')}, due on {due_date.strftime('%Y-%m-%d')}) — "
            f"not subject to current-month grace period; eligible for overdue reminder."
        )

    def check_recent_reminder_deduplication(
        self, tenant_id: str, current_time: Optional[datetime] = None
    ) -> Tuple[bool, Optional[WorkflowDecision], str]:
        """
        Rule 4: Deduplication & Cooldown Rule.
        Cooldown is triggered EXCLUSIVELY by workflow-generated rent reminders.
        - When a workflow-generated rent reminder is sent today, put that tenant into cooldown
          for the remainder of that day.
        - The next morning (now.date() > reminder.date()), automatically reactivate the tenant
          for reminder eligibility, provided they still have an outstanding balance.
        - If no workflow-generated reminder was sent, do not activate cooldown.
        """
        now = current_time or datetime.utcnow()
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        last_reminder = db.get_last_successful_reminder(tenant_id)

        if not last_reminder:
            return False, None, "No workflow-generated rent reminders on record"

        try:
            last_ts = datetime.fromisoformat(last_reminder.timestamp.replace("Z", "+00:00"))
            if last_ts.tzinfo is not None:
                last_ts = last_ts.replace(tzinfo=None)
        except Exception:
            return False, None, "Could not parse previous reminder timestamp"

        # Check calendar day: Reminder was sent today?
        if last_ts.date() == now.date():
            elapsed = now - last_ts
            hours_elapsed = max(0.0, elapsed.total_seconds() / 3600.0)
            end_of_day = datetime(now.year, now.month, now.day, 23, 59, 59)
            hours_remaining = max(0.0, (end_of_day - now).total_seconds() / 3600.0)
            return (
                True,
                last_reminder,
                f"Workflow rent reminder sent today ({hours_elapsed:.1f}h ago at {last_ts.strftime('%I:%M %p')}). "
                f"Cooldown active for remainder of day ({hours_remaining:.1f}h remaining until morning reactivation)"
            )
        elif last_ts.date() < now.date():
            # Sent on a previous day -> automatically reactivate the next morning
            return (
                False,
                last_reminder,
                f"Previous workflow reminder sent on {last_ts.strftime('%Y-%m-%d')} (Reactivated for morning reminder eligibility)"
            )
        else:
            # Future timestamp edge case
            return True, last_reminder, "Workflow rent reminder sent today (Cooldown active)"

    def check_manual_review_in_notes(
        self, record: Optional[RentVineRecord], raw_text: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Rule: Check if RentVine Follow-Up Notes (or internal notes) contains 'Manual Review' (case-insensitive).
        If present:
        - Automatically move the tenant to Manual Review / Disputes.
        - Disable automated rent reminders.
        - Keep tracking balances, lease details, and SMS history.
        """
        candidates = []
        if raw_text and raw_text.strip():
            candidates.append(raw_text.strip())
        if record:
            if record.follow_up_notes and record.follow_up_notes.strip():
                candidates.append(record.follow_up_notes.strip())
            if record.notes:
                for n in record.notes:
                    if n.text and n.text.strip():
                        candidates.append(n.text.strip())

        for text in candidates:
            if "manual review" in text.lower():
                return True, "Follow-Up Notes contains 'Manual Review' in RentVine — automated reminders disabled for staff review"

        return False, None

    def check_existing_promise_in_notes(
        self, record: Optional[RentVineRecord], current_time: Optional[datetime] = None, raw_follow_up: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Rule 5: Check RentVine notes and Follow-Up Notes for a logged payment promise that hasn't expired.
        Supports:
        - [AI PROMISE ON FILE: Will pay by YYYY-MM-DD]
        - Standard notes: will pay by YYYY-MM-DD
        - Follow-Up Notes date commitments: MM/DD/YYYY - ...
        - Promises like: Rent will be paid by MM/DD
        """
        candidates = []
        if raw_follow_up and raw_follow_up.strip():
            candidates.append((raw_follow_up.strip(), "RentVine Follow-Up Notes"))
        if record:
            if record.follow_up_notes and record.follow_up_notes.strip():
                candidates.append((record.follow_up_notes.strip(), "RentVine Follow-Up Notes"))
            if record.notes:
                for note in record.notes:
                    if note.text and note.text.strip():
                        candidates.append((note.text.strip(), f"Note by {note.author}"))

        if not candidates:
            return False, None, "No active payment promise recorded in notes"

        now = (current_time or datetime.utcnow()).date()
        date_iso_pattern = re.compile(r"will pay by\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
        date_us_pattern = re.compile(r"(?:will (?:be )?paid by|pay by|promise(?:d)? by)\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.IGNORECASE)
        leading_date_pattern = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\s*-\s*", re.IGNORECASE)

        for text, source in candidates:
            # 1. ISO format: will pay by YYYY-MM-DD
            m_iso = date_iso_pattern.search(text)
            if m_iso:
                promise_date_str = m_iso.group(1)
                try:
                    p_date = datetime.strptime(promise_date_str, "%Y-%m-%d").date()
                    if p_date >= now:
                        return True, promise_date_str, f"Active promise on file in {source} to pay by {promise_date_str}"
                    else:
                        return False, promise_date_str, f"Prior payment promise expired on {promise_date_str}"
                except Exception:
                    pass

            # 2. Leading date in Follow-Up Notes: e.g. "10/31/2026 - Owner-approved extension..."
            m_lead = leading_date_pattern.search(text)
            if m_lead:
                lead_str = m_lead.group(1)
                try:
                    p_date = datetime.strptime(lead_str, "%m/%d/%Y").date()
                    p_iso = p_date.strftime("%Y-%m-%d")
                    # Only hold if the text actually references a hold, promise, extension, or recheck
                    text_lower = text.lower()
                    if any(kw in text_lower for kw in ("hold", "extension", "promise", "plans", "pay remainder", "resubmit")):
                        if p_date >= now:
                            return True, p_iso, f"Active payment commitment/extension in {source} until {p_iso}"
                        else:
                            return False, p_iso, f"Prior payment follow-up deadline expired on {p_iso}"
                except Exception:
                    pass

            # 3. US date format: will be paid by M/D or M/D/YYYY
            m_us = date_us_pattern.search(text)
            if m_us:
                us_str = m_us.group(1)
                try:
                    parts = us_str.split("/")
                    month = int(parts[0])
                    day = int(parts[1])
                    year = int(parts[2]) if len(parts) > 2 else now.year
                    if year < 100:
                        year += 2000
                    p_date = datetime(year, month, day).date()
                    p_iso = p_date.strftime("%Y-%m-%d")
                    if p_date >= now:
                        return True, p_iso, f"Active promise on file in {source} to pay by {p_iso}"
                    else:
                        return False, p_iso, f"Prior payment promise expired on {p_iso}"
                except Exception:
                    pass

        return False, None, "No active payment promise recorded in notes"
