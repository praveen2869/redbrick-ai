"""
Red Bricks Property Management - Excel Report Exporter
Generates well-organized, multi-tab executive Excel (.xlsx) workbooks matching
live dashboard categories, grouped by unique property/lease.
"""

import io
from datetime import datetime
from typing import List, Dict, Any, Optional

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from src.database import db
from src.config import settings


def fetch_live_report_records() -> List[Dict[str, Any]]:
    """
    Fetches the latest consolidated records directly from database,
    grouped by unique RentVine lease ID. Matches the live dashboard dataset.
    """
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                t.id, t.lease_id, t.first_name, t.last_name, t.phone, t.email,
                t.property_name, t.unit_number, t.is_excluded, t.exclusion_reason,
                r.balance, r.monthly_rent, r.due_date, r.last_payment_date,
                r.last_payment_amount, r.portal_link, r.lease_status, r.follow_up_notes
            FROM tenants t
            LEFT JOIN rentvine_records r ON t.id = r.tenant_id
            ORDER BY t.property_name, t.unit_number, t.id
        """)
        tenant_rows = cursor.fetchall()

        cursor.execute("SELECT * FROM communication_messages ORDER BY timestamp DESC")
        all_raw_messages = cursor.fetchall()

        cursor.execute("SELECT tenant_id, sent_at, reminder_type, delivery_status FROM workflow_reminders ORDER BY sent_at DESC")
        all_raw_reminders = cursor.fetchall()

    decisions = db.get_latest_decisions(limit=2000)
    latest_decision_by_tenant = {}
    for d in decisions:
        if d.tenant_id not in latest_decision_by_tenant:
            latest_decision_by_tenant[d.tenant_id] = {
                "outcome": d.outcome.value,
                "reason": d.reason,
                "timestamp": d.timestamp,
                "sms_sent": d.sms_sent,
                "note_written": d.note_written,
                "details": d.details or {}
            }

    reminders_by_tenant: Dict[str, Any] = {}
    for r in all_raw_reminders:
        tid = r["tenant_id"]
        if tid and tid not in reminders_by_tenant:
            reminders_by_tenant[tid] = r

    messages_by_tenant: Dict[str, list] = {}
    messages_by_phone: Dict[str, list] = {}
    for m in all_raw_messages:
        tid = m["tenant_id"]
        if tid:
            messages_by_tenant.setdefault(tid, []).append(m)
        sender = m["sender"] or ""
        clean_p = "".join(c for c in sender if c.isdigit())[-10:]
        if clean_p:
            messages_by_phone.setdefault(clean_p, []).append(m)

    now = datetime.utcnow()
    current_month_first = now.strftime("%Y-%m-01")

    # Group by unique RentVine lease ID
    grouped_leases: Dict[str, list] = {}
    for r in tenant_rows:
        lid = str(r["lease_id"]).strip() if r["lease_id"] else f"__solo_{r['id']}"
        grouped_leases.setdefault(lid, []).append(r)

    all_records = []
    for lid, group_rows in grouped_leases.items():
        # Primary tenant leads
        def _primary_sort_key(r):
            tid = str(r["id"])
            bal = float(r["balance"] or 0.0)
            return (not "_co" in tid, bal > 0)
        group_rows.sort(key=_primary_sort_key, reverse=True)
        primary_row = group_rows[0]

        # Combine all tenants on this lease (deduplicated by phone/name)
        tenants_list = []
        seen_tenants = set()
        for r in group_rows:
            clean_p = "".join(c for c in (r["phone"] or "") if c.isdigit())[-10:]
            t_key = clean_p if clean_p else f"{r['first_name']} {r['last_name']}".strip().lower()
            if t_key in seen_tenants:
                continue
            seen_tenants.add(t_key)
            tenants_list.append({
                "id": r["id"],
                "first_name": (r["first_name"] or "").strip(),
                "last_name": (r["last_name"] or "").strip(),
                "full_name": f"{(r['first_name'] or '').strip()} {(r['last_name'] or '').strip()}".strip(),
                "phone": (r["phone"] or "").strip(),
                "email": (r["email"] or "").strip()
            })

        all_names = [t["full_name"] for t in tenants_list if t["full_name"]]
        all_phones = [t["phone"] for t in tenants_list if t["phone"]]
        all_emails = [t["email"] for t in tenants_list if t["email"]]
        tenant_ids = [t["id"] for t in tenants_list]
        clean_lease_id = lid if not lid.startswith("__solo_") else primary_row["id"]

        # Financial data
        lease_bal = max(float(r["balance"] or 0.0) for r in group_rows)
        lease_rent = max(float(r["monthly_rent"] or 0.0) for r in group_rows)
        raw_due = next((r["due_date"] for r in group_rows if r["due_date"]), None)
        if lease_bal <= 0.0 and raw_due and raw_due < current_month_first:
            effective_due = current_month_first
        else:
            effective_due = raw_due or current_month_first

        lease_status = next((r["lease_status"] for r in group_rows if r["lease_status"]), "ACTIVE")
        all_notes_set = set(r["follow_up_notes"] for r in group_rows if r["follow_up_notes"])
        combined_notes = " | ".join(filter(None, all_notes_set))

        # Check cooldown across all tenant IDs on this lease
        is_cool, cool_last_rem, cool_h_elapsed, cool_h_rem, cool_exp_at = False, None, 0.0, 0.0, None
        if lease_bal > 0:
            for tid in tenant_ids:
                c_ok, c_rem, c_el, c_rm, c_exp = db.get_cooldown_status(tid, current_time=now, cooldown_hours=24.0)
                if c_ok:
                    is_cool, cool_last_rem, cool_h_elapsed, cool_h_rem, cool_exp_at = c_ok, c_rem, c_el, c_rm, c_exp
                    break

        promise_dec = next((latest_decision_by_tenant[tid] for tid in tenant_ids if latest_decision_by_tenant.get(tid) and latest_decision_by_tenant[tid].get("outcome") == "HOLD_PROMISE"), None)
        mr_dec = next((latest_decision_by_tenant[tid] for tid in tenant_ids if latest_decision_by_tenant.get(tid) and latest_decision_by_tenant[tid].get("outcome") == "FLAG_MANUAL_REVIEW"), None)
        grace_dec = next((latest_decision_by_tenant[tid] for tid in tenant_ids if latest_decision_by_tenant.get(tid) and latest_decision_by_tenant[tid].get("outcome") == "SKIP_GRACE_PERIOD"), None)
        is_group_excluded = any(bool(r["is_excluded"]) for r in group_rows)
        excl_reason = next((r["exclusion_reason"] for r in group_rows if r["exclusion_reason"]), "Excluded from automated reminders")
        has_mr_note = any(k in combined_notes.lower() for k in ["manual review", "dispute", "attorney", "eviction"])

        # Check monthly Days 1-4 global pause or grace period
        from src.engine.rules import BusinessRulesEngine, ReminderStage
        rules_eng = BusinessRulesEngine()
        cur_stage = rules_eng.get_reminder_stage(now)

        promise_date_str = ""
        reminder_sent_str = ""
        cooldown_exp_str = ""

        if lease_bal <= 0.0:
            outcome = "SKIP_PAID"
            reason = "Property balance $0.00 — rent paid/cleared"
        elif 0.0 < lease_bal < 200.0:
            outcome = "FLAG_MANUAL_REVIEW"
            reason = f"Total outstanding balance (${lease_bal:,.2f}) is greater than $0 but less than $200 — moved to Manual Review / Disputes for staff verification."
        elif is_cool and cool_last_rem:
            outcome = "SKIP_ALREADY_REMINDED"
            disp_exp = cool_exp_at.strftime('%Y-%m-%d %I:%M %p') if cool_exp_at else "in 24h"
            reason = f"Workflow rent reminder sent {cool_h_elapsed:.1f}h ago. Cooldown active for {cool_h_rem:.1f}h remaining (expires {disp_exp})."
            reminder_sent_str = cool_last_rem.timestamp
            cooldown_exp_str = disp_exp
        elif is_group_excluded or has_mr_note or mr_dec:
            outcome = "FLAG_MANUAL_REVIEW"
            if is_group_excluded:
                reason = f"Excluded / Manual Review: {excl_reason}"
            elif has_mr_note:
                reason = f"Flagged for manual review: {combined_notes}"
            else:
                reason = mr_dec.get("reason", "Flagged for manual review") if mr_dec else "Flagged for manual review"
        elif promise_dec:
            outcome = "HOLD_PROMISE"
            reason = promise_dec.get("reason", "Payment promise on file")
            promise_date_str = promise_dec.get("details", {}).get("promise_date", "") or ""
        elif grace_dec or cur_stage == ReminderStage.STAGE_PAUSE_GRACE.value:
            outcome = "SKIP_GRACE_PERIOD"
            reason = grace_dec.get("reason") if grace_dec else "Monthly Grace Period / Global Pause (Days 1-4) — no automated reminders"
        else:
            outcome = "SEND_REMINDER"
            reason = f"Unpaid balance ${lease_bal:,.2f} past grace period — reminder needed"

        # Check latest reminder sent timestamp from pre-fetched reminders mapping
        if not reminder_sent_str:
            for tid in tenant_ids:
                if tid in reminders_by_tenant:
                    reminder_sent_str = reminders_by_tenant[tid]["sent_at"]
                    break

        # Messages across all lease members
        lease_msgs = []
        seen_mids = set()
        for tid in tenant_ids:
            for m in messages_by_tenant.get(tid, []):
                if m["id"] not in seen_mids:
                    seen_mids.add(m["id"])
                    lease_msgs.append(m)
        for p in all_phones:
            clean_p = "".join(c for c in p if c.isdigit())[-10:]
            if clean_p:
                for m in messages_by_phone.get(clean_p, []):
                    if m["id"] not in seen_mids:
                        seen_mids.add(m["id"])
                        lease_msgs.append(m)

        latest_msg_str = "No messages"
        if lease_msgs:
            m = lease_msgs[0]
            direction = m["direction"]
            content = m["content"] or ""
            ts = (m["timestamp"] or "")[:16].replace("T", " ")
            latest_msg_str = f"[{direction}] {content} ({ts})"

        all_records.append({
            "lease_id": clean_lease_id,
            "tenant_ids": tenant_ids,
            "property_address": primary_row["property_name"] or "",
            "unit_number": primary_row["unit_number"] or "",
            "tenant_names": ", ".join(all_names),
            "phone_numbers": ", ".join(all_phones),
            "email_addresses": ", ".join(all_emails),
            "monthly_rent": lease_rent,
            "outstanding_balance": lease_bal,
            "due_date": effective_due,
            "latest_sms_reply": latest_msg_str,
            "payment_promise_date": promise_date_str or "—",
            "reminder_sent_timestamp": reminder_sent_str or "—",
            "cooldown_expiry": cooldown_exp_str or "—",
            "ai_outcome": outcome,
            "eligibility_reason": reason,
            "follow_up_notes": combined_notes or "None",
            "lease_status": lease_status,
            "is_in_cooldown": bool(is_cool),
        })

    return all_records


def build_daily_excel_workbook(
    data_records: Optional[List[Dict[str, Any]]] = None,
    dispatch_results: Optional[List[Dict[str, Any]]] = None,
    is_final_report: bool = False,
) -> openpyxl.Workbook:
    """
    Builds a professional, multi-tab Excel (.xlsx) workbook for the executive Daily/Final Report.
    Includes Summary tab and 6 categorized detail worksheets:
      - All Active
      - 📩 Reminders Needed
      - 🛡️ Promises on File
      - ✅ Paid & Cleared
      - 🔴 Manual Review & Disputes
      - 🚫 In Cooldown & Grace
    When `dispatch_results` is supplied (post-HITL Final Report), records reflect actual
    dispatch status, sent timestamps, cooldown activation, rejections, and failures.
    """
    if data_records is None:
        data_records = fetch_live_report_records()

    # Build lookup index from dispatch_results (by lease_id and tenant_id)
    dispatch_lookup: Dict[str, Dict[str, Any]] = {}
    for dr in (dispatch_results or []):
        for key in (dr.get("lease_id"), dr.get("tenant_id")):
            if key:
                skey = str(key).strip()
                dispatch_lookup[skey] = dr
                if skey.startswith("rv_lease_"):
                    dispatch_lookup[skey.replace("rv_lease_", "", 1)] = dr
                else:
                    dispatch_lookup[f"rv_lease_{skey}"] = dr

    for rec in data_records:
        dr = None
        candidates = [rec.get("lease_id")] + list(rec.get("tenant_ids") or [])
        for cand in candidates:
            if cand and str(cand).strip() in dispatch_lookup:
                dr = dispatch_lookup[str(cand).strip()]
                break
        if dr:
            st = dr.get("status", "")
            if st == "DISPATCHED":
                rec.setdefault("this_run_dispatch_status", "✅ SENT THIS RUN")
                rec.setdefault("this_run_sent_at", dr.get("sent_at") or rec.get("reminder_sent_timestamp") or "—")
                rec.setdefault("this_run_cooldown", "Yes (24h)" if dr.get("cooldown_applied", True) else "No")
                rec.setdefault("this_run_note", f"Msg ID: {dr.get('message_id', '—')} | Verified: {dr.get('delivery_status', 'SENT')}")
            elif st in ("SKIPPED_BY_OPERATOR", "REJECTED_IN_HITL"):
                rec.setdefault("this_run_dispatch_status", "⛔ REJECTED / SKIPPED (HITL)")
                rec.setdefault("this_run_sent_at", "—")
                rec.setdefault("this_run_cooldown", "No (Rejected)")
                rec.setdefault("this_run_note", dr.get("reason", "Rejected by operator during HITL review"))
            elif st == "FAILED":
                rec.setdefault("this_run_dispatch_status", "❌ DISPATCH FAILED")
                rec.setdefault("this_run_sent_at", "—")
                rec.setdefault("this_run_cooldown", "No (Failed)")
                rec.setdefault("this_run_note", dr.get("error", "Quo SMS dispatch failed"))
        else:
            rec.setdefault("this_run_dispatch_status", "—")
            rec.setdefault("this_run_sent_at", "—")
            rec.setdefault("this_run_cooldown", "Yes (Prior 24h)" if rec.get("is_in_cooldown") else "No")
            rec.setdefault("this_run_note", "")

    now_utc = datetime.utcnow()
    gen_time_str = now_utc.strftime("%Y-%m-%d %I:%M:%S %p UTC")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # Remove default blank sheet

    # Typography & Colors
    font_family = "Segoe UI"
    title_font = Font(name=font_family, size=16, bold=True, color="0F172A")
    subtitle_font = Font(name=font_family, size=11, color="475569")
    meta_font = Font(name=font_family, size=9.5, italic=True, color="64748B")

    header_font = Font(name=font_family, size=10.5, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")

    data_font = Font(name=font_family, size=10, color="0F172A")
    bold_data_font = Font(name=font_family, size=10, bold=True, color="0F172A")
    muted_data_font = Font(name=font_family, size=9.5, color="64748B")

    thin_border_side = Side(border_style="thin", color="CBD5E1")
    cell_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)

    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    # Outcome badge styles
    outcome_styles = {
        "SEND_REMINDER": {
            "fill": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
            "font": Font(name=font_family, size=10, bold=True, color="991B1B"),
            "label": "REMINDER NEEDED"
        },
        "HOLD_PROMISE": {
            "fill": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
            "font": Font(name=font_family, size=10, bold=True, color="92400E"),
            "label": "PROMISE (HOLD)"
        },
        "SKIP_PAID": {
            "fill": PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
            "font": Font(name=font_family, size=10, bold=True, color="065F46"),
            "label": "PAID (SKIP)"
        },
        "FLAG_MANUAL_REVIEW": {
            "fill": PatternFill(start_color="FCE7F3", end_color="FCE7F3", fill_type="solid"),
            "font": Font(name=font_family, size=10, bold=True, color="831843"),
            "label": "MANUAL REVIEW"
        },
        "SKIP_ALREADY_REMINDED": {
            "fill": PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid"),
            "font": Font(name=font_family, size=10, bold=True, color="334155"),
            "label": "IN COOLDOWN"
        },
        "SKIP_GRACE_PERIOD": {
            "fill": PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid"),
            "font": Font(name=font_family, size=10, bold=True, color="334155"),
            "label": "IN GRACE PERIOD"
        }
    }

    # Only include active (non-completed) leases
    active_records = [r for r in data_records if r.get("lease_status") != "COMPLETED"]

    # Six categories (Excel sheet names cannot contain '/' or '\\')
    cat_all_active = active_records
    cat_reminders = [r for r in active_records if r.get("ai_outcome") == "SEND_REMINDER"]
    cat_promises = [r for r in active_records if r.get("ai_outcome") == "HOLD_PROMISE"]
    cat_paid = [r for r in active_records if r.get("ai_outcome") == "SKIP_PAID"]
    cat_flagged = [r for r in active_records if r.get("ai_outcome") == "FLAG_MANUAL_REVIEW"]
    cat_cooldown = [r for r in active_records if r.get("ai_outcome") in ("SKIP_ALREADY_REMINDED", "SKIP_GRACE_PERIOD")]

    # ─────────────────────────────────────────────────────────────────────────
    # WORKSHEET 1: Summary
    # ─────────────────────────────────────────────────────────────────────────
    ws_sum = wb.create_sheet(title="Summary")
    ws_sum.views.sheetView[0].showGridLines = True

    # Header Banner
    ws_sum.cell(row=2, column=2, value="Red Bricks Property Management").font = title_font
    subtitle_txt = (
        "Final Post-HITL Rent Reminder Executive Report & Reconciliation"
        if (is_final_report or dispatch_results is not None)
        else "Daily Rent Reminder Executive Report & AI Outcomes"
    )
    ws_sum.cell(row=3, column=2, value=subtitle_txt).font = subtitle_font
    ws_sum.cell(row=4, column=2, value=f"Generated: {gen_time_str} | Authoritative Source: RentVine Report #72 & Quo Live SMS").font = meta_font

    # Summary table header
    sum_headers = ["#", "Category Name", "Worksheet Tab", "Properties / Leases", "Total Outstanding Balance", "Status & Policy Description"]
    for col_idx, h_text in enumerate(sum_headers, start=2):
        c = ws_sum.cell(row=6, column=col_idx, value=h_text)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center" if col_idx in (2, 4) else ("right" if col_idx in (5, 6) else "left"), vertical="center")
        c.border = cell_border
    ws_sum.row_dimensions[6].height = 26

    tab_all = "All Active"
    tab_reminders = "📩 Reminders Needed"
    tab_promises = "🛡️ Promises on File"
    tab_paid = "✅ Paid & Cleared"
    tab_flagged = "🔴 Manual Review & Disputes"
    tab_cooldown = "🚫 In Cooldown & Grace"

    summary_rows = [
        (1, "All Active", tab_all, len(cat_all_active), sum(r.get("outstanding_balance", 0.0) for r in cat_all_active), "Total active property leases in RentVine portfolio"),
        (2, "📩 Reminders Needed", tab_reminders, len(cat_reminders), sum(r.get("outstanding_balance", 0.0) for r in cat_reminders), "Eligible for rent reminder SMS dispatch past grace period"),
        (3, "🛡️ Promises on File", tab_promises, len(cat_promises), sum(r.get("outstanding_balance", 0.0) for r in cat_promises), "Confirmed payment commitment on file with future payment date — reminders held"),
        (4, "✅ Paid / Cleared", tab_paid, len(cat_paid), sum(r.get("outstanding_balance", 0.0) for r in cat_paid), "Tenant balance $0.00 — rent paid and cleared in RentVine"),
        (5, "🔴 Manual Review / Disputes", tab_flagged, len(cat_flagged), sum(r.get("outstanding_balance", 0.0) for r in cat_flagged), "Outstanding balance < $200, dispute, exclusion, or follow-up note requiring staff review"),
        (6, "🚫 In Cooldown / Grace", tab_cooldown, len(cat_cooldown), sum(r.get("outstanding_balance", 0.0) for r in cat_cooldown), "HITL-approved reminder dispatched within last 24h (active cooldown) or in monthly grace pause"),
    ]

    total_overdue = sum(r.get("outstanding_balance", 0.0) for r in active_records if r.get("outstanding_balance", 0.0) > 0)

    for row_idx, (num, cat_name, tab_name, count_val, bal_val, desc_val) in enumerate(summary_rows, start=7):
        is_even = (row_idx % 2 == 0)
        row_fill = zebra_fill if is_even else white_fill

        c_num = ws_sum.cell(row=row_idx, column=2, value=num)
        c_cat = ws_sum.cell(row=row_idx, column=3, value=cat_name)
        c_tab = ws_sum.cell(row=row_idx, column=4, value=f"=HYPERLINK(\"#'{tab_name}'!A1\", \"{tab_name} →\")")
        c_cnt = ws_sum.cell(row=row_idx, column=5, value=count_val)
        c_bal = ws_sum.cell(row=row_idx, column=6, value=bal_val)
        c_dsc = ws_sum.cell(row=row_idx, column=7, value=desc_val)

        c_num.alignment = Alignment(horizontal="center", vertical="center")
        c_cat.font = bold_data_font
        c_tab.font = Font(name=font_family, size=10, bold=True, color="0284C7", underline="single")
        c_cnt.font = bold_data_font
        c_cnt.alignment = Alignment(horizontal="right", vertical="center")
        c_bal.font = bold_data_font
        c_bal.number_format = "$#,##0.00"
        c_bal.alignment = Alignment(horizontal="right", vertical="center")
        c_dsc.font = muted_data_font

        for cell in (c_num, c_cat, c_tab, c_cnt, c_bal, c_dsc):
            cell.fill = row_fill
            cell.border = cell_border
        ws_sum.row_dimensions[row_idx].height = 22

    # Total Overdue Row
    tot_row = 13
    c_tot_lbl = ws_sum.cell(row=tot_row, column=3, value="Total Portfolio Overdue Balance")
    c_tot_lbl.font = Font(name=font_family, size=11, bold=True, color="991B1B")
    c_tot_bal = ws_sum.cell(row=tot_row, column=6, value=total_overdue)
    c_tot_bal.font = Font(name=font_family, size=11, bold=True, color="991B1B")
    c_tot_bal.number_format = "$#,##0.00"
    c_tot_bal.alignment = Alignment(horizontal="right", vertical="center")
    tot_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    for col in range(2, 8):
        c = ws_sum.cell(row=tot_row, column=col)
        c.fill = tot_fill
        c.border = cell_border
    ws_sum.row_dimensions[tot_row].height = 24

    # Post-HITL Execution Summary block on Summary tab (when dispatch_results supplied)
    if dispatch_results is not None:
        sent_cnt = len([d for d in dispatch_results if d.get("status") == "DISPATCHED"])
        rej_cnt = len([d for d in dispatch_results if d.get("status") in ("SKIPPED_BY_OPERATOR", "REJECTED_IN_HITL")])
        fail_cnt = len([d for d in dispatch_results if d.get("status") == "FAILED"])
        cool_cnt = len([d for d in dispatch_results if d.get("status") == "DISPATCHED" and d.get("cooldown_applied", True)])

        ws_sum.cell(row=15, column=2, value="HITL Dispatch & Reconciliation Execution Summary").font = Font(name=font_family, size=12, bold=True, color="0F172A")
        exec_headers = ["#", "Execution Metric", "Count", "Notes"]
        for ci, h_txt in enumerate(exec_headers, start=2):
            c = ws_sum.cell(row=16, column=ci, value=h_txt)
            c.font = header_font
            c.fill = header_fill
            c.border = cell_border
            c.alignment = Alignment(horizontal="center" if ci in (2, 4) else "left", vertical="center")
        ws_sum.row_dimensions[16].height = 22

        exec_rows = [
            (1, "✅ Approved & Successfully Dispatched (Quo Verified)", sent_cnt, "SMS dispatched and confirmed via Quo API; actual sent timestamp recorded"),
            (2, "🚫 Leases Entered 24h Cooldown (This Run)", cool_cnt, "Cooldown applied across entire lease & co-tenants only upon confirmed send"),
            (3, "⛔ Rejected / Skipped during HITL", rej_cnt, "Manually deselected/excluded — no SMS sent and NO cooldown applied"),
            (4, "❌ Dispatch Failures", fail_cnt, "Quo dispatch failed — accurately reported as failed and NO cooldown applied"),
        ]
        for idx, (n, label, val, note) in enumerate(exec_rows, start=17):
            c1 = ws_sum.cell(row=idx, column=2, value=n)
            c2 = ws_sum.cell(row=idx, column=3, value=label)
            c3 = ws_sum.cell(row=idx, column=4, value=val)
            c4 = ws_sum.cell(row=idx, column=5, value=note)
            c1.alignment = Alignment(horizontal="center", vertical="center")
            c2.font = bold_data_font
            c3.font = bold_data_font
            c3.alignment = Alignment(horizontal="center", vertical="center")
            c4.font = muted_data_font
            for c in (c1, c2, c3, c4):
                c.border = cell_border
                c.fill = zebra_fill if idx % 2 == 0 else white_fill
            ws_sum.row_dimensions[idx].height = 20

    ws_sum.column_dimensions["B"].width = 6
    ws_sum.column_dimensions["C"].width = 34
    ws_sum.column_dimensions["D"].width = 28
    ws_sum.column_dimensions["E"].width = 24
    ws_sum.column_dimensions["F"].width = 26
    ws_sum.column_dimensions["G"].width = 65

    # ─────────────────────────────────────────────────────────────────────────
    # WORKSHEETS 2–7: Category Detail Sheets
    # ─────────────────────────────────────────────────────────────────────────
    detail_columns = [
        ("RentVine Lease ID", 18, "center"),
        ("Property Address", 32, "left"),
        ("Unit Number", 28, "left"),
        ("Tenant Name(s)", 30, "left"),
        ("Phone Number(s)", 26, "left"),
        ("Email Address(es)", 32, "left"),
        ("Monthly Rent ($)", 16, "right"),
        ("Outstanding Balance ($)", 22, "right"),
        ("Due Date", 22, "left"),
        ("Latest SMS Reply", 40, "left"),
        ("Payment Promise Date", 20, "center"),
        ("Reminder Sent Timestamp", 24, "center"),
        ("Cooldown Expiry", 22, "center"),
        ("AI Decision Outcome", 22, "center"),
        ("Eligibility Status & Reason", 48, "left"),
        ("Follow-Up Notes / Manual Review Details", 48, "left"),
        ("HITL Dispatch Status (This Run)", 26, "center"),
        ("Actual Sent Timestamp (This Run)", 26, "center"),
        ("Cooldown Entered (24h)", 20, "center"),
        ("Dispatch / Verification Note", 42, "left"),
    ]

    detail_sheets_meta = [
        (tab_all, cat_all_active),
        (tab_reminders, cat_reminders),
        (tab_promises, cat_promises),
        (tab_paid, cat_paid),
        (tab_flagged, cat_flagged),
        (tab_cooldown, cat_cooldown),
    ]

    for title, records in detail_sheets_meta:
        ws = wb.create_sheet(title=title)
        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "A2"

        # Headers
        for col_idx, (col_name, col_width, align) in enumerate(detail_columns, start=1):
            c = ws.cell(row=1, column=col_idx, value=col_name)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
            c.border = cell_border
            col_letter = get_column_letter(col_idx)
            ws.column_dimensions[col_letter].width = col_width
        ws.row_dimensions[1].height = 28

        if not records:
            empty_cell = ws.cell(row=2, column=1, value="No records in this category.")
            empty_cell.font = muted_data_font
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(detail_columns))
            ws.row_dimensions[2].height = 24
            continue

        for r_idx, r in enumerate(records, start=2):
            is_even = (r_idx % 2 == 0)
            base_fill = zebra_fill if is_even else white_fill
            ws.row_dimensions[r_idx].height = 22

            outcome = r.get("ai_outcome", "")
            ostyle = outcome_styles.get(outcome, {})

            row_values = [
                r.get("lease_id") or "N/A",
                r.get("property_address") or "",
                r.get("unit_number") or "",
                r.get("tenant_names") or "",
                r.get("phone_numbers") or "",
                r.get("email_addresses") or "",
                float(r.get("monthly_rent") or 0.0),
                float(r.get("outstanding_balance") or 0.0),
                r.get("due_date") or "",
                r.get("latest_sms_reply") or "No messages",
                r.get("payment_promise_date") or "—",
                r.get("reminder_sent_timestamp") or "—",
                r.get("cooldown_expiry") or "—",
                ostyle.get("label", outcome),
                r.get("eligibility_reason") or "",
                r.get("follow_up_notes") or "None",
                r.get("this_run_dispatch_status") or "—",
                r.get("this_run_sent_at") or "—",
                r.get("this_run_cooldown") or "No",
                r.get("this_run_note") or "",
            ]

            for c_idx, val in enumerate(row_values, start=1):
                cell = ws.cell(row=r_idx, column=c_idx, value=val)
                align_type = detail_columns[c_idx - 1][2]
                cell.border = cell_border
                cell.fill = base_fill

                if c_idx == 1:
                    cell.font = Font(name=font_family, size=10, bold=True, color="334155")
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif c_idx == 4:
                    cell.font = Font(name=font_family, size=10, bold=True, color="0F172A")
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                elif c_idx in (7, 8):
                    cell.number_format = "$#,##0.00"
                    cell.font = Font(name=font_family, size=10, bold=(c_idx == 8 and val > 0), color="991B1B" if (c_idx == 8 and val > 0) else "0F172A")
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif c_idx == 14:
                    if ostyle:
                        cell.fill = ostyle["fill"]
                        cell.font = ostyle["font"]
                    else:
                        cell.font = bold_data_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif c_idx == 17:
                    s_val = str(val)
                    if "SENT THIS RUN" in s_val:
                        cell.fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
                        cell.font = Font(name=font_family, size=10, bold=True, color="065F46")
                    elif "FAILED" in s_val:
                        cell.fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
                        cell.font = Font(name=font_family, size=10, bold=True, color="991B1B")
                    elif "REJECTED" in s_val or "SKIPPED" in s_val:
                        cell.fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
                        cell.font = Font(name=font_family, size=10, bold=True, color="92400E")
                    else:
                        cell.font = muted_data_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif c_idx in (10, 15, 16, 20):
                    cell.font = muted_data_font
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                else:
                    cell.font = data_font
                    cell.alignment = Alignment(horizontal=align_type, vertical="center")

        max_col_letter = get_column_letter(len(detail_columns))
        ws.auto_filter.ref = f"A1:{max_col_letter}{len(records) + 1}"

    return wb


def export_daily_report_to_bytes() -> bytes:
    """
    Generates the complete Excel workbook and returns raw bytes for HTTP download.
    """
    wb = build_daily_excel_workbook()
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()
