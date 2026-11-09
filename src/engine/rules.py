"""
Red Bricks Property Management - Business Rules Engine (Deterministic)
Evaluates rental balances, grace periods, deduplication cooldowns, exclusion lists,
and active payment promise windows.
"""

import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple, Dict, Any
from src.config import settings
from src.models import (
    Tenant, RentVineRecord, DecisionOutcome, WorkflowDecision
)
from src.database import db

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None

logger = logging.getLogger("RulesEngine")

def get_pm_datetime(current_time: Optional[datetime] = None) -> datetime:
    """
    Returns datetime localized to the property management timezone (default America/New_York).
    If current_time is None, returns current time in PM timezone.
    If naive datetime is provided, returns it directly to honor reference test clocks.
    """
    tz_name = getattr(settings, "PROPERTY_TIMEZONE", "America/New_York")
    tz = None
    if ZoneInfo:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    if not tz:
        from datetime import timezone as _tz, timedelta as _td
        tz = _tz(_td(hours=-5))

    if current_time is None:
        from datetime import timezone as _tz
        return datetime.now(_tz.utc).astimezone(tz)

    if current_time.tzinfo is not None:
        return current_time.astimezone(tz)

    return current_time


class ReminderStage(str, Enum):
    STAGE_PAUSE_GRACE = "GLOBAL_PAUSE_GRACE"                      # Days 1–4: Global SMS pause
    STAGE_1_STANDARD = "RENT_REMINDER_STAGE_1"                    # Day 5: Standard reminder (grace period ends today)
    STAGE_2_ESCALATION = "RENT_REMINDER_STAGE_2_ESCALATION"        # From Day 6 onward: Escalation reminder with 10-day notice
    STAGE_STANDARD_ROLLING = "RENT_REMINDER_STAGE_2_ESCALATION"   # Backward compatibility: From Day 6 onward rolling escalation


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

    @staticmethod
    def get_pm_datetime(current_time: Optional[datetime] = None) -> datetime:
        return get_pm_datetime(current_time)

    def get_reminder_stage(self, current_time: Optional[datetime] = None) -> str:
        """
        Determines the reminder stage based on the monthly schedule in the property management timezone:
          - Days 1–4: GLOBAL_PAUSE_GRACE (Global SMS pause; no automated SMS sent; all data synced)
          - Day 5: RENT_REMINDER_STAGE_1 (First-stage standard rent reminder)
          - Day 6+: RENT_REMINDER_STAGE_2_ESCALATION (From Day 6 onward: Escalation reminder with 10-day notice)
        """
        pm_dt = get_pm_datetime(current_time)
        day = pm_dt.day
        if 1 <= day <= 4:
            return ReminderStage.STAGE_PAUSE_GRACE.value
        elif day == 5:
            return ReminderStage.STAGE_1_STANDARD.value
        else:
            return ReminderStage.STAGE_2_ESCALATION.value

    def check_escalation_eligibility(
        self,
        tenant: Tenant,
        record: Optional[RentVineRecord],
        current_time: Optional[datetime] = None,
        raw_follow_up: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Strictly verifies all required conditions before generating or dispatching the Day 6 escalation message:
          1. Latest RentVine balance is still outstanding (> $0.00).
          2. Tenant has not already paid.
          3. No active payment promise covering current date.
          4. Tenant is not marked Manual Review or Dispute.
          5. Balance is not below the $200 manual-review threshold (must be >= $200.00).
          6. Applicable reminder cooldown has expired (at least 24h since previous dispatch).
          7. Tenant received the first-stage reminder, and its dispatch was confirmed.
          8. Tenant has not already received an escalation reminder this billing cycle.
        If ANY condition is not satisfied, returns (False, reason).
        """
        if not record:
            return False, "No RentVine record on file for tenant"

        # 1 & 2: Balance outstanding and not paid
        if record.balance <= 0.0:
            return False, f"Rent balance is $0.00 — already paid in full"

        is_paid, paid_msg = self.check_paid_status(record)
        if is_paid:
            return False, paid_msg

        # 3: Low balance threshold ($0 < balance < $200 -> Manual Review)
        is_low, low_reason = self.check_low_balance_threshold(record.balance)
        if is_low:
            return False, low_reason

        # 4: Manual review / dispute check in exclusion list and follow-up notes
        is_excl, excl_reason = self.check_exclusion_list(tenant)
        if is_excl:
            return False, f"Account flagged for manual review / dispute: {excl_reason}"

        fu_text = raw_follow_up or record.follow_up_notes or ""
        has_mr, mr_reason = self.check_manual_review_in_notes(record, raw_text=fu_text)
        if has_mr:
            return False, mr_reason

        # 5: Active payment promise check
        has_promise, pdate, preason = self.check_existing_promise_in_notes(
            record, current_time=current_time, raw_follow_up=fu_text
        )
        if has_promise:
            return False, f"Active payment promise on file until {pdate}"

        # 6: Applicable reminder cooldown (24 hours)
        is_cooldown, last_rem, cd_reason = self.check_recent_reminder_deduplication(tenant.id, current_time)
        if is_cooldown:
            return False, cd_reason

        # 7 & 8: On Day 6 specifically, verify confirmed Stage 1 received and prevent duplicate on Day 6
        pm_dt = get_pm_datetime(current_time)
        if pm_dt.day == 6:
            has_stage_1 = db.has_received_stage_1_reminder(tenant.id, pm_dt.year, pm_dt.month)
            if not has_stage_1:
                return False, f"Stage 1 reminder was not confirmed as dispatched for {pm_dt.strftime('%B %Y')} billing cycle"

            has_escalated = db.has_received_escalation_reminder(tenant.id, pm_dt.year, pm_dt.month)
            if has_escalated:
                return False, f"Stage 2 escalation reminder already dispatched for {pm_dt.strftime('%B %Y')} billing cycle"

        return True, "All escalation conditions satisfied"

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

    def check_low_balance_threshold(self, balance: float) -> Tuple[bool, Optional[str]]:
        """
        Rule: If total outstanding balance is > $0.00 but < $200.00, automatically flag for Manual Review / Disputes
        rather than sending automated rent reminders.
        """
        try:
            b_val = float(balance or 0.0)
        except Exception:
            b_val = 0.0

        if 0.0 < b_val < 200.0:
            return (
                True,
                f"Total outstanding balance (${b_val:,.2f}) is greater than $0 but less than $200 — automatically moved to Manual Review / Disputes for staff verification."
            )
        return False, None

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
        - When a workflow-generated rent reminder is sent, put that tenant into cooldown
          for exactly 24 hours from actual dispatch timestamp.
        - When 24 hours expire (hours_elapsed >= 24.0), automatically reactivate the tenant
          for reminder eligibility, provided they still have an outstanding balance.
        - If no workflow-generated reminder was sent, do not activate cooldown.
        """
        now = current_time or datetime.utcnow()
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)

        is_in_cooldown, last_reminder, hours_elapsed, hours_remaining, expires_at = db.get_cooldown_status(
            tenant_id, current_time=now, cooldown_hours=24.0
        )

        if not last_reminder:
            return False, None, "No workflow-generated rent reminders on record"

        if is_in_cooldown and expires_at:
            return (
                True,
                last_reminder,
                f"Workflow rent reminder sent {hours_elapsed:.1f}h ago. "
                f"Cooldown active ({hours_remaining:.1f}h remaining until {expires_at.strftime('%Y-%m-%d %I:%M %p')})"
            )
        else:
            return (
                False,
                last_reminder,
                f"Previous workflow reminder sent {hours_elapsed:.1f}h ago "
                f"({last_reminder.timestamp[:16]}) — 24h cooldown window expired. Eligible for reminder."
            )

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
