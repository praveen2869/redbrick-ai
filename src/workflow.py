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
from datetime import timedelta
from typing import List, Optional, Dict, Any
from src.config import settings
from src.models import (
    Tenant, RentVineRecord, DecisionOutcome, WorkflowDecision,
    DailyRunSummary, IntentType, Channel, LeaseTenantGroup
)
from src.database import db
from src.clients.rentvine_mcp import RentVineMCPClient
from src.clients.quo_sms import QuoSMSClient
from src.clients.gmail_client import GmailClient
from src.engine.rules import BusinessRulesEngine, ReminderStage
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

        # Step 3: Group tenants by lease and process each group as one unit.
        # For single-tenant leases → standard per-tenant logic.
        # For multi-tenant leases → one combined group SMS sent to all co-tenants.
        lease_groups = db.get_lease_tenant_groups(active_only=False)

        # Build a quick set of all tenant IDs already covered by their group
        # so we do not double-process a co-tenant that also appears in the flat list.
        processed_tenant_ids: set = set()

        for group in lease_groups:
            try:
                # Only process this group if the primary tenant is in our active list
                group_tenant_ids = {t.id for t in group.all_tenants}
                if not group_tenant_ids.intersection({t.id for t in tenants}):
                    continue

                if group.is_multi_tenant:
                    # --- Multi-Tenant Group: send ONE combined SMS ---
                    group_decisions = self._process_lease_group(
                        group=group,
                        run_id=run_id,
                        is_dry_run=is_dry_run,
                        current_time=current_time,
                        force_failure_tenant_id=force_failure_tenant_id,
                        report_lookup=report_lookup
                    )
                    for decision in group_decisions:
                        decisions.append(decision)
                        processed_tenant_ids.add(decision.tenant_id)
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
                        else:
                            count_skipped += 1
                            total_unpaid_balance += decision.balance
                else:
                    # --- Single-Tenant Lease: standard per-tenant processing ---
                    tenant = group.primary_tenant
                    if tenant.id in processed_tenant_ids:
                        continue
                    decision = self._process_tenant(
                        tenant=tenant,
                        run_id=run_id,
                        is_dry_run=is_dry_run,
                        current_time=current_time,
                        force_failure=(tenant.id == force_failure_tenant_id),
                        report_lookup=report_lookup
                    )
                    decisions.append(decision)
                    processed_tenant_ids.add(tenant.id)
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
                    else:
                        count_skipped += 1
                        total_unpaid_balance += decision.balance

            except Exception as e:
                group_id = getattr(group, "group_id", "unknown")
                logger.error(f"Error processing lease group {group_id}: {e}", exc_info=True)
                count_failed += 1

        # Fallback: process any active tenants not covered by any group
        for tenant in tenants:
            if tenant.id in processed_tenant_ids:
                continue
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
                processed_tenant_ids.add(tenant.id)
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
            total_tenants=len(tenants),  # individual tenant count (not group count)
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

    # ─── Multi-Tenant Group Helpers ──────────────────────────────────────────

    @staticmethod
    def _format_tenant_names_list(names: List[str]) -> str:
        """
        Formats a list of tenant names for use in the SMS greeting.
        Deduplicates names preserving order so no name is repeated.
        Examples:
          ["Joshua"]                                     -> "Joshua"
          ["Joshua", "Sarah"]                            -> "Joshua and Sarah"
          ["Joshua","Sarah","Kai"]                       -> "Joshua, Sarah, and Kai"
          ["Kukunoor Tenant", "Second Kukunoor Tenant"]  -> "Kukunoor Tenant and Second Kukunoor Tenant"
        """
        deduped = []
        seen = set()
        for n in names:
            clean = (n or "").strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                deduped.append(clean)

        if not deduped:
            return "Tenant"
        if len(deduped) == 1:
            return deduped[0]
        if len(deduped) == 2:
            return f"{deduped[0]} and {deduped[1]}"
        return ", ".join(deduped[:-1]) + f", and {deduped[-1]}"

    def _process_lease_group(
        self,
        group: LeaseTenantGroup,
        run_id: str,
        is_dry_run: bool,
        current_time,
        force_failure_tenant_id: Optional[str] = None,
        report_lookup: Optional[Dict[str, Any]] = None
    ) -> List[WorkflowDecision]:
        """
        Processes a multi-tenant lease group:
        1. Runs the full eligibility decision on the PRIMARY tenant (holds the balance).
        2. If a reminder is warranted, generates ONE combined group SMS that addresses
           all co-tenants by name and is dispatched to every verified phone number.
        3. Returns one WorkflowDecision per tenant so deduplication and audit trails
           are maintained per individual tenant.
        """
        primary = group.primary_tenant
        logger.info(
            f"[Group SMS] Processing lease group {group.lease_id} "
            f"({len(group.all_tenants)} tenants): {[t.full_name for t in group.all_tenants]}"
        )

        # Step 1: Run the standard workflow decision on the primary tenant
        primary_decision = self._process_tenant(
            tenant=primary,
            run_id=run_id,
            is_dry_run=is_dry_run,
            current_time=current_time,
            force_failure=(primary.id == force_failure_tenant_id),
            report_lookup=report_lookup
        )

        decisions = [primary_decision]

        # If the primary is not eligible for a reminder, propagate the same
        # outcome to all co-tenants as audit-trail entries (no SMS sent).
        if primary_decision.outcome != DecisionOutcome.SEND_REMINDER:
            for co in group.co_tenants:
                co_decision = WorkflowDecision(
                    tenant_id=co.id,
                    tenant_name=co.full_name,
                    property_unit=primary_decision.property_unit,
                    balance=0.0,  # balance tracked on primary tenant record
                    outcome=primary_decision.outcome,
                    reason=f"[Co-Tenant of {primary.full_name}] {primary_decision.reason}",
                    details={**primary_decision.details, "group_lease_id": group.lease_id,
                             "primary_tenant_id": primary.id, "is_co_tenant": True},
                    run_id=run_id
                )
                db.save_decision(co_decision)
                decisions.append(co_decision)
            return decisions

        # Step 2: Primary IS eligible → generate one combined group SMS
        # Collect all first names from verified phone holders
        all_names = group.all_names  # [primary.first_name, co1.first_name, ...]
        greeting_names = self._format_tenant_names_list(all_names)

        # Collect all verified phone numbers in the group
        phones_to_send: List[tuple] = []
        seen_phones: set = set()
        for t in group.all_tenants:
            if t.phone and t.phone.strip():
                from src.clients.quo_sms import phone_to_10digit
                p10 = phone_to_10digit(t.phone)
                if p10 and p10 not in seen_phones:
                    seen_phones.add(p10)
                    phones_to_send.append((t.id, t.full_name, t.phone))

        # Retrieve the RentVine record for balance/due-date info (held on primary)
        rv_record = self.rentvine.get_tenant_record(primary.id)
        balance = primary_decision.balance
        prop_unit = primary_decision.property_unit

        # Check reminder stage (Day 5 Stage 1 vs Day 6 Escalation vs Day 7+ Rolling)
        reminder_stage = self.rules.get_reminder_stage(current_time)
        is_stage_2 = (
            reminder_stage == ReminderStage.STAGE_2_ESCALATION.value
            or primary_decision.details.get("stage") == 2
            or primary_decision.details.get("reminder_type") == "RENT_REMINDER_STAGE_2_ESCALATION"
        )

        if is_stage_2:
            # Multi-tenant group escalation: greeting addresses all unique tenant names
            deduped_names = []
            for n in all_names:
                clean_n = (n or "").strip()
                if clean_n and clean_n not in deduped_names:
                    deduped_names.append(clean_n)
            greeting_names = ", ".join(deduped_names)

            group_sms_text = settings.GROUP_ESCALATION_REMINDER_TEMPLATE.format(
                tenant_names=greeting_names,
                tenant_name=greeting_names,
                balance=balance,
                property_name=primary.property_name,
                unit_number=primary.unit_number,
                property_address=prop_unit,
                due_date=rv_record.due_date if rv_record else "1st of the month",
                portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL
            )
            reminder_type = "RENT_REMINDER_STAGE_2_ESCALATION"
            stage_num = 2
            rv_note_title = "[GROUP AUTOMATED ESCALATION RENT REMINDER SENT]"
            reason_label = "Unpaid balance ${balance:,.2f} — Stage 2 Escalation (10-day notice warning) group SMS dispatched to all co-tenants"
        else:
            group_sms_text = settings.GROUP_REMINDER_TEMPLATE.format(
                tenant_names=greeting_names,
                tenant_name=greeting_names,  # ReminderTemplate uses tenant_name internally
                balance=balance,
                property_name=primary.property_name,
                unit_number=primary.unit_number,
                property_address=prop_unit,
                due_date=rv_record.due_date if rv_record else "1st of the month",
                portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL
            )
            reminder_type = "RENT_REMINDER_STAGE_1" if reminder_stage == ReminderStage.STAGE_1_STANDARD.value else "RENT_REMINDER"
            stage_num = 1
            rv_note_title = "[GROUP AUTOMATED RENT REMINDER SENT]"
            reason_label = f"Unpaid ${balance:,.2f} — group SMS dispatched to all co-tenants"

        logger.info(
            f"[Group SMS] Sending combined reminder (stage {stage_num}) for lease {group.lease_id} "
            f"to {len(phones_to_send)} recipients: {[p[2] for p in phones_to_send]}"
        )
        logger.debug(f"[Group SMS] Message:\n{group_sms_text}")

        # Step 3: Dispatch ONE combined Group SMS to all verified co-tenants in the group
        any_sms_sent = False
        any_note_written = False
        all_msg_ids: List[str] = []
        all_group_phones = [p[2] for p in phones_to_send]

        if not is_dry_run and all_group_phones:
            try:
                # OpenPhone API: passing a list of numbers in `to` initiates/uses the GROUP MMS thread
                send_res = self.quo.send_sms(
                    tenant_id=primary.id,
                    to_number=all_group_phones,
                    text=group_sms_text,
                    recipient_type="tenant",
                    force_failure=(primary.id == force_failure_tenant_id)
                )
                status_res = send_res.get("status", "")
                is_actual_dispatch = status_res in ("SENT", "DELIVERED") or bool(send_res.get("dispatched", False))
                if is_actual_dispatch:
                    any_sms_sent = True
                    sms_msg_id = send_res.get("message_id")
                    all_msg_ids.append(sms_msg_id or "")
                    now_iso = datetime.utcnow().isoformat()
                    for t_id, t_full_name, t_phone in phones_to_send:
                        reminder_id = f"rem_{t_id}_{datetime.utcnow().strftime('%Y%m%d')}"
                        db.record_workflow_reminder(
                            reminder_id=reminder_id,
                            tenant_id=t_id,
                            reminder_type=reminder_type,
                            message_id=sms_msg_id or reminder_id,
                            workflow_id=run_id,
                            phone=t_phone,
                            content=group_sms_text,
                            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
                            delivery_status="DELIVERED",
                            sent_at=now_iso,
                            stage=stage_num
                        )
            except Exception as e:
                for t_id, t_full_name, t_phone in phones_to_send:
                    db.add_retry_queue_item(
                        tenant_id=t_id,
                        payload={"to_number": t_phone, "text": group_sms_text, "recipient_type": "tenant"},
                        error=str(e)
                    )
                logger.error(f"[Group SMS] Failed to send to {all_group_phones}: {e}. Queued for retry.")

        # Write ONE consolidated RentVine note for the entire group (on primary tenant)
        if not is_dry_run and any_sms_sent:
            phone_list = ", ".join(p[2] for p in phones_to_send)
            rv_note_text = (
                f"{rv_note_title} Quo SMS delivered to group "
                f"({len(phones_to_send)} recipients: {phone_list}) "
                f"from {settings.QUO_PHONE_TENANTS_FORMATTED} (Tenants & Vendors) "
                f"for ${balance:,.2f} due on {rv_record.due_date if rv_record else 'N/A'}. "
                f"Co-tenants notified: {greeting_names}."
            )
            self.rentvine.write_tenant_note(primary.id, rv_note_text, author="AI Rent Reminder Agent")
            any_note_written = True

        # Step 4: Create WorkflowDecision entries for the primary and each co-tenant
        # Update the primary decision (already saved by _process_tenant) with group context
        group_detail = {
            "group_sms_text": group_sms_text,
            "phones_sent": [p[2] for p in phones_to_send],
            "tenant_names_in_sms": all_names,
            "greeting": greeting_names,
            "lease_id": group.lease_id,
            "is_group_reminder": True,
            "stage": stage_num,
            "reminder_type": reminder_type,
            "co_tenant_count": len(group.co_tenants),
            "is_dry_run": is_dry_run,
            "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
            "sender_inbox": "Tenants & Vendors"
        }
        reason_prefix = (
            f"[GROUP REMINDER] Lease {group.lease_id} · {len(phones_to_send)} recipient(s): {greeting_names} · "
        )
        primary_group_decision = WorkflowDecision(
            tenant_id=primary.id,
            tenant_name=primary.full_name,
            property_unit=prop_unit,
            balance=balance,
            outcome=DecisionOutcome.SEND_REMINDER,
            reason=reason_prefix + reason_label.format(balance=balance),
            details=group_detail,
            sms_sent=any_sms_sent,
            sms_message_id=all_msg_ids[0] if all_msg_ids else None,
            note_written=any_note_written,
            run_id=run_id
        )
        db.save_decision(primary_group_decision)
        # Replace the original primary_decision (from per-tenant call) in the list
        decisions[0] = primary_group_decision

        # Create decisions for co-tenants (audit trail, no duplicate SMS)
        for idx, co in enumerate(group.co_tenants):
            co_msg_id = all_msg_ids[idx + 1] if idx + 1 < len(all_msg_ids) else None
            co_decision = WorkflowDecision(
                tenant_id=co.id,
                tenant_name=co.full_name,
                property_unit=prop_unit,
                balance=0.0,  # balance tracked on primary tenant only
                outcome=DecisionOutcome.SEND_REMINDER,
                reason=reason_prefix + f"Co-tenant of {primary.full_name} — group SMS sent to {co.phone}",
                details={**group_detail, "is_co_tenant": True, "primary_tenant_id": primary.id},
                sms_sent=any_sms_sent and bool(co.phone),
                sms_message_id=co_msg_id,
                note_written=False,
                run_id=run_id
            )
            db.save_decision(co_decision)
            decisions.append(co_decision)

        logger.info(
            f"[Group SMS] Completed lease group {group.lease_id}: "
            f"{len(phones_to_send)} SMS(es) dispatched, "
            f"{len(decisions)} audit decisions recorded."
        )
        return decisions

    # ─────────────────────────────────────────────────────────────────────────

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
        report_rec = None

        # ─── Report #72 Source of Truth Validation ───
        if report_lookup:
            clean_phone = "".join([c for c in (tenant.phone or "") if c.isdigit()])
            if clean_phone and clean_phone[-10:] in report_lookup:
                report_rec = report_lookup[clean_phone[-10:]]
            elif tenant.id.startswith("RV-") and tenant.id.replace("RV-", "") in report_lookup:
                report_rec = report_lookup[tenant.id.replace("RV-", "")]
            elif tenant.id.startswith("rv_lease_") and tenant.id.replace("rv_lease_", "") in report_lookup:
                report_rec = report_lookup[tenant.id.replace("rv_lease_", "")]
            elif tenant.full_name.strip().lower() in report_lookup:
                report_rec = report_lookup[tenant.full_name.strip().lower()]

            if not report_rec:
                # Safeguard: If tenant has Manual Review in Follow-Up Notes, always preserve Manual Review!
                fu_text = getattr(rv_record, "follow_up_notes", "") or ""
                if (tenant.is_excluded and "manual review" in (tenant.exclusion_reason or "").lower()) or "manual review" in fu_text.lower():
                    decision = WorkflowDecision(
                        tenant_id=tenant.id,
                        tenant_name=tenant.full_name,
                        property_unit=prop_unit,
                        balance=balance,
                        outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                        reason="Follow-Up Notes contains 'Manual Review' in RentVine — automated reminders disabled for staff review",
                        details={"step": "manual_review_safeguard", "action": "Manual Review / Disputes", "follow_up_notes": fu_text},
                        run_id=run_id
                    )
                    db.save_decision(decision)
                    return decision

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

        # Rule 1a: Low Balance Threshold Check ($0 < balance < $200 -> Manual Review / Disputes)
        is_low_bal, low_bal_reason = self.rules.check_low_balance_threshold(balance)
        if is_low_bal:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                reason=low_bal_reason,
                details={"step": "low_balance_threshold", "action": "Manual Review / Disputes", "balance": balance, "threshold": "< $200"},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Rule 1b: Follow-Up Notes Manual Review Check (RentVine Source of Truth)
        fu_text = (report_rec.get("follow_up_notes") if report_rec else None) or (rv_record.follow_up_notes if rv_record else "")
        has_mr, mr_reason = self.rules.check_manual_review_in_notes(rv_record, raw_text=fu_text)
        if has_mr:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                reason=mr_reason,
                details={"step": "follow_up_notes_manual_review", "action": "Manual Review / Disputes", "follow_up_notes": fu_text},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # If Manual Review was previously stored on the tenant but has now been removed/cleared in RentVine,
        # dynamically restore the tenant's normal reminder eligibility!
        if tenant.is_excluded and "manual review" in (tenant.exclusion_reason or "").lower():
            if not has_mr:
                tenant.is_excluded = False
                tenant.exclusion_reason = None
                db.upsert_tenant(tenant)

        # Rule 2: Manual Exclusion Check (Legal dispute / outside PM handling)
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

        # Rule 3: Check existing active promise in RentVine notes and Follow-Up Notes
        has_note_promise, note_pdate, note_preason = self.rules.check_existing_promise_in_notes(
            rv_record, current_time, raw_follow_up=fu_text
        )
        if has_note_promise:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.HOLD_PROMISE,
                reason=note_preason,
                details={"step": "active_note_promise", "promise_date": note_pdate, "follow_up_notes": fu_text},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Step 2b: Pull Communication Data ONLY for confirmed overdue tenants
        # (Report #72 validation above ensures we only reach here for unpaid rent tenants)
        logger.debug(f"[SMS Fetch] Fetching conversation history for overdue tenant {tenant.id} ({tenant.full_name})")
        quo_fetch_failed = False
        try:
            sms_history = self.quo.get_conversation_history(tenant.id, tenant.phone, days=7, reference_time=current_time)
        except Exception as e:
            logger.error(f"[Quo Retrieval Error] Could not fetch SMS for {tenant.id} ({tenant.phone}): {e}")
            quo_fetch_failed = True
            sms_history = []

        if quo_fetch_failed:
            logger.warning(f"[Workflow] Proceeding with empty communication history for {tenant.id} due to API retrieval error")

        email_history = self.gmail.get_recent_email_replies(tenant.id, tenant.email, hours=48, reference_time=current_time)
        combined_messages = sms_history + email_history

        # Step 3: Run AI NLU Intent Classifier on communications
        ai_result = self.ai.classify_tenant_communications(combined_messages, reference_date=current_time)

        # AI Outcome 1: PROMISE_MADE (Specific payment date provided)
        if ai_result.intent == IntentType.PROMISE_MADE and ai_result.extracted_date:
            promise_date = ai_result.extracted_date
            today_str = current_time.strftime("%Y-%m-%d")

            # Check if promise date is active (current or future date)
            if promise_date >= today_str:
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
                    reason=f"Payment promised via {ai_result.source_channel.value} for {promise_date}: \"{ai_result.raw_text}\" — reminder held",
                    details={
                        "intent": ai_result.intent.value,
                        "confidence": ai_result.confidence,
                        "extracted_date": promise_date,
                        "promise_date": promise_date,
                        "raw_text": ai_result.raw_text,
                        "channel": ai_result.source_channel.value,
                        "note_written": not is_dry_run
                    },
                    note_written=not is_dry_run,
                    run_id=run_id
                )
                db.save_decision(decision)
                return decision
            else:
                logger.info(
                    f"Prior payment promise for {tenant.id} expired on {promise_date}. "
                    f"Balance is ${balance:,.2f}; proceeding to evaluate reminder eligibility."
                )

        # AI Outcome 2: PAYMENT_UNSPECIFIED (Payment-related reply without specific date -> Manual Review / Disputes)
        elif ai_result.intent == IntentType.PAYMENT_UNSPECIFIED:
            note_text = (
                f"[MANUAL REVIEW - NO PAYMENT DATE] Tenant replied: \"{ai_result.raw_text}\". "
                "Payment-related reply without confirmed date; flagged for Manual Review / Disputes."
            )
            if not is_dry_run:
                self.rentvine.write_tenant_note(tenant.id, note_text, author="AI Rent Reminder Agent")

            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                reason=f"Payment-related reply without specific payment date (\"{ai_result.raw_text}\") — moved to Manual Review / Disputes",
                details={
                    "intent": ai_result.intent.value,
                    "confidence": ai_result.confidence,
                    "raw_text": ai_result.raw_text,
                    "channel": ai_result.source_channel.value,
                    "classification_reason": "Payment-related reply without specific date"
                },
                note_written=not is_dry_run,
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # AI Outcome 3: UNABLE_TO_PAY (Unable or unwilling to pay -> Manual Review / Disputes for staff follow-up)
        elif ai_result.intent == IntentType.UNABLE_TO_PAY:
            note_text = (
                f"[MANUAL REVIEW - UNABLE TO PAY] Tenant stated unable/unwilling to pay: \"{ai_result.raw_text}\". "
                "Flagged for staff follow-up."
            )
            if not is_dry_run:
                self.rentvine.write_tenant_note(tenant.id, note_text, author="AI Rent Reminder Agent")

            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=balance,
                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                reason=f"Unable/unwilling to pay (\"{ai_result.raw_text}\") — moved to Manual Review / Disputes for staff follow-up",
                details={
                    "intent": ai_result.intent.value,
                    "confidence": ai_result.confidence,
                    "raw_text": ai_result.raw_text,
                    "channel": ai_result.source_channel.value,
                    "classification_reason": "Unable or unwilling to pay"
                },
                note_written=not is_dry_run,
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # AI Outcome 4: DISPUTE
        elif ai_result.intent == IntentType.DISPUTE:
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

        # AI Outcome 5: ALREADY_PAID claim
        elif ai_result.intent == IntentType.ALREADY_PAID:
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

        # AI Outcome 6: UNRELATED / GENERAL_INQUIRY / NO_REPLY
        # Unrelated messages do not alter reminder eligibility; proceed to evaluate standard business rules below.

        # Rule 4: Grace Period Check (1st–4th of current month for current-month rent)
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

        # Step 4: Final Payment Status Verification before dispatch
        # Re-check latest RentVine ledger record to ensure rent remains unpaid
        verify_rv = self.rentvine.get_tenant_record(tenant.id) or rv_record
        latest_balance = float(verify_rv.balance if verify_rv else balance)
        if latest_balance <= 0.0:
            decision = WorkflowDecision(
                tenant_id=tenant.id,
                tenant_name=tenant.full_name,
                property_unit=prop_unit,
                balance=0.0,
                outcome=DecisionOutcome.SKIP_PAID,
                reason="Payment status verified: balance is $0.00 — rent is already paid in full",
                details={"step": "pre_dispatch_payment_verification", "balance": 0.0},
                run_id=run_id
            )
            db.save_decision(decision)
            return decision

        # Step 4: Determine Reminder Stage & Select Date-Based Template
        reminder_stage = self.rules.get_reminder_stage(current_time)

        if reminder_stage == ReminderStage.STAGE_2_ESCALATION.value:
            # From Day 6 onward: Second-stage Escalation Reminder (10-day notice)
            pm_dt = self.rules.get_pm_datetime(current_time)

            if pm_dt.day == 6:
                has_stage_1 = db.has_received_stage_1_reminder(tenant.id, pm_dt.year, pm_dt.month)
                if not has_stage_1:
                    decision = WorkflowDecision(
                        tenant_id=tenant.id,
                        tenant_name=tenant.full_name,
                        property_unit=prop_unit,
                        balance=balance,
                        outcome=DecisionOutcome.SKIP_ALREADY_REMINDED,
                        reason="Day 6 escalation suppressed: Stage 1 reminder was not confirmed as dispatched for this billing cycle",
                        details={
                            "step": "day_6_escalation_guard",
                            "stage": 2,
                            "reminder_stage": reminder_stage,
                            "suppressed_reason": "Stage 1 not confirmed"
                        },
                        run_id=run_id
                    )
                    db.save_decision(decision)
                    return decision

                has_escalated = db.has_received_escalation_reminder(tenant.id, pm_dt.year, pm_dt.month)
                if has_escalated:
                    decision = WorkflowDecision(
                        tenant_id=tenant.id,
                        tenant_name=tenant.full_name,
                        property_unit=prop_unit,
                        balance=balance,
                        outcome=DecisionOutcome.SKIP_ALREADY_REMINDED,
                        reason=f"Stage 2 escalation reminder already dispatched for {pm_dt.strftime('%B %Y')} billing cycle",
                        details={
                            "step": "escalation_duplicate_guard",
                            "stage": 2,
                            "reminder_stage": reminder_stage
                        },
                        run_id=run_id
                    )
                    db.save_decision(decision)
                    return decision

            # Day 6+ reminder: Always uses the escalation template (10-day notice). Never falls back to Day 5 template.
            sms_text = settings.ESCALATION_REMINDER_TEMPLATE.format(
                tenant_name=tenant.greeting_name,
                balance=balance,
                property_name=tenant.property_name,
                unit_number=tenant.unit_number,
                property_address=prop_unit,
                due_date=rv_record.due_date if rv_record else "1st of the month",
                portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL
            )
            reminder_type = "RENT_REMINDER_STAGE_2_ESCALATION"
            stage_num = 2
            rv_note_title = "[AUTOMATED ESCALATION RENT REMINDER SENT]"
            decision_reason = (
                f"Unpaid balance ${balance:,.2f} — Stage 2 Escalation reminder (10-day notice warning) "
                f"dispatched" if not is_dry_run else f"[DRY-RUN] Would send Stage 2 Escalation reminder for ${balance:,.2f}"
            )
        else:
            # Day 5 (Stage 1) or Day 7+ (Standard rolling)
            sms_text = settings.REMINDER_TEMPLATE.format(
                tenant_name=tenant.greeting_name,
                balance=balance,
                property_name=tenant.property_name,
                unit_number=tenant.unit_number,
                property_address=prop_unit,
                due_date=rv_record.due_date if rv_record else "1st of the month",
                portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL
            )
            if reminder_stage == ReminderStage.STAGE_1_STANDARD.value:
                reminder_type = "RENT_REMINDER_STAGE_1"
                stage_num = 1
                rv_note_title = "[AUTOMATED RENT REMINDER STAGE 1 SENT]"
                decision_reason = (
                    f"Unpaid balance ${balance:,.2f} past grace period — Stage 1 rent reminder dispatched"
                    if not is_dry_run else f"[DRY-RUN] Would send Stage 1 reminder for ${balance:,.2f}"
                )
            else:
                reminder_type = "RENT_REMINDER"
                stage_num = 1
                rv_note_title = "[AUTOMATED RENT REMINDER SENT]"
                decision_reason = (
                    f"Unpaid balance ${balance:,.2f} — standard rolling rent reminder dispatched"
                    if not is_dry_run else f"[DRY-RUN] Would send standard rolling reminder for ${balance:,.2f}"
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
                    # Record workflow-generated rent reminder delivery record
                    now_iso_dispatch = datetime.utcnow().isoformat()
                    reminder_id = f"rem_{tenant.id}_{datetime.utcnow().strftime('%Y%m%d')}"
                    db.record_workflow_reminder(
                        reminder_id=reminder_id,
                        tenant_id=tenant.id,
                        reminder_type=reminder_type,
                        message_id=sms_msg_id or reminder_id,
                        workflow_id=run_id,
                        phone=tenant.phone,
                        content=sms_text,
                        sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
                        delivery_status="DELIVERED",
                        sent_at=now_iso_dispatch,
                        stage=stage_num
                    )

                    # Log confirmation note to RentVine only when actually dispatched
                    rv_note_text = (
                        f"{rv_note_title} Quo SMS delivered to {tenant.phone} "
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
            reason=decision_reason,
            details={
                "sms_text": sms_text,
                "phone": tenant.phone,
                "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                "sender_inbox": "Tenants & Vendors",
                "sender_phone_id": settings.QUO_PHONE_TENANTS_ID,
                "is_dry_run": is_dry_run,
                "force_failed": force_failure,
                "stage": stage_num,
                "reminder_type": reminder_type,
                "reminder_status": "REMINDER_NEEDED (SEND_SKIPPED)"
            },
            sms_sent=sms_sent,
            sms_message_id=sms_msg_id,
            note_written=note_written,
            run_id=run_id
        )
        db.save_decision(decision)
        return decision
