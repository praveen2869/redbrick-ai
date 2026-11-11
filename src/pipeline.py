"""
Red Bricks Property Management - Azure Pipeline Automation & HITL Staging
Provides Stage 1 (Evaluation & HITL Draft Staging) and Stage 2 (Approved Dispatch)
for Azure Scheduled Pipelines with interactive HITL portal and Markdown summaries.
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from src.workflow import WorkflowOrchestrator
from src.reporting import ReportGenerator
from src.engine.excel_exporter import build_daily_excel_workbook, fetch_live_report_records
from src.engine.hitl_portal_generator import generate_hitl_portal_html
from src.models import DecisionOutcome, DailyRunSummary
from src.database import db
from src.config import settings

logger = logging.getLogger("PipelineRunner")


def run_pipeline_evaluate(output_dir: str = "reports") -> Dict[str, Any]:
    """
    PART 1: Evaluates portfolio, generates reports, and stages HITL drafts.
    Does NOT dispatch live SMS.
    Outputs:
      - RBPM_Daily_Rent_Report.xlsx
      - hitl_portal.html (Interactive standalone UI for review & 1-click approve/reject)
      - hitl_pending_reminders.json
      - hitl_summary.md (Uploaded to Azure DevOps Run Summary)
      - daily_rent_report.html
      - daily_rent_report.md
      - summary.json
    """
    os.makedirs(output_dir, exist_ok=True)
    logger.info("=== Starting Pipeline Stage 1: Evaluation & HITL Staging ===")

    orchestrator = WorkflowOrchestrator()
    summary: DailyRunSummary = orchestrator.execute_daily_run(is_dry_run=True)

    # 1. Generate Executive Excel Workbook
    excel_wb = build_daily_excel_workbook()
    excel_filename = "RBPM_Daily_Rent_Report.xlsx"
    excel_path = os.path.join(output_dir, excel_filename)
    excel_wb.save(excel_path)
    logger.info(f"Excel report saved: {excel_path}")

    # 2. Extract Pending Reminders for HITL Review (Grouped per Lease / Property)
    pending_reminders: List[Dict[str, Any]] = []
    seen_leases = set()

    for d in summary.decisions:
        if d.outcome == DecisionOutcome.SEND_REMINDER:
            details = d.details or {}
            lid = str(details.get("lease_id") or d.tenant_id)

            # Avoid duplicate rows for co-tenants of the same lease
            if lid in seen_leases and details.get("is_group_reminder") and d.balance <= 0.0:
                continue
            seen_leases.add(lid)

            sms_draft = details.get("group_sms_text") or details.get("sms_text") or d.sms_draft or ""
            if not sms_draft:
                rec_names = ", ".join(details.get("tenant_names_in_sms") or [d.tenant_name])
                sms_draft = settings.GROUP_REMINDER_TEMPLATE.format(
                    tenant_name=rec_names,
                    balance=d.balance,
                    property_name=d.property_unit,
                    unit_number="",
                    due_date=details.get("due_date", "the 1st of the month")
                )

            phones = details.get("phones_sent") or details.get("all_phones") or details.get("phone") or []
            if isinstance(phones, str):
                phones = [p.strip() for p in phones.split(",") if p.strip()]

            pending_reminders.append({
                "decision_id": f"hitl_{len(pending_reminders)+1}",
                "lease_id": lid,
                "tenant_id": d.tenant_id,
                "tenant_name": ", ".join(details.get("tenant_names_in_sms") or [d.tenant_name]),
                "property_unit": d.property_unit,
                "balance": d.balance,
                "due_date": getattr(d, "due_date", None) or details.get("due_date", "Past Due"),
                "recipients": details.get("tenant_names_in_sms") or details.get("recipients") or [d.tenant_name],
                "recipient_phones": phones,
                "draft_sms_text": sms_draft.strip(),
                "sender_phone": details.get("sender_phone", settings.QUO_PHONE_TENANTS_FORMATTED),
                "reason": d.reason,
                "action": "SEND REMINDER",
                "status": "AWAITING_HITL_APPROVAL",
                "staged_at": datetime.utcnow().isoformat()
            })

    # Collect Promises on File (Hold)
    promises_on_file = [
        {
            "tenant_name": d.tenant_name,
            "property_unit": d.property_unit,
            "balance": d.balance,
            "due_date": (d.details or {}).get("due_date", "Promised"),
            "reason": d.reason
        }
        for d in summary.decisions if d.outcome == DecisionOutcome.HOLD_PROMISE
    ]

    # Collect Manual Reviews & Disputes
    manual_reviews = [
        {
            "tenant_name": d.tenant_name,
            "property_unit": d.property_unit,
            "balance": d.balance,
            "reason": d.reason
        }
        for d in summary.decisions if d.outcome == DecisionOutcome.FLAG_MANUAL_REVIEW
    ]

    # Collect Paid & Cleared
    paid_cleared = [
        {
            "tenant_name": d.tenant_name,
            "property_unit": d.property_unit,
            "balance": 0.0
        }
        for d in summary.decisions if d.outcome == DecisionOutcome.SKIP_PAID
    ]

    # 3. Save JSON files
    hitl_path = os.path.join(output_dir, "hitl_pending_reminders.json")
    with open(hitl_path, "w", encoding="utf-8") as f:
        json.dump(pending_reminders, f, indent=2, ensure_ascii=False)
    logger.info(f"HITL pending reminders staged: {hitl_path} ({len(pending_reminders)} items)")

    # 4. Generate Interactive Standalone HITL Portal (UI with individual Approve/Reject toggles)
    overdue_bal = getattr(summary, "total_balance_unpaid", 0.0)
    summary_metrics = {
        "run_id": summary.run_id,
        "completed_at": summary.completed_at,
        "total_active_leases": summary.total_tenants,
        "reminders_awaiting_hitl": len(pending_reminders),
        "promises_on_file": summary.count_promised,
        "paid_cleared": summary.count_paid,
        "manual_review_disputes": getattr(summary, "count_flagged", 0),
        "in_cooldown_grace": summary.count_skipped,
        "total_overdue_balance": overdue_bal
    }

    portal_html = generate_hitl_portal_html(
        pending_reminders=pending_reminders,
        promises_on_file=promises_on_file,
        manual_reviews=manual_reviews,
        paid_cleared=paid_cleared,
        summary_metrics=summary_metrics
    )
    portal_path = os.path.join(output_dir, "hitl_portal.html")
    with open(portal_path, "w", encoding="utf-8") as f:
        f.write(portal_html)
    logger.info(f"Interactive HITL Review Portal generated: {portal_path}")

    # Also sync a copy to web assets directory so http://localhost:8000/hitl.html works
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    if os.path.exists(web_dir):
        with open(os.path.join(web_dir, "hitl.html"), "w", encoding="utf-8") as f:
            f.write(portal_html)

    # 5. Generate Standard Reports (HTML & Markdown)
    html_content = ReportGenerator.generate_html(summary)
    with open(os.path.join(output_dir, "daily_rent_report.html"), "w", encoding="utf-8") as f:
        f.write(html_content)

    md_content = ReportGenerator.generate_markdown(summary)
    with open(os.path.join(output_dir, "daily_rent_report.md"), "w", encoding="utf-8") as f:
        f.write(md_content)

    # 6. Generate Rich Markdown Approval Summary for Azure DevOps Run Overview
    total_staged_balance = sum(r["balance"] for r in pending_reminders)
    summary_md_lines = [
        "# 🏢 Red Bricks Property Management — Daily HITL Reminders Approval",
        "",
        f"**Run ID:** `{summary.run_id}` | **Generated:** `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC`",
        "",
        "### 📊 Portfolio Executive Overview",
        f"- **Total Active Portfolio Leases:** `{summary.total_tenants}`",
        f"- **Leases Awaiting HITL Approval:** `{len(pending_reminders)}`",
        f"- **Total Overdue Balance to Remind:** `${total_staged_balance:,.2f}`",
        f"- **Payment Promises on File (Held):** `{summary.count_promised}`",
        f"- **Paid / Cleared ($0.00 Due):** `{summary.count_paid}`",
        f"- **Manual Review / Disputes:** `{getattr(summary, 'count_flagged', 0)}`",
        "",
        "---",
        "",
        "## 📋 Review Pending Tenants & Drafted SMS Messages",
        "",
        "The following table displays every tenant eligible for a reminder, their contact numbers, overdue balance, and the exact drafted SMS message ready for dispatch.",
        "",
        "| # | Tenant Name(s) | Property & Unit | Balance | Recipient Phone(s) | Recommended Action | Exact Drafted SMS Message |",
        "|---|---|---|---|---|:---:|---|"
    ]

    for idx, r in enumerate(pending_reminders, start=1):
        phones_str = "<br>".join(r["recipient_phones"]) if r["recipient_phones"] else "No phone"
        escaped_sms = r["draft_sms_text"].replace("|", "\\|").replace("\n\n", "<br><br>").replace("\n", "<br>")
        summary_md_lines.append(
            f"| {idx} | **{r['tenant_name']}** | {r['property_unit']} | **${r['balance']:,.2f}** | `{phones_str}` | <span style=\"color:#ef4444;font-weight:bold;\">SEND REMINDER</span> | <pre>{escaped_sms}</pre> |"
        )

    summary_md_lines.extend([
        "",
        "---",
        "",
        "### 🎯 How to Approve or Exclude in Azure DevOps (Stage 2)",
        "",
        "1. **Interactive UI Review Portal:** Open `hitl_portal.html` from the **`RentReminderReports`** build artifact for a 1-click **[Approve] / [Reject]** toggle UI.",
        "2. **To Approve and Send ALL Reminders:** Click the **Review** button on the `Approve_And_Dispatch_HITL` stage → Click **Approve**.",
        "3. **To Exclude Specific Tenant(s):** Pass their name or address in the `exclude_tenants` parameter (e.g. `Gary Owens`).",
        "4. **To Reject All:** Click **Reject**.",
        "",
        "*Report automatically compiled by Red Bricks AI Engine.*"
    ])

    hitl_summary_path = os.path.join(output_dir, "hitl_summary.md")
    with open(hitl_summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_md_lines))
    logger.info(f"HITL summary markdown written: {hitl_summary_path}")

    # 7. Write summary.json
    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_metrics, f, indent=2)

    # Print executive banner
    print("\n" + "=" * 70)
    print("  AZURE PIPELINE PART 1: EVALUATION & HITL STAGING COMPLETE")
    print("=" * 70)
    print(f"  Run ID:                   {summary.run_id}")
    print(f"  Total Active Leases:      {summary.total_tenants}")
    print(f"  Reminders Awaiting HITL:  {len(pending_reminders)}")
    print(f"  Promises on File (Held):  {summary.count_promised}")
    print(f"  Paid / Cleared:           {summary.count_paid}")
    print(f"  Manual Review / Disputes: {getattr(summary, 'count_flagged', 0)}")
    print(f"  In Cooldown / Grace:      {summary.count_skipped}")
    print(f"  Total Overdue Balance:    ${total_staged_balance:,.2f}")
    print("-" * 70)
    print(f"  Artifacts saved to:       {os.path.abspath(output_dir)}")
    print("  - hitl_portal.html            (Interactive Approve/Reject UI)")
    print("  - RBPM_Daily_Rent_Report.xlsx (7 Category Worksheets)")
    print("  - hitl_summary.md             (Uploaded to Azure DevOps Summary)")
    print("  - hitl_pending_reminders.json (Pending Reminders for HITL)")
    print("=" * 70)
    print("  STATUS: Pausing at HITL Gate. Open hitl_portal.html or Review Table to approve.")
    print("=" * 70 + "\n")

    return summary_metrics


def _get_lease_member_ids(tenant_id: Optional[str], lease_id: Optional[str]) -> List[str]:
    """Returns all tenant IDs belonging to the same RentVine lease (for group cooldown)."""
    member_ids: List[str] = []
    if tenant_id:
        member_ids.append(str(tenant_id))
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            resolved_lid = str(lease_id).strip() if lease_id else ""
            if resolved_lid.startswith("rv_lease_"):
                alt_lid = resolved_lid.replace("rv_lease_", "", 1)
            else:
                alt_lid = resolved_lid
            if not resolved_lid and tenant_id:
                cursor.execute("SELECT lease_id FROM tenants WHERE id = ?", (tenant_id,))
                row = cursor.fetchone()
                if row and row["lease_id"]:
                    resolved_lid = str(row["lease_id"]).strip()
                    alt_lid = resolved_lid
            candidates = [c for c in {resolved_lid, alt_lid} if c and not c.startswith("__solo_")]
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                cursor.execute(f"SELECT id FROM tenants WHERE lease_id IN ({placeholders})", candidates)
                for r in cursor.fetchall():
                    if r["id"] and r["id"] not in member_ids:
                        member_ids.append(r["id"])
    except Exception as e:
        logger.warning(f"Could not resolve co-tenants for lease {lease_id}: {e}")
    return member_ids


def run_pipeline_dispatch(
    input_file: Optional[str] = None,
    output_dir: str = "receipts",
    exclude_tenants: Optional[str] = None,
    approve_tenants: Optional[str] = None,
    reconcile: bool = True,
) -> Dict[str, Any]:
    """
    PART 2: Dispatches ONLY the HITL-approved reminders via Quo API, verifies delivery,
    records actual dispatch timestamps & 24h lease cooldowns ONLY on confirmed sends,
    performs Final Reconciliation against latest RentVine balances, and generates
    the Final Excel Report.
    """
    os.makedirs(output_dir, exist_ok=True)
    logger.info("=== Starting Pipeline Stage 2: Approved Reminders Dispatch & Verification ===")

    # Parse exclusions if provided
    excluded_names = set()
    if exclude_tenants:
        for item in str(exclude_tenants).split(","):
            cleaned = item.strip().lower()
            if cleaned and cleaned not in ("none", "null", "empty", "false", '""', "''"):
                excluded_names.add(cleaned)

    # Parse explicit allowlist if provided
    approved_filter = set()
    if approve_tenants:
        for item in str(approve_tenants).split(","):
            cleaned = item.strip().lower()
            if cleaned and cleaned not in ("none", "null", "empty", "false", "all", '""', "''"):
                approved_filter.add(cleaned)

    # Load HITL payload / staged reminders
    rejected_from_payload: List[Dict[str, Any]] = []
    if input_file and os.path.exists(input_file):
        with open(input_file, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    else:
        default_hitl = os.path.join("reports", "hitl_pending_reminders.json")
        if os.path.exists(default_hitl):
            with open(default_hitl, "r", encoding="utf-8") as f:
                raw_payload = json.load(f)
        else:
            logger.warning("No pending HITL file found. Running final reconciliation and report directly.")
            raw_payload = []

    if isinstance(raw_payload, dict):
        pending_items = raw_payload.get("approved_tenants", [])
        rejected_from_payload = raw_payload.get("rejected_tenants", []) or []
    else:
        pending_items = raw_payload or []

    orchestrator = WorkflowOrchestrator()
    dispatch_results: List[Dict[str, Any]] = []
    now_iso = datetime.utcnow().isoformat()

    total_staged = len(pending_items) + len(rejected_from_payload)
    print("\n" + "=" * 70)
    print(f"  AZURE PIPELINE PART 2: DISPATCHING APPROVED REMINDERS ({len(pending_items)} Approved / {len(rejected_from_payload)} Rejected in HITL)")
    if approved_filter:
        print(f"  ALLOWLIST ACTIVE: Only dispatching tenants matching {approved_filter}")
    if excluded_names:
        print(f"  EXCLUSIONS ACTIVE: Skipping tenants matching {excluded_names}")
    print("=" * 70)

    # Track explicitly rejected items from HITL Portal so they appear accurately in the Final Report
    # and NEVER receive SMS or enter cooldown.
    for rej in rejected_from_payload:
        dispatch_results.append({
            "lease_id": str(rej.get("lease_id") or ""),
            "tenant_id": rej.get("tenant_id"),
            "tenant_name": rej.get("tenant_name", ""),
            "property_unit": rej.get("property_unit", ""),
            "balance": float(rej.get("balance") or 0.0),
            "phones": rej.get("recipient_phones") or [],
            "status": "SKIPPED_BY_OPERATOR",
            "cooldown_applied": False,
            "reason": "Rejected by operator in HITL review portal — no SMS sent, no cooldown applied",
        })

    for item in pending_items:
        tid = item.get("tenant_id")
        t_name = item.get("tenant_name", "")
        p_unit = item.get("property_unit", "")
        lid = str(item.get("lease_id", ""))
        phones = item.get("recipient_phones") or []
        if isinstance(phones, str):
            phones = [p.strip() for p in phones.split(",") if p.strip()]

        sms_text = item.get("draft_sms_text")
        bal = float(item.get("balance") or 0.0)

        # Check allowlist (if set) & operator exclusions
        in_allowlist = True
        if approved_filter:
            in_allowlist = any(
                inc in t_name.lower() or inc in p_unit.lower() or inc in lid.lower() or inc == str(tid or "").lower()
                for inc in approved_filter
            )

        is_excluded = any(
            exc in t_name.lower() or exc in p_unit.lower() or exc in lid.lower()
            for exc in excluded_names
        )

        if not in_allowlist or is_excluded:
            skip_reason = (
                "Explicitly excluded by human operator during HITL review"
                if is_excluded
                else "Not selected in HITL approved_tenants list"
            )
            print(f"  [SKIPPED BY OPERATOR] {t_name} ({p_unit})")
            dispatch_results.append({
                "lease_id": lid,
                "tenant_id": tid,
                "tenant_name": t_name,
                "property_unit": p_unit,
                "balance": bal,
                "phones": phones,
                "status": "SKIPPED_BY_OPERATOR",
                "cooldown_applied": False,
                "reason": skip_reason,
            })
            continue

        if not phones or not sms_text:
            err_msg = "Missing recipient phone number(s) or SMS draft text"
            print(f"  [FAILED] {t_name}: {err_msg}")
            dispatch_results.append({
                "lease_id": lid,
                "tenant_id": tid,
                "tenant_name": t_name,
                "property_unit": p_unit,
                "balance": bal,
                "phones": phones,
                "status": "FAILED",
                "cooldown_applied": False,
                "error": err_msg,
            })
            continue

        # Determine reminder type & stage (normal conversations must never match RENT_REMINDER%)
        is_escalation = "10-day notice" in sms_text.lower() or "stage 2" in str(item.get("reason", "")).lower()
        rem_type = "RENT_REMINDER_STAGE_2_ESCALATION" if is_escalation else "RENT_REMINDER"
        rem_stage = 2 if is_escalation else 1

        # Dispatch via Quo client & verify delivery before recording cooldown
        try:
            send_res = orchestrator.quo.send_sms(
                tenant_id=tid,
                to_number=phones,
                text=sms_text,
                recipient_type="tenant",
                is_workflow_reminder=True,
                reminder_type=rem_type,
            )
            sms_msg_id = (send_res or {}).get("message_id") or f"msg_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            verify_res = orchestrator.quo.verify_message_delivery(str(sms_msg_id))

            if not verify_res.get("verified", True):
                fail_err = verify_res.get("error") or f"Quo verification reported status: {verify_res.get('status')}"
                logger.error(f"[VERIFICATION FAILED] {t_name}: {fail_err}")
                dispatch_results.append({
                    "lease_id": lid,
                    "tenant_id": tid,
                    "tenant_name": t_name,
                    "property_unit": p_unit,
                    "balance": bal,
                    "phones": phones,
                    "message_id": str(sms_msg_id),
                    "status": "FAILED",
                    "delivery_status": verify_res.get("status", "FAILED"),
                    "cooldown_applied": False,
                    "error": fail_err,
                })
                print(f"  [FAILED VERIFICATION] {t_name}: {fail_err}")
                continue

            actual_sent_at = verify_res.get("sent_at") or (send_res or {}).get("timestamp") or datetime.utcnow().isoformat()
            delivery_status = verify_res.get("status") or (send_res or {}).get("status") or "DELIVERED"
            # Normalize accepted/sent to SQLite cooldown-compatible status
            db_delivery_status = "DELIVERED" if delivery_status in ("ACCEPTED", "SENT", "QUEUED", "DELIVERED", "DELIVERED_MOCK") else delivery_status

            # Apply 24h cooldown from actual dispatch time across the ENTIRE lease and all co-tenants
            lease_member_ids = _get_lease_member_ids(tid, lid)
            ts_tag = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            for member_tid in lease_member_ids:
                for p in phones:
                    p_suffix = "".join(c for c in str(p) if c.isdigit())[-4:] or "0000"
                    rem_id = f"rem_{member_tid}_{ts_tag}_{p_suffix}"
                    try:
                        db.record_workflow_reminder(
                            reminder_id=rem_id,
                            tenant_id=member_tid,
                            message_id=str(sms_msg_id),
                            phone=p,
                            content=sms_text,
                            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
                            delivery_status=db_delivery_status,
                            sent_at=actual_sent_at,
                            workflow_id="HITL_DISPATCH",
                            reminder_type=rem_type,
                            stage=rem_stage,
                        )
                    except Exception as db_err:
                        logger.warning(f"Could not record workflow reminder to db for {member_tid}: {db_err}")

            dispatch_results.append({
                "lease_id": lid,
                "tenant_id": tid,
                "tenant_ids_covered": lease_member_ids,
                "tenant_name": t_name,
                "property_unit": p_unit,
                "balance": bal,
                "phones": phones,
                "message_id": str(sms_msg_id),
                "status": "DISPATCHED",
                "delivery_status": delivery_status,
                "verified": True,
                "cooldown_applied": True,
                "sent_at": actual_sent_at,
            })
            print(f"  [DISPATCHED & VERIFIED] {t_name} ({', '.join(phones)}) - Balance: ${bal:,.2f} - Sent: {actual_sent_at}")
        except Exception as e:
            logger.error(f"Failed to dispatch to {t_name}: {e}")
            dispatch_results.append({
                "lease_id": lid,
                "tenant_id": tid,
                "tenant_name": t_name,
                "property_unit": p_unit,
                "balance": bal,
                "phones": phones,
                "status": "FAILED",
                "cooldown_applied": False,
                "error": str(e),
            })
            print(f"  [FAILED] {t_name}: {e}")

    receipts_path = os.path.join(output_dir, "dispatch_receipt.json")
    receipt_data = {
        "dispatched_at": now_iso,
        "total_staged": total_staged,
        "successfully_sent": len([r for r in dispatch_results if r["status"] == "DISPATCHED"]),
        "skipped_by_operator": len([r for r in dispatch_results if r["status"] == "SKIPPED_BY_OPERATOR"]),
        "failed": len([r for r in dispatch_results if r["status"] == "FAILED"]),
        "results": dispatch_results,
    }
    with open(receipts_path, "w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2)

    print("=" * 70)
    print(f"  DISPATCH COMPLETE: {receipt_data['successfully_sent']} sent, {receipt_data['skipped_by_operator']} skipped/rejected, {receipt_data['failed']} failed")
    print(f"  Receipt saved: {os.path.abspath(receipts_path)}")
    print("=" * 70 + "\n")

    # ── Final Reconciliation & Final Excel Report Generation ──
    try:
        final_excel_path = run_pipeline_final_report(
            dispatch_results=dispatch_results,
            output_dir=output_dir,
            reconcile=reconcile,
        )
        receipt_data["final_report_path"] = final_excel_path
        receipt_data["final_report_filename"] = os.path.basename(final_excel_path)
        print(f"  Final Excel Report: {os.path.abspath(final_excel_path)}")
    except Exception as report_err:
        logger.error(f"Final report generation failed: {report_err}", exc_info=True)
        receipt_data["final_report_path"] = None
        receipt_data["final_report_error"] = str(report_err)

    return receipt_data


def run_pipeline_final_report(
    dispatch_results: Optional[List[Dict[str, Any]]] = None,
    output_dir: str = "receipts",
    reconcile: bool = True,
    receipt_file: Optional[str] = None,
) -> str:
    """
    Step 6 & 7: Performs Final Reconciliation (fetches latest payment statuses from RentVine,
    recalculates all 6 tenant categories with newly applied HITL cooldowns) and generates
    the authoritative Final Post-HITL Excel Report (.xlsx) and summary markdown.

    Returns the path to the generated Excel file.
    """
    os.makedirs(output_dir, exist_ok=True)
    logger.info("=== Stage 2 Final Reconciliation & Post-HITL Excel Report ===")

    # Load dispatch_results from receipt file if called standalone
    if dispatch_results is None and receipt_file and os.path.exists(receipt_file):
        with open(receipt_file, "r", encoding="utf-8") as f:
            rec_json = json.load(f)
            dispatch_results = rec_json.get("results", [])

    if reconcile:
        try:
            orchestrator = WorkflowOrchestrator()
            if not orchestrator.rentvine.mock_mode:
                logger.info("[Reconciliation] Fetching latest RentVine Report #72 payment statuses...")
                orchestrator.rentvine.sync_report_to_database(report_id=72)
            db.cleanup_invalid_cooldown_decisions()
            logger.info("[Reconciliation] Recalculating all tenant categories across portfolio (dry-run)...")
            orchestrator.execute_daily_run(is_dry_run=True)
        except Exception as rec_err:
            logger.warning(f"Final reconciliation step encountered a warning (continuing with DB state): {rec_err}")

    # Build lookup index from dispatch results
    dispatched_by_key: Dict[str, Dict[str, Any]] = {}
    skipped_by_key: Dict[str, str] = {}
    failed_by_key: Dict[str, str] = {}

    def _register_keys(r: Dict[str, Any], target_dict: Dict[str, Any], val: Any):
        for k in (r.get("lease_id"), r.get("tenant_id")):
            if k:
                sk = str(k).strip()
                target_dict[sk] = val
                if sk.startswith("rv_lease_"):
                    target_dict[sk.replace("rv_lease_", "", 1)] = val
                else:
                    target_dict[f"rv_lease_{sk}"] = val

    if dispatch_results:
        for r in dispatch_results:
            status = r.get("status", "")
            if status == "DISPATCHED":
                _register_keys(r, dispatched_by_key, r)
            elif status in ("SKIPPED_BY_OPERATOR", "REJECTED_IN_HITL"):
                _register_keys(r, skipped_by_key, r.get("reason", "Rejected / skipped in HITL"))
            elif status == "FAILED":
                _register_keys(r, failed_by_key, r.get("error", "Quo SMS dispatch failed"))

    # Fetch live reconciled records from DB (now includes cooldown for dispatched leases & latest RentVine payments)
    data_records = fetch_live_report_records()

    # Overlay exact dispatch outcome per lease
    for rec in data_records:
        keys = [str(rec.get("lease_id") or "").strip()] + [str(t).strip() for t in (rec.get("tenant_ids") or [])]
        matched_disp = next((dispatched_by_key[k] for k in keys if k and k in dispatched_by_key), None)
        matched_skip = next((skipped_by_key[k] for k in keys if k and k in skipped_by_key), None)
        matched_fail = next((failed_by_key[k] for k in keys if k and k in failed_by_key), None)

        if matched_disp:
            rec["this_run_dispatch_status"] = "✅ SENT THIS RUN"
            rec["this_run_sent_at"] = matched_disp.get("sent_at", "—")
            rec["this_run_cooldown"] = "Yes (24h)" if matched_disp.get("cooldown_applied", True) else "No"
            rec["this_run_note"] = f"Msg ID: {matched_disp.get('message_id', '—')} | Verified: {matched_disp.get('delivery_status', 'SENT')}"
        elif matched_skip is not None:
            rec["this_run_dispatch_status"] = "⛔ REJECTED (HITL)"
            rec["this_run_sent_at"] = "—"
            rec["this_run_cooldown"] = "No (Rejected)"
            rec["this_run_note"] = matched_skip
        elif matched_fail is not None:
            rec["this_run_dispatch_status"] = "❌ FAILED"
            rec["this_run_sent_at"] = "—"
            rec["this_run_cooldown"] = "No (Failed)"
            rec["this_run_note"] = matched_fail
        else:
            rec["this_run_dispatch_status"] = "—"
            rec["this_run_sent_at"] = "—"
            rec["this_run_cooldown"] = "Yes (Prior 24h)" if rec.get("is_in_cooldown") else "No"
            rec["this_run_note"] = ""

    # Build Final Excel Workbook with Summary, 6 Category Worksheets, and Dispatch Audit sheet
    wb = _build_final_excel_workbook(data_records, dispatch_results or [])

    now_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    final_filename = f"RBPM_Final_Report_{now_ts}.xlsx"
    final_path = os.path.join(output_dir, final_filename)
    wb.save(final_path)

    # Also write a Markdown summary for Azure DevOps Run Overview
    dispatched_list = [r for r in (dispatch_results or []) if r.get("status") == "DISPATCHED"]
    skipped_list = [r for r in (dispatch_results or []) if r.get("status") == "SKIPPED_BY_OPERATOR"]
    failed_list = [r for r in (dispatch_results or []) if r.get("status") == "FAILED"]
    active_recs = [r for r in data_records if r.get("lease_status") != "COMPLETED"]

    cat_counts = {
        "All Active": len(active_recs),
        "📩 Reminders Needed": len([r for r in active_recs if r.get("ai_outcome") == "SEND_REMINDER"]),
        "🛡️ Promises on File": len([r for r in active_recs if r.get("ai_outcome") == "HOLD_PROMISE"]),
        "✅ Paid / Cleared": len([r for r in active_recs if r.get("ai_outcome") == "SKIP_PAID"]),
        "🔴 Manual Review / Disputes": len([r for r in active_recs if r.get("ai_outcome") == "FLAG_MANUAL_REVIEW"]),
        "🚫 In Cooldown / Grace": len([r for r in active_recs if r.get("ai_outcome") in ("SKIP_ALREADY_REMINDED", "SKIP_GRACE_PERIOD")]),
    }

    md_lines = [
        "# 📊 Red Bricks Property Management — Final Post-HITL Reconciliation Report",
        "",
        f"**Generated:** `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC` | **Excel Artifact:** `{final_filename}`",
        "",
        "### 🚀 Dispatch Execution Summary",
        f"- **Approved & Successfully Dispatched (Quo Verified):** `{len(dispatched_list)}`",
        f"- **Rejected / Skipped in HITL (No SMS, No Cooldown):** `{len(skipped_list)}`",
        f"- **Failed Dispatches (No Cooldown):** `{len(failed_list)}`",
        "",
        "### 📂 Final Reconciled Category Counts",
    ]
    for k, v in cat_counts.items():
        md_lines.append(f"- **{k}:** `{v}`")

    with open(os.path.join(output_dir, "final_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    logger.info(
        f"Final Excel Report saved: {final_path} "
        f"({len(data_records)} leases, {len(dispatched_list)} dispatched this run)"
    )
    return final_path


def _build_final_excel_workbook(
    data_records: List[Dict[str, Any]],
    dispatch_results: List[Dict[str, Any]]
) -> "openpyxl.Workbook":
    """
    Builds the final post-dispatch Excel workbook.
    Reuses build_daily_excel_workbook(is_final_report=True) for Summary & the 6 category sheets,
    then appends a '🚀 This Run — Dispatch' audit worksheet.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = build_daily_excel_workbook(
        data_records=data_records,
        dispatch_results=dispatch_results,
        is_final_report=True,
    )

    font_family = "Segoe UI"
    title_font = Font(name=font_family, size=16, bold=True, color="0F172A")
    subtitle_font = Font(name=font_family, size=11, color="475569")
    header_font = Font(name=font_family, size=10.5, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    data_font = Font(name=font_family, size=10, color="0F172A")
    thin_side = Side(border_style="thin", color="CBD5E1")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    sent_fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
    sent_font = Font(name=font_family, size=10, bold=True, color="065F46")
    skip_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    skip_font = Font(name=font_family, size=10, bold=True, color="92400E")
    fail_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    fail_font = Font(name=font_family, size=10, bold=True, color="991B1B")

    now_utc = datetime.utcnow()
    gen_time_str = now_utc.strftime("%Y-%m-%d %I:%M:%S %p UTC")

    dispatched = [r for r in dispatch_results if r.get("status") == "DISPATCHED"]
    skipped = [r for r in dispatch_results if r.get("status") == "SKIPPED_BY_OPERATOR"]
    failed = [r for r in dispatch_results if r.get("status") == "FAILED"]

    # Insert "🚀 This Run — Dispatch" audit sheet after Summary (index=1)
    ws = wb.create_sheet(title="🚀 This Run — Dispatch", index=1)
    ws.sheet_view.showGridLines = True
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 32
    ws.column_dimensions["E"].width = 16
    ws.column_dimensions["F"].width = 24
    ws.column_dimensions["G"].width = 22
    ws.column_dimensions["H"].width = 20
    ws.column_dimensions["I"].width = 44

    ws.merge_cells("B2:I2")
    c = ws.cell(row=2, column=2, value="Red Bricks Property Management — Post-HITL Dispatch Audit")
    c.font = title_font
    c.alignment = Alignment(horizontal="left", vertical="center")

    ws.merge_cells("B3:I3")
    c = ws.cell(row=3, column=2, value=f"Final Post-Dispatch & Verification Log  |  Generated: {gen_time_str}")
    c.font = subtitle_font
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[2].height = 28
    ws.row_dimensions[3].height = 18

    ws.row_dimensions[5].height = 20
    stats = [
        (3, f"Total Staged: {len(dispatch_results)}", "0F172A"),
        (4, f"✅ Sent & Verified: {len(dispatched)}", "065F46"),
        (6, f"⛔ Rejected (HITL): {len(skipped)}", "92400E"),
        (8, f"❌ Failed: {len(failed)}", "991B1B"),
    ]
    for col_idx, val, color in stats:
        c = ws.cell(row=5, column=col_idx, value=val)
        c.font = Font(name=font_family, size=11, bold=True, color=color)

    headers = ["#", "Tenant Name", "Property & Unit", "Balance", "Phones", "Dispatch Status", "24h Cooldown", "Actual Sent At / Audit Note"]
    ws.row_dimensions[7].height = 24
    for ci, h in enumerate(headers, start=2):
        c = ws.cell(row=7, column=ci, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = cell_border

    for row_idx, item in enumerate(dispatch_results, start=1):
        excel_row = 7 + row_idx
        ws.row_dimensions[excel_row].height = 20
        status = item.get("status", "")
        if status == "DISPATCHED":
            row_fill = sent_fill
            status_disp = "✅ SENT"
            status_font = sent_font
            cool_val = "Yes (24h)" if item.get("cooldown_applied", True) else "No"
            note_val = f"{item.get('sent_at', '—')} (Msg: {item.get('message_id', '—')})"
        elif status == "SKIPPED_BY_OPERATOR":
            row_fill = skip_fill
            status_disp = "⛔ REJECTED"
            status_font = skip_font
            cool_val = "No"
            note_val = item.get("reason", "Rejected by operator in HITL review")
        else:
            row_fill = fail_fill
            status_disp = "❌ FAILED"
            status_font = fail_font
            cool_val = "No"
            note_val = item.get("error", "Unknown error")

        phones_str = ", ".join(item.get("phones") or [])
        bal = item.get("balance", 0.0)
        values = [
            row_idx,
            item.get("tenant_name", ""),
            item.get("property_unit", ""),
            f"${float(bal):,.2f}" if bal else "—",
            phones_str,
            status_disp,
            cool_val,
            note_val,
        ]
        for ci, val in enumerate(values, start=2):
            c = ws.cell(row=excel_row, column=ci, value=val)
            c.fill = row_fill if ci == 7 else (zebra_fill if row_idx % 2 == 0 else white_fill)
            c.border = cell_border
            if ci == 7:
                c.font = status_font
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif ci in (2, 8):
                c.font = data_font
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.font = data_font
                c.alignment = Alignment(horizontal="left", vertical="center")

    if dispatch_results:
        last_row = 7 + len(dispatch_results)
        ws.auto_filter.ref = f"B7:I{last_row}"

    return wb
