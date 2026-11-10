"""
Red Bricks Property Management - Azure Pipeline Automation & HITL Staging
Provides Stage 1 (Evaluation & HITL Draft Staging) and Stage 2 (Approved Dispatch)
for Azure Scheduled Pipelines with comprehensive Markdown approval summaries.
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from src.workflow import WorkflowOrchestrator
from src.reporting import ReportGenerator
from src.engine.excel_exporter import build_daily_excel_workbook
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
      - daily_rent_report.html
      - daily_rent_report.md
      - hitl_pending_reminders.json
      - hitl_summary.md (Uploaded to Azure DevOps Run Summary)
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

    # 2. Generate Interactive HTML Report
    html_content = ReportGenerator.generate_html(summary)
    html_path = os.path.join(output_dir, "daily_rent_report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"HTML report saved: {html_path}")

    # 3. Generate Markdown Report
    md_content = ReportGenerator.generate_markdown(summary)
    md_path = os.path.join(output_dir, "daily_rent_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    logger.info(f"Markdown report saved: {md_path}")

    # 4. Extract Pending Reminders for HITL Review (Grouped per Lease / Property)
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
            # Fallback: if template text not saved in details, format cleanly
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

    hitl_path = os.path.join(output_dir, "hitl_pending_reminders.json")
    with open(hitl_path, "w", encoding="utf-8") as f:
        json.dump(pending_reminders, f, indent=2, ensure_ascii=False)
    logger.info(f"HITL pending reminders staged: {hitl_path} ({len(pending_reminders)} items)")

    # 5. Generate Rich Markdown Approval Summary for Azure DevOps Run Overview
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
        # Format message into single cell with markdown breaks
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
        "1. **To Approve and Send ALL Reminders:**",
        "   - Click the **Review** button on the `Approve_And_Dispatch_HITL` stage.",
        "   - Click **Approve**.",
        "   - All above SMS messages will be immediately delivered through the Quo API.",
        "",
        "2. **To Exclude Specific Tenant(s):**",
        "   - In the approval comments box, specify any tenant name or address to exclude (e.g., `Exclude: Gary Owens`).",
        "   - Or set the pipeline variable `EXCLUDE_TENANTS` before triggering.",
        "",
        "3. **To Reject / Cancel Dispatch:**",
        "   - Click **Reject**. No SMS messages will be sent.",
        "",
        "*Report automatically compiled by Red Bricks AI Engine.*"
    ])

    hitl_summary_path = os.path.join(output_dir, "hitl_summary.md")
    with open(hitl_summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_md_lines))
    logger.info(f"HITL summary markdown written: {hitl_summary_path}")

    # 6. Summary metrics
    overdue_bal = getattr(summary, "total_balance_unpaid", 0.0)
    metrics = {
        "run_id": summary.run_id,
        "completed_at": summary.completed_at,
        "total_active_leases": summary.total_tenants,
        "reminders_awaiting_hitl": len(pending_reminders),
        "promises_on_file": summary.count_promised,
        "paid_cleared": summary.count_paid,
        "manual_review_disputes": getattr(summary, "count_flagged", 0),
        "in_cooldown_grace": summary.count_skipped,
        "total_overdue_balance": overdue_bal,
        "artifacts": {
            "excel": excel_filename,
            "html": "daily_rent_report.html",
            "markdown": "daily_rent_report.md",
            "hitl_pending_json": "hitl_pending_reminders.json",
            "hitl_summary_md": "hitl_summary.md"
        }
    }
    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    # Print executive summary banner
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
    print("  - RBPM_Daily_Rent_Report.xlsx (7 Category Worksheets)")
    print("  - hitl_summary.md             (Uploaded to Azure DevOps Summary)")
    print("  - hitl_pending_reminders.json (Pending Reminders for HITL)")
    print("=" * 70)
    print("  STATUS: Pausing at HITL Gate. Review table on Azure DevOps Run page to approve.")
    print("=" * 70 + "\n")

    return metrics


def run_pipeline_dispatch(
    input_file: Optional[str] = None,
    output_dir: str = "receipts",
    exclude_tenants: Optional[str] = None
) -> Dict[str, Any]:
    """
    PART 2: Dispatches the approved HITL reminders via Quo API.
    Supports excluding specific tenants/leases if requested by operator.
    """
    os.makedirs(output_dir, exist_ok=True)
    logger.info("=== Starting Pipeline Stage 2: Approved Reminders Dispatch ===")

    # Parse exclusions if provided
    excluded_names = set()
    if exclude_tenants:
        for item in str(exclude_tenants).split(","):
            cleaned = item.strip().lower()
            if cleaned:
                excluded_names.add(cleaned)

    # Load approved reminders
    if input_file and os.path.exists(input_file):
        with open(input_file, "r", encoding="utf-8") as f:
            pending_items = json.load(f)
    else:
        default_hitl = os.path.join("reports", "hitl_pending_reminders.json")
        if os.path.exists(default_hitl):
            with open(default_hitl, "r", encoding="utf-8") as f:
                pending_items = json.load(f)
        else:
            logger.warning("No pending HITL file found. Running live workflow dispatch directly.")
            orchestrator = WorkflowOrchestrator()
            summary = orchestrator.execute_daily_run(is_dry_run=False)
            receipts_path = os.path.join(output_dir, "dispatch_receipt.json")
            with open(receipts_path, "w", encoding="utf-8") as f:
                json.dump({"run_id": summary.run_id, "sent": summary.count_reminded}, f, indent=2)
            return {"dispatched": summary.count_reminded}

    orchestrator = WorkflowOrchestrator()
    dispatch_results = []
    now_iso = datetime.utcnow().isoformat()

    print("\n" + "=" * 70)
    print(f"  AZURE PIPELINE PART 2: DISPATCHING APPROVED REMINDERS ({len(pending_items)} Total Staged)")
    if excluded_names:
        print(f"  EXCLUSIONS ACTIVE: Skipping tenants matching {excluded_names}")
    print("=" * 70)

    for item in pending_items:
        tid = item.get("tenant_id")
        t_name = item.get("tenant_name", "")
        p_unit = item.get("property_unit", "")
        lid = str(item.get("lease_id", ""))
        phones = item.get("recipient_phones") or []
        if isinstance(phones, str):
            phones = [p.strip() for p in phones.split(",") if p.strip()]

        sms_text = item.get("draft_sms_text")
        bal = item.get("balance", 0.0)

        # Check operator exclusions
        is_excluded = any(
            exc in t_name.lower() or exc in p_unit.lower() or exc in lid
            for exc in excluded_names
        )
        if is_excluded:
            print(f"  [SKIPPED BY OPERATOR] {t_name} ({p_unit})")
            dispatch_results.append({
                "tenant_id": tid,
                "tenant_name": t_name,
                "property_unit": p_unit,
                "status": "SKIPPED_BY_OPERATOR",
                "reason": "Explicitly excluded by human operator during HITL review"
            })
            continue

        # Dispatch via Quo client
        if phones and sms_text:
            try:
                send_res = orchestrator.quo.send_sms(
                    tenant_id=tid,
                    to_number=phones,
                    text=sms_text,
                    recipient_type="tenant"
                )
                db.record_reminder(
                    tenant_id=tid,
                    reminder_type="SMS_WORKFLOW_REMINDER",
                    recipient_phone=", ".join(phones),
                    sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
                    delivery_status="DELIVERED",
                    sent_at=now_iso,
                    stage=1
                )
                db.log_audit_event(
                    event_type="HITL_REMINDER_DISPATCHED",
                    tenant_id=tid,
                    details={
                        "recipients": phones,
                        "balance": bal,
                        "sms_text": sms_text,
                        "approved_by": "AZURE_HITL_OPERATOR"
                    }
                )
                dispatch_results.append({
                    "tenant_id": tid,
                    "tenant_name": t_name,
                    "property_unit": p_unit,
                    "phones": phones,
                    "status": "DISPATCHED",
                    "sent_at": now_iso
                })
                print(f"  [DISPATCHED] {t_name} ({', '.join(phones)}) - Balance: ${bal:,.2f}")
            except Exception as e:
                logger.error(f"Failed to dispatch to {t_name}: {e}")
                dispatch_results.append({
                    "tenant_id": tid,
                    "tenant_name": t_name,
                    "property_unit": p_unit,
                    "phones": phones,
                    "status": "FAILED",
                    "error": str(e)
                })
                print(f"  [FAILED] {t_name}: {e}")

    receipts_path = os.path.join(output_dir, "dispatch_receipt.json")
    receipt_data = {
        "dispatched_at": now_iso,
        "total_staged": len(pending_items),
        "successfully_sent": len([r for r in dispatch_results if r["status"] == "DISPATCHED"]),
        "skipped_by_operator": len([r for r in dispatch_results if r["status"] == "SKIPPED_BY_OPERATOR"]),
        "failed": len([r for r in dispatch_results if r["status"] == "FAILED"]),
        "results": dispatch_results
    }
    with open(receipts_path, "w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2)

    print("=" * 70)
    print(f"  DISPATCH COMPLETE: {receipt_data['successfully_sent']} sent, {receipt_data['skipped_by_operator']} skipped, {receipt_data['failed']} failed")
    print(f"  Receipt saved: {os.path.abspath(receipts_path)}")
    print("=" * 70 + "\n")

    return receipt_data
