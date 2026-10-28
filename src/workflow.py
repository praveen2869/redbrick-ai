"""
Red Bricks Property Management - 5-Step Workflow Orchestrator
Executes the full automated workflow logic:
Step 1: Scheduler Trigger
Step 2: Data Collection (RentVine MCP + Quo SMS + Gmail API)
Step 3: Per-Tenant Decision Loop (Rules + AI NLU)
Step 4: Action Dispatch (Quo SMS, RentVine Note Write-back, Deduplication)
Step 5: Results & Daily Report Summary
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from src.config import settings
from src.models import (
    Tenant, RentVineRecord, DecisionOutcome, WorkflowDecision,
    DailyRunSummary, IntentType, Channel
)
from src.database import db
from src.clients.rentvine_mcp import RentVineMCPClient
from src.clients.quo_sms import QuoSMSClient
from src.clients.gmail_client import GmailClient
from src.engine.rules import BusinessRulesEngine
from src.engine.ai_classifier import AIIntentClassifier
from src.engine.retry_manager import RetryManager
from src.sync_manager import sync_manager

logger = logging.getLogger("WorkflowOrchestrator")

class WorkflowOrchestrator:
    def __init__(
        self,
        rentvine_client: Optional[RentVineMCPClient] = None,
        quo_client: Optional[QuoSMSClient] = None,
        gmail_client: Optional[GmailClient] = None,
        rules_engine: Optional[BusinessRulesEngine] = None,
        ai_classifier: Optional[AIIntentClassifier] = None,
    ):
        self.rentvine = rentvine_client or RentVineMCPClient()
        self.quo = quo_client or QuoSMSClient()
        self.gmail = gmail_client or GmailClient()
        self.rules = rules_engine or BusinessRulesEngine()
        self.ai = ai_classifier or AIIntentClassifier()
        self.retry_mgr = RetryManager(self.quo)

    def execute_daily_run(
        self,
        is_dry_run: bool = False,
        reference_time: Optional[datetime] = None,
        force_failure_tenant_id: Optional[str] = None,
        use_report_source_of_truth: Optional[bool] = None
    ) -> DailyRunSummary:
        """
        Executes the daily automated rent reminder workflow across the entire tenant portfolio.
        """
        run_id = f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        started_at = datetime.utcnow().isoformat()
        current_time = reference_time or datetime.utcnow()

        logger.info(f"=== Starting Daily Rent Workflow Run [{run_id}] (Dry-Run: {is_dry_run}) ===")

        # Process any pending retries from prior runs first
        self.retry_mgr.process_pending_retries()

        # Step 1: Wait for any ongoing background sync to complete
        sync_manager.wait_for_sync(timeout=60.0)

        # Authoritative Source of Truth: RentVine Report #72 (Rent Rem Praveen)
        # In live mode or when explicitly requested, Report #72 is the strict source of truth
        should_use_report = (not self.rentvine.mock_mode) if use_report_source_of_truth is None else use_report_source_of_truth
        report_data = None
        report_lookup: Dict[str, Dict[str, Any]] = {}

        if should_use_report:
            try:
                report_data = self.rentvine.fetch_lease_balance_report(report_id=72)
                # Sync report to DB to guarantee perfect alignment
                self.rentvine.sync_report_to_database(report_id=72)
                for rec in report_data.get("records", []):
                    clean_p = "".join([c for c in (rec.get("phone") or "") if c.isdigit()])
                    if clean_p:
                        report_lookup[clean_p[-10:]] = rec
                    if rec.get("lease_id"):
                        report_lookup[str(rec.get("lease_id"))] = rec
                    if rec.get("tenant_name"):
                        report_lookup[rec.get("tenant_name").strip().lower()] = rec
                logger.info(f"Loaded Report #72 with {len(report_data.get('records', []))} authoritative overdue records.")
            except Exception as e:
                logger.warning(f"Could not fetch Report #72 as source of truth: {e}")

        # Pull active lease tenants
        tenants: List[Tenant] = db.get_active_tenants()

        # If no active tenants exist and live mode is active, trigger sync
        if not self.rentvine.mock_mode and not tenants:
            try:
                synced = sync_manager.sync_live_portfolio(limit=1000)
                if synced > 0:
                    logger.info(f"Populated local SQLite database with {synced} real RentVine active records.")
                    tenants = db.get_active_tenants()
            except Exception as e:
                logger.warning(f"RentVine portfolio sync skipped: {e}")

        # Fallback to get_all_tenants if database has demo/seed data without active RV records
        if not tenants:
            tenants = db.get_all_tenants()

        # Step 2: Data Collection - pull tenants and records
        decisions: List[WorkflowDecision] = []
        count_paid = 0
        count_promised = 0
        count_reminded = 0
        count_skipped = 0
        count_flagged = 0
        count_failed = 0
        total_unpaid_balance = 0.0

        # Step 3: Per-Tenant Decision Loop
        for tenant in tenants:
            try:
                decision = self._process_tenant(
                    tenant=tenant,
                    run_id=run_id,
                    is_dry_run=is_dry_run,
                    current_time=current_time,
                    force_failure=(tenant.id == force_failure_tenant_id),
                    report_lookup=report_lookup
                )
                decisions.append(decision)

                # Aggregate metrics
                if decision.outcome == DecisionOutcome.SKIP_PAID:
                    count_paid += 1
                elif decision.outcome == DecisionOutcome.HOLD_PROMISE:
                    count_promised += 1
                    total_unpaid_balance += decision.balance
                elif decision.outcome == DecisionOutcome.SEND_REMINDER:
                    count_reminded += 1
                    total_unpaid_balance += decision.balance
                elif decision.outcome == DecisionOutcome.FLAG_MANUAL_REVIEW:
                    count_flagged += 1
                    total_unpaid_balance += decision.balance
                else:  # SKIP_ALREADY_REMINDED or SKIP_GRACE_PERIOD
                    count_skipped += 1
                    total_unpaid_balance += decision.balance

            except Exception as e:
                logger.error(f"Error processing tenant {tenant.id} ({tenant.full_name}): {e}", exc_info=True)
                count_failed += 1

        completed_at = datetime.utcnow().isoformat()

        # Step 4 & 5: Save Summary & Return
        summary = DailyRunSummary(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            is_dry_run=is_dry_run,
            total_tenants=len(tenants),
            count_paid=count_paid,
            count_promised=count_promised,
            count_reminded=count_reminded,
            count_skipped=count_skipped,
            count_flagged=count_flagged,
            count_failed=count_failed,
            total_balance_unpaid=total_unpaid_balance,
            decisions=decisions
        )

        db.save_run_summary(summary)
        logger.info(
            f"=== Finished Run [{run_id}]: {len(tenants)} evaluated | "
            f"{count_reminded} reminded | {count_paid} paid | {count_promised} promises | "
            f"{count_skipped} skipped | {count_flagged} flagged ==="
        )

        return summary

    def _process_tenant(
        self,
        tenant: Tenant,
        run_id: str,
        is_dry_run: bool,
        current_time: datetime,
        force_failure: bool = False,
        report_lookup: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> WorkflowDecision:
        """
        Evaluates a single tenant against deterministic rules and AI NLU signals.
        Validates directly against RentVine Report #72 as the single source of truth.
        """
        # Step 2a: Pull RentVine data via MCP
        rv_record = self.rentvine.get_tenant_record(tenant.id)
        balance = rv_record.balance if rv_record else 0.0
        prop_unit = f"{tenant.property_name} {tenant.unit_number}"

        # ─── Report #72 Source of Truth Validation ───
        if report_lookup:
            report_rec = None
            clean_phone = "".join([c for c in (tenant.phone or "") if c.isdigit()])
            if clean_phone and clean_phone[-10:] in report_lookup:
                report_rec = report_lookup[clean_phone[-10:]]
            elif tenant.id.startswith("rv_lease_") and tenant.id.replace("rv_lease_", "") in report_lookup:
                report_rec = report_lookup[tenant.id.replace("rv_lease_", "")]
            elif tenant.full_name.strip().lower() in report_lookup:
                report_rec = report_lookup[tenant.full_name.strip().lower()]

            if not report_rec:
                # Strictly excluded: Tenant is not listed in Report #72
                decision = WorkflowDecision(
                    tenant_id=tenant.id,
                    tenant_name=tenant.full_name,
                    property_unit=prop_unit,
                    balance=0.0,
                    outcome=DecisionOutcome.SKIP_PAID,
                    reason="Tenant not in RentVine Report #72 (Rent Rem Praveen) — balance paid/cleared or lease inactive",
                    details={"step": "report_72_validation", "status": "Excluded by Report 72"},
                    run_id=run_id
                )
                db.save_decision(decision)
                return decision

            # Validate criteria from Report #72
            past_due_rent = report_rec.get("past_due_rent", 0.0)
            past_due_total = report_rec.get("past_due_total", 0.0)
            total_balance = report_rec.get("total_balance", balance)
            balance = total_balance

            if past_due_total <= 0:
                decision = WorkflowDecision(
                    tenant_id=tenant.id,
                    tenant_name=tenant.full_name,
                    property_unit=prop_unit,
                    balance=balance,
                    outcome=DecisionOutcome.SKIP_PAID,
                    reason="Report #72: Total past due is $0.00 — tenant has no overdue balance",
                    details={"step": "report_72_criteria", "past_due_total": past_due_total},
                    run_id=run_id
                )
                db.save_decision(decision)
                return decision

        # Rule 1: Paid Check
        is_paid, paid_reason = self.rules.check_paid_status(rv_record)
        if is_paid:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.SKIP_PAID,
                reason=paid_reason,
                details={"step": "paid_check", "status": "Paid — no action"},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Rule 2: Manual Exclusion Check
        is_excluded, excl_reason = self.rules.check_exclusion_list(tenant)
        if is_excluded:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                reason=f"Account excluded: {excl_reason}",
                details={"step": "exclusion_check", "action": "Escalate to staff"},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Rule 3: Check existing active promise in RentVine notes
        has_note_promise, note_pdate, note_preason = self.rules.check_existing_promise_in_notes(rv_record, current_time)
        if has_note_promise:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.HOLD_PROMISE,
                reason=note_preason,
                details={"step": "active_note_promise", "promise_date": note_pdate},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Step 2b: Pull Communication Data (Quo SMS & Gmail)
        sms_history = self.quo.get_conversation_history(tenant.id, tenant.phone, days=7, reference_time=current_time)
        email_history = self.gmail.get_recent_email_replies(tenant.id, tenant.email, hours=48, reference_time=current_time)
        combined_messages = sms_history + email_history

        # Step 3: Run AI NLU Intent Classifier on communications
        ai_result = self.ai.classify_tenant_communications(combined_messages, reference_date=current_time)

        # AI Outcome A: PROMISE_MADE
        if ai_result.intent == IntentType.PROMISE_MADE:
            promise_date = ai_result.extracted_date or (current_time.date() + timedelta(days=3)).strftime("%Y-%m-%d")
            note_text = (
                f"[AI PAYMENT PROMISE ON FILE: Will pay by {promise_date}] "
                f"Tenant confirmed via {ai_result.source_channel.value}: \"{ai_result.raw_text}\""
            )
            # Write back note to RentVine MCP
            if not is_dry_run:
                self.rentvine.write_tenant_note(tenant.id, note_text, author="AI Rent Reminder Agent")

            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.HOLD_PROMISE,
                reason=f"Valid payment promise detected: will pay by {promise_date} (Confidence: {ai_result.confidence * 100:.0f}%)",
                details={
                    "intent": ai_result.intent.value,
                    "confidence": ai_result.confidence,
                    "extracted_date": promise_date,
                    "raw_text": ai_result.raw_text,
                    "channel": ai_result.source_channel.value,
                    "note_written": not is_dry_run
                },
                note_written=not is_dry_run,
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # AI Outcome B: DISPUTE
        if ai_result.intent == IntentType.DISPUTE:
            note_text = f"[DISPUTE DETECTED] Tenant response flagged: \"{ai_result.raw_text}\". Pausing auto-reminders."
            if not is_dry_run:
                self.rentvine.write_tenant_note(tenant.id, note_text, author="AI Rent Reminder Agent")

            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                reason=f"Dispute detected via {ai_result.source_channel.value}: {ai_result.reasoning}",
                details={
                    "intent": ai_result.intent.value,
                    "raw_text": ai_result.raw_text,
                    "channel": ai_result.source_channel.value
                },
                note_written=not is_dry_run,
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # AI Outcome C: ALREADY_PAID claim
        if ai_result.intent == IntentType.ALREADY_PAID:
            note_text = f"[PAYMENT CLAIMED] Tenant states rent was already paid: \"{ai_result.raw_text}\". Awaiting bank sync."
            if not is_dry_run:
                self.rentvine.write_tenant_note(tenant.id, note_text, author="AI Rent Reminder Agent")

            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.SKIP_PAID,
                reason=f"Tenant claims payment completed ({ai_result.raw_text}) - holding reminder for bank clearance",
                details={
                    "intent": ai_result.intent.value,
                    "raw_text": ai_result.raw_text,
                    "channel": ai_result.source_channel.value
                },
                note_written=not is_dry_run,
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Rule 4: Grace Period Check
        if rv_record:
            is_in_grace, grace_reason = self.rules.check_grace_period(rv_record, current_time)
            if is_in_grace:
                decision = WorkflowDecision(
                    tenant_id=tenant.id,
                    tenant_name=tenant.full_name,
                    property_unit=prop_unit,
                    balance=balance,
                    outcome=DecisionOutcome.SKIP_GRACE_PERIOD,
                    reason=grace_reason,
                    details={"step": "grace_period_check"},
                    run_id=run_id
                )
                db.save_decision(decision)
                return decision

        # Rule 5: Deduplication Check (Has reminder been sent in last 24h?)
        is_dup, last_rem, dedup_reason = self.rules.check_recent_reminder_deduplication(tenant.id, current_time)
        if is_dup:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.SKIP_ALREADY_REMINDED,
                reason=dedup_reason,
                details={
                    "step": "deduplication_check",
                    "last_reminder_timestamp": last_rem.timestamp if last_rem else None
                },
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Guard: If balance is zero or negative, do not dispatch rent reminder
        if balance <= 0.0:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.SKIP_PAID,
                reason="Balance is $0.00 — tenant has no outstanding balance",
                details={"step": "zero_balance_guard"},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Step 4: Unpaid & Eligible -> Dispatch Quo SMS Reminder
        sms_text = settings.REMINDER_TEMPLATE.format(
            tenant_name=tenant.first_name,
            balance=balance,
            property_name=tenant.property_name,
            unit_number=tenant.unit_number,
            due_date=rv_record.due_date if rv_record else "1st of the month",
            portal_link=rv_record.portal_link if rv_record else "https://redbrick.rentvine.com/portal/pay"
        )

        sms_sent = False
        sms_msg_id = None
        note_written = False

        if not is_dry_run:
            try:
                send_res = self.quo.send_sms(
                    tenant_id=tenant.id,
                    to_number=tenant.phone,
                    text=sms_text,
                    recipient_type="tenant",
                    force_failure=force_failure
                )
                # Cooldown feature is ONLY active once the reminder has actually been sent to carrier
                status_res = send_res.get("status", "")
                is_actual_dispatch = status_res in ("SENT", "DELIVERED") or bool(send_res.get("dispatched", False))
                sms_sent = is_actual_dispatch
                sms_msg_id = send_res.get("message_id")

                if is_actual_dispatch:
                    # Log confirmation note to RentVine only when actually dispatched
                    rv_note_text = (
                        f"[AUTOMATED RENT REMINDER SENT] Quo SMS delivered to {tenant.phone} "
                        f"from {settings.QUO_PHONE_TENANTS_FORMATTED} (Tenants & Vendors) "
                        f"for ${balance:,.2f} due on {rv_record.due_date if rv_record else 'N/A'}."
                    )
                    self.rentvine.write_tenant_note(tenant.id, rv_note_text, author="AI Rent Reminder Agent")
                    note_written = True

            except Exception as e:
                # Add to retry queue for automatic retry
                db.add_retry_queue_item(
                    tenant_id=tenant.id,
                    payload={"to_number": tenant.phone, "text": sms_text, "recipient_type": "tenant"},
                    error=str(e)
                )
                logger.error(f"Failed to dispatch SMS to {tenant.phone}: {e}. Queued for retry.")

        decision = WorkflowDecision(
            tenant_id=tenant.id,
            tenant_name=tenant.full_name,
            property_unit=prop_unit,
            balance=balance,
            outcome=DecisionOutcome.SEND_REMINDER,
            reason=f"Unpaid balance ${balance:,.2f} past grace period — [DEMO MODE] Reminder needs to be sent (Live SMS skipped for testing)" if not is_dry_run else f"[DRY-RUN] Would send Quo SMS reminder for ${balance:,.2f}",
            details={
                "sms_text": sms_text,
                "phone": tenant.phone,
                "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                "sender_inbox": "Tenants & Vendors",
                "sender_phone_id": settings.QUO_PHONE_TENANTS_ID,
                "is_dry_run": is_dry_run,
                "force_failed": force_failure,
                "reminder_status": "REMINDER_NEEDED (SEND_SKIPPED)"
            },
            sms_sent=sms_sent,
            sms_message_id=sms_msg_id,
            note_written=note_written,
            run_id=run_id
        )
        db.save_decision(decision)
        return decision
