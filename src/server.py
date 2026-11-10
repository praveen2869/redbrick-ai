"""
Red Bricks Property Management - Integrated HTTP API & Dashboard Server
Provides REST API endpoints for the web control center and serves frontend static assets.
Pure standard library implementation with zero external dependencies.
"""

import json
import logging
import mimetypes
import os
import sys
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, quote

from src.config import settings
from src.database import db
from src.models import (
    Tenant, RentVineRecord, RentVineNote, CommunicationMessage,
    Channel, MessageDirection, DecisionOutcome, IntentType, WorkflowDecision
)
from src.clients.rentvine_mcp import RentVineMCPClient, clean_property_address, format_friendly_due_date
from src.clients.quo_sms import QuoSMSClient
from src.clients.gmail_client import GmailClient
from src.engine.rules import BusinessRulesEngine
from src.engine.ai_classifier import AIIntentClassifier
from src.workflow import WorkflowOrchestrator
from src.reporting import ReportGenerator
from src.seed_data import populate_sample_data
from src.sync_manager import sync_manager

logger = logging.getLogger("RedBrickServer")

# Determine web assets directory
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# Fast in-memory cache to prevent slow repeated external API calls (TTL in seconds)
_API_CACHE: Dict[str, tuple] = {}

class RedBrickAPIHandler(BaseHTTPRequestHandler):
    orchestrator = WorkflowOrchestrator()

    def do_OPTIONS(self):
        """Handle CORS pre-flight requests."""
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.end_headers()

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            body_bytes = self.rfile.read(content_length)
            return json.loads(body_bytes.decode("utf-8"))
        return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # API Routes
        if path == "/api/status":
            return self._handle_get_status()
        elif path == "/api/tenants":
            return self._handle_get_tenants(query)
        elif path.startswith("/api/tenant/"):
            tenant_id = path.replace("/api/tenant/", "").strip()
            return self._handle_get_tenant_detail(tenant_id)
        elif path == "/api/runs":
            return self._handle_get_runs()
        elif path in ("/api/export-excel", "/api/daily-report-excel"):
            return self._handle_get_export_excel()
        elif path == "/api/latest-report":
            return self._handle_get_latest_report(query)
        elif path == "/api/settings":
            return self._handle_get_settings()
        elif path == "/api/connection-test":
            return self._handle_get_connection_test()
        elif path == "/api/rentvine-data":
            return self._handle_get_rentvine_data(query)
        elif path == "/api/rentvine-tenants":
            return self._handle_get_rentvine_tenants(query)
        elif path == "/api/quo-live":
            return self._handle_get_quo_live(query)
        elif path == "/api/rentvine-report":
            return self._handle_get_rentvine_report(query)

        # Serve static web frontend
        self._serve_static_file(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()
        except Exception as e:
            return self._send_json({"error": f"Invalid JSON body: {e}"}, 400)

        if path == "/api/run-workflow":
            return self._handle_post_run_workflow(body)
        elif path == "/api/simulate-reply":
            return self._handle_post_simulate_reply(body)
        elif path == "/api/send-sms":
            return self._handle_post_send_sms(body)
        elif path == "/api/send-reminder":
            return self._handle_post_send_reminder(body)
        elif path == "/api/pipeline-dispatch-approved":
            return self._handle_post_pipeline_dispatch_approved(body)
        elif path == "/api/add-note":
            return self._handle_post_add_note(body)
        elif path == "/api/reset-seed":
            return self._handle_post_reset_seed()
        elif path == "/api/settings":
            return self._handle_post_settings(body)
        elif path == "/api/connection-test":
            return self._handle_post_connection_test(body)

        self._send_json({"error": "Endpoint not found"}, 404)

    # --- Route Handlers ---

    def _handle_get_status(self):
        """Returns health status of the agent, MCP connection, Quo API, and DB."""
        recent_runs = db.get_recent_runs(limit=1)
        latest_run = recent_runs[0] if recent_runs else None

        data = {
            "system": "Red Bricks AI Rent Reminder Workflow",
            "version": "1.0",
            "environment": settings.ENVIRONMENT,
            "status": "OPERATIONAL",
            "sync": {
                "is_syncing": sync_manager.is_syncing,
                "status": sync_manager.sync_status,
                "last_synced": sync_manager.last_sync_time,
                "active_count": sync_manager.active_count
            },
            "scheduler": {
                "provider": "Azure Scheduler",
                "cron": "0 8 * * *",
                "status": "ACTIVE",
                "next_run": "Tomorrow at 08:00 UTC"
            },
            "integrations": {
                "rentvine_mcp": {
                    "endpoint": settings.RENTVINE_MCP_ENDPOINT,
                    "status": "CONNECTED",
                    "protocol": "Model Context Protocol (MCP) JSON-RPC 2.0",
                    "mode": "Simulation / Live-Ready" if settings.RENTVINE_MOCK_MODE else "Live MCP"
                },
                "quo_sms": {
                    "endpoint": settings.QUO_API_URL,
                    "phone_number_id": settings.QUO_DEFAULT_PHONE_NUMBER_ID,
                    "status": "ACTIVE",
                    "mode": "Simulation / Live-Ready" if settings.QUO_MOCK_MODE else "Live Quo API"
                },
                "gmail": {
                    "status": "CONNECTED",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                    "mode": "Simulation / Live-Ready" if settings.GMAIL_MOCK_MODE else "Live Gmail OAuth"
                },
                "ai_engine": {
                    "provider": settings.AI_PROVIDER,
                    "model": settings.AI_MODEL,
                    "has_claude_key": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or settings.ANTHROPIC_API_KEY),
                    "has_openai_key": bool(settings.OPENAI_API_KEY),
                    "has_groq_key": bool(settings.GROQ_API_KEY),
                    "active_engine": (
                        f"Claude Sonnet ({settings.AI_MODEL})" if (os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or settings.ANTHROPIC_API_KEY)
                        else (f"Groq ({settings.AI_MODEL})" if settings.GROQ_API_KEY
                        else (f"OpenAI ({settings.AI_MODEL})" if settings.OPENAI_API_KEY
                        else "Built-in Semantic NLU Engine"))
                    )
                }
            },
            "report_source_of_truth": {
                "report_id": "72",
                "report_name": "Rent Rem Praveen",
                "title": "Lease Balances",
                "url": "https://redbricksproperties.rentvine.com/reports/lease-balances?reportID=72",
                "status": "AUTHORITATIVE_SOURCE_OF_TRUTH"
            },
            "latest_run": latest_run
        }
        self._send_json(data)

    def _handle_get_tenants(self, query: Optional[Dict[str, list]] = None):
        """
        Returns records organized by unique RentVine property/lease (One Property/Lease = One Row).
        Combines all co-tenants living at the property, consolidates financial balances and due dates,
        tracks all SMS communications, and reflects the property's collective AI outcome and reminder status.
        """
        active_only = False
        if query and query.get("active_only", ["false"])[0].lower() in ("true", "1"):
            active_only = True

        with db.get_connection() as conn:
            cursor = conn.cursor()
            if active_only:
                cursor.execute("""
                    SELECT 
                        t.id, t.lease_id, t.first_name, t.last_name, t.phone, t.email,
                        t.property_name, t.unit_number, t.is_excluded, t.exclusion_reason,
                        r.balance, r.monthly_rent, r.due_date, r.last_payment_date,
                        r.last_payment_amount, r.portal_link, r.lease_status, r.follow_up_notes
                    FROM tenants t
                    JOIN rentvine_records r ON t.id = r.tenant_id
                    WHERE r.lease_status = 'ACTIVE'
                    ORDER BY t.property_name, t.unit_number, t.id
                """)
            else:
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

            # Batch retrieve all communication messages for fast SMS tracking
            cursor.execute("SELECT * FROM communication_messages ORDER BY timestamp DESC")
            all_raw_messages = cursor.fetchall()

        decisions = db.get_latest_decisions(limit=2000)
        latest_decision_by_tenant = {}
        for d in decisions:
            if d.tenant_id not in latest_decision_by_tenant:
                latest_decision_by_tenant[d.tenant_id] = {
                    "outcome": d.outcome.value,
                    "reason": d.reason,
                    "timestamp": d.timestamp,
                    "sms_sent": d.sms_sent,
                    "note_written": d.note_written
                }

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

        # Group records using the unique RentVine lease ID (or fallback to solo tenant ID)
        grouped_leases: Dict[str, list] = {}
        for r in tenant_rows:
            lid = str(r["lease_id"]).strip() if r["lease_id"] else f"__solo_{r['id']}"
            grouped_leases.setdefault(lid, []).append(r)

        results = []
        for lid, group_rows in grouped_leases.items():
            # Sort group rows so primary tenant (non-_co, or holding balance) leads
            def _primary_sort_key(r):
                tid = str(r["id"])
                bal = float(r["balance"] or 0.0)
                return (not "_co" in tid, bal > 0)
            group_rows.sort(key=_primary_sort_key, reverse=True)
            primary_row = group_rows[0]

            # Combine all tenants on this lease (deduplicated by phone / name)
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
                    "email": (r["email"] or "").strip(),
                    "is_primary": (len(tenants_list) == 0)
                })

            all_names = [t["full_name"] for t in tenants_list if t["full_name"]]
            all_phones = [t["phone"] for t in tenants_list if t["phone"]]
            all_emails = [t["email"] for t in tenants_list if t["email"]]
            tenant_ids = [t["id"] for t in tenants_list]
            primary_tenant_id = primary_row["id"]
            clean_lease_id = lid if not lid.startswith("__solo_") else None

            # Consolidate financial data: display property's total balance & rent only once
            lease_bal = max(float(r["balance"] or 0.0) for r in group_rows)
            lease_rent = max(float(r["monthly_rent"] or 0.0) for r in group_rows)
            raw_due = next((r["due_date"] for r in group_rows if r["due_date"]), None)
            if lease_bal <= 0.0 and raw_due and raw_due < current_month_first:
                effective_due = current_month_first
            else:
                effective_due = raw_due or current_month_first

            last_payment_date = next((r["last_payment_date"] for r in group_rows if r["last_payment_date"]), None)
            last_payment_amount = max(float(r["last_payment_amount"] or 0.0) for r in group_rows)
            portal_link = next((r["portal_link"] for r in group_rows if r["portal_link"]), settings.RENTVINE_PORTAL_PAYMENT_URL)
            lease_status = next((r["lease_status"] for r in group_rows if r["lease_status"]), "ACTIVE")
            
            # Combine follow-up notes across all lease rows
            all_notes_set = set(r["follow_up_notes"] for r in group_rows if r["follow_up_notes"])
            combined_notes = " | ".join(filter(None, all_notes_set))

            # Cooldown check across all tenant IDs on this lease
            is_cool, cool_last_rem, cool_h_elapsed, cool_h_rem, cool_exp_at = False, None, 0.0, 0.0, None
            if lease_bal > 0:
                for tid in tenant_ids:
                    c_ok, c_rem, c_el, c_rm, c_exp = db.get_cooldown_status(tid, current_time=now, cooldown_hours=24.0)
                    if c_ok:
                        is_cool, cool_last_rem, cool_h_elapsed, cool_h_rem, cool_exp_at = c_ok, c_rem, c_el, c_rm, c_exp
                        break

            # Payment promise check across all tenant IDs on this lease
            promise_dec = next((latest_decision_by_tenant[tid] for tid in tenant_ids if latest_decision_by_tenant.get(tid) and latest_decision_by_tenant[tid].get("outcome") == "HOLD_PROMISE"), None)
            mr_dec = next((latest_decision_by_tenant[tid] for tid in tenant_ids if latest_decision_by_tenant.get(tid) and latest_decision_by_tenant[tid].get("outcome") == "FLAG_MANUAL_REVIEW"), None)
            has_mr_note = any(k in combined_notes.lower() for k in ["manual review", "dispute", "attorney", "eviction"])

            # Property-level combined AI status & outcome
            if lease_bal <= 0.0:
                t_dec = {
                    "outcome": "SKIP_PAID",
                    "reason": "Property balance $0.00 — rent paid/cleared",
                    "timestamp": now.isoformat(),
                    "sms_sent": False,
                    "note_written": False
                }
            elif 0.0 < lease_bal < 200.0:
                t_dec = {
                    "outcome": "FLAG_MANUAL_REVIEW",
                    "reason": f"Total outstanding balance (${lease_bal:,.2f}) is greater than $0 but less than $200 — moved to Manual Review / Disputes for staff verification.",
                    "timestamp": now.isoformat(),
                    "sms_sent": False,
                    "note_written": False
                }
            elif is_cool and cool_last_rem:
                disp_exp = cool_exp_at.strftime('%Y-%m-%d %I:%M %p') if cool_exp_at else "in 24h"
                t_dec = {
                    "outcome": "SKIP_ALREADY_REMINDED",
                    "reason": f"Workflow rent reminder sent {cool_h_elapsed:.1f}h ago. Cooldown active for {cool_h_rem:.1f}h remaining (expires {disp_exp}).",
                    "timestamp": cool_last_rem.timestamp,
                    "sms_sent": True,
                    "note_written": False
                }
            elif has_mr_note or mr_dec:
                mr_reason = f"Flagged for manual review: {combined_notes}" if has_mr_note else (mr_dec.get("reason", "Flagged for manual review") if mr_dec else "Flagged for manual review")
                t_dec = {
                    "outcome": "FLAG_MANUAL_REVIEW",
                    "reason": mr_reason,
                    "timestamp": now.isoformat(),
                    "sms_sent": False,
                    "note_written": False
                }
            elif promise_dec:
                t_dec = {
                    "outcome": "HOLD_PROMISE",
                    "reason": promise_dec.get("reason", "Payment promise on file"),
                    "timestamp": promise_dec.get("timestamp", now.isoformat()),
                    "sms_sent": False,
                    "note_written": False
                }
            else:
                t_dec = {
                    "outcome": "SEND_REMINDER",
                    "reason": f"Unpaid balance ${lease_bal:,.2f} past grace period — reminder needed",
                    "timestamp": now.isoformat(),
                    "sms_sent": False,
                    "note_written": False
                }

            # Collect SMS messages across all tenants & phone numbers on this lease
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

            latest_msg = None
            if lease_msgs:
                m = lease_msgs[0]
                sender_name = next((t["first_name"] for t in tenants_list if t["phone"] and m["sender"] and t["phone"] in m["sender"]), None)
                latest_msg = {
                    "id": m["id"],
                    "channel": m["channel"],
                    "direction": m["direction"],
                    "sender": m["sender"],
                    "sender_name": sender_name,
                    "content": m["content"],
                    "timestamp": m["timestamp"]
                }

            results.append({
                "id": primary_tenant_id,
                "lease_id": clean_lease_id,
                "property_id": clean_lease_id,
                "first_name": tenants_list[0]["first_name"],
                "last_name": tenants_list[0]["last_name"],
                "full_name": ", ".join(all_names),
                "all_names": all_names,
                "phone": ", ".join(filter(None, all_phones)),
                "all_phones": all_phones,
                "email": ", ".join(filter(None, all_emails)),
                "all_emails": all_emails,
                "property_name": primary_row["property_name"],
                "unit_number": primary_row["unit_number"],
                "is_excluded": any(bool(r["is_excluded"]) for r in group_rows),
                "exclusion_reason": next((r["exclusion_reason"] for r in group_rows if r["exclusion_reason"]), None),
                "balance": lease_bal,
                "monthly_rent": lease_rent,
                "due_date": effective_due,
                "last_payment_date": last_payment_date,
                "last_payment_amount": last_payment_amount,
                "portal_link": portal_link,
                "lease_status": lease_status,
                "follow_up_notes": combined_notes,
                "is_multi_tenant": len(tenants_list) > 1,
                "co_tenant_count": len(tenants_list) - 1,
                "tenants": tenants_list,
                "tenant_ids": tenant_ids,
                "latest_decision": t_dec,
                "latest_message": latest_msg,
                "message_count": len(lease_msgs),
                "notes_count": len(all_notes_set)
            })

        self._send_json({"tenants": results, "total": len(results)})

    def _handle_get_tenant_detail(self, tenant_id: str):
        """Returns full property/tenant dossier: RentVine record, notes, Quo SMS messages, Gmail emails, and audit log."""
        tenants = db.get_all_tenants()
        matched = [t for t in tenants if t.id == tenant_id]
        if not matched:
            matched = [t for t in tenants if t.lease_id and str(t.lease_id) == str(tenant_id)]
        if not matched:
            return self._send_json({"error": "Tenant or Lease not found"}, 404)

        t = matched[0]
        # Find all co-tenants on the same lease
        lease_tenants = [ot for ot in tenants if ot.lease_id and ot.lease_id == t.lease_id] if t.lease_id else [t]
        if not lease_tenants:
            lease_tenants = [t]

        rv = db.get_rentvine_record(t.id)
        if not rv and t.lease_id:
            for lt in lease_tenants:
                rv = db.get_rentvine_record(lt.id)
                if rv and rv.balance > 0:
                    break

        # Consolidate messages across all lease tenants
        all_messages = []
        seen_mids = set()
        for lt in lease_tenants:
            for m in db.get_messages_for_tenant(lt.id):
                if m.id not in seen_mids:
                    seen_mids.add(m.id)
                    all_messages.append(m)
        all_messages.sort(key=lambda m: getattr(m, 'timestamp', ''), reverse=True)

        # Consolidate notes across all lease tenants
        all_notes = []
        seen_nids = set()
        for lt in lease_tenants:
            for n in db.get_rentvine_notes(lt.id):
                if n.id not in seen_nids:
                    seen_nids.add(n.id)
                    all_notes.append(n)
        all_notes.sort(key=lambda n: getattr(n, 'created_at', ''), reverse=True)

        # Consolidate decisions across all lease tenants
        all_decisions = []
        seen_dids = set()
        for lt in lease_tenants:
            for d in db.get_decisions_for_tenant(lt.id, limit=50):
                d_key = f"{d.run_id}_{d.outcome.value}"
                if d_key not in seen_dids:
                    seen_dids.add(d_key)
                    all_decisions.append({
                        "run_id": d.run_id,
                        "outcome": d.outcome.value,
                        "reason": d.reason,
                        "timestamp": d.timestamp,
                        "sms_sent": d.sms_sent,
                        "note_written": d.note_written,
                        "details": d.details
                    })

        data = {
            "tenant": {
                "id": t.id,
                "lease_id": t.lease_id,
                "full_name": ", ".join(lt.full_name for lt in lease_tenants),
                "primary_name": t.full_name,
                "phone": ", ".join(filter(None, [lt.phone for lt in lease_tenants])),
                "email": ", ".join(filter(None, [lt.email for lt in lease_tenants])),
                "property_name": t.property_name,
                "unit_number": t.unit_number,
                "is_excluded": any(lt.is_excluded for lt in lease_tenants),
                "exclusion_reason": next((lt.exclusion_reason for lt in lease_tenants if lt.exclusion_reason), None),
                "is_multi_tenant": len(lease_tenants) > 1,
                "all_tenants": [
                    {
                        "id": lt.id,
                        "full_name": lt.full_name,
                        "first_name": lt.first_name,
                        "last_name": lt.last_name,
                        "phone": lt.phone,
                        "email": lt.email,
                        "is_primary": (lt.id == t.id)
                    }
                    for lt in lease_tenants
                ]
            },
            "rentvine": {
                "balance": rv.balance if rv else 0.0,
                "monthly_rent": rv.monthly_rent if rv else 0.0,
                "due_date": rv.due_date if rv else None,
                "last_payment_date": rv.last_payment_date if rv else None,
                "last_payment_amount": rv.last_payment_amount if rv else 0.0,
                "portal_link": rv.portal_link if rv else settings.RENTVINE_PORTAL_PAYMENT_URL,
                "notes": [
                    {
                        "id": n.id,
                        "created_at": n.created_at,
                        "author": n.author,
                        "text": n.text,
                        "is_automated": n.is_automated
                    }
                    for n in all_notes
                ]
            },
            "communications": [
                {
                    "id": m.id,
                    "channel": m.channel.value if hasattr(m.channel, 'value') else str(m.channel),
                    "direction": m.direction.value if hasattr(m.direction, 'value') else str(m.direction),
                    "sender": m.sender,
                    "recipient": m.recipient,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "status": m.status,
                    "metadata": m.metadata
                }
                for m in all_messages
            ],
            "decisions": all_decisions
        }
        self._send_json(data)

    def _handle_get_runs(self):
        runs = db.get_recent_runs(limit=20)
        self._send_json({"runs": runs})

    def _handle_get_export_excel(self):
        """
        Generates and serves the complete multi-tab executive Excel (.xlsx) report
        matching the 6 dashboard categories + Summary sheet.
        """
        try:
            from src.engine.excel_exporter import export_daily_report_to_bytes
            excel_bytes = export_daily_report_to_bytes()
            now_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"RBPM_Daily_Rent_Report_{now_str}.xlsx"

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(excel_bytes)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(excel_bytes)
        except Exception as e:
            logger.error(f"Error generating daily Excel report: {e}", exc_info=True)
            self._send_json({"error": f"Failed to generate Excel report: {str(e)}"}, 500)

    def _handle_get_latest_report(self, query: Dict[str, Any]):
        runs = db.get_recent_runs(limit=1)
        if not runs:
            return self._send_json({"error": "No workflow runs on record yet"}, 404)

        run_dict = runs[0]
        # Reconstruct DailyRunSummary for HTML or markdown output
        format_type = query.get("format", ["json"])[0]

        if format_type in ("excel", "xlsx"):
            return self._handle_get_export_excel()
        elif format_type == "html":
            # Return raw HTML
            summary = self.orchestrator.execute_daily_run(is_dry_run=True)
            html_content = ReportGenerator.generate_html(summary)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))
            return
        elif format_type == "markdown":
            summary = self.orchestrator.execute_daily_run(is_dry_run=True)
            md_content = ReportGenerator.generate_markdown(summary)
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.end_headers()
            self.wfile.write(md_content.encode("utf-8"))
            return

        self._send_json(run_dict)

    def _handle_get_settings(self):
        data = {
            "grace_period_days": settings.GRACE_PERIOD_DAYS,
            "deduplication_window_hours": settings.DEDUPLICATION_WINDOW_HOURS,
            "promise_expiry_days": settings.PROMISE_EXPIRY_DAYS,
            "rentvine_endpoint": settings.RENTVINE_MCP_ENDPOINT,
            "rentvine_mock_mode": settings.RENTVINE_MOCK_MODE,
            "rentvine_has_credentials": bool(settings.RENTVINE_USERNAME or settings.RENTVINE_BEARER_TOKEN),
            "quo_api_url": settings.QUO_API_URL,
            "quo_mock_mode": settings.QUO_MOCK_MODE,
            "quo_has_key": not settings.QUO_MOCK_MODE,
            "quo_phone_tenants_id": settings.QUO_PHONE_TENANTS_ID,
            "quo_phone_tenants_number": settings.QUO_PHONE_TENANTS_FORMATTED,
            "quo_phone_landlords_id": settings.QUO_PHONE_LANDLORDS_ID,
            "quo_phone_landlords_number": settings.QUO_PHONE_LANDLORDS_FORMATTED,
            "quo_phone_primary_id": settings.QUO_PHONE_PRIMARY_ID,
            "quo_phone_primary_number": settings.QUO_PHONE_PRIMARY_FORMATTED,
            "ai_model": settings.AI_MODEL,
            "has_openai_key": bool(settings.OPENAI_API_KEY)
        }
        self._send_json(data)

    def _handle_get_connection_test(self):
        """Quick connection probe for all three integrations."""
        rv_client = RentVineMCPClient()
        rv_result = rv_client.connection_test()
        quo_result = self._test_quo_connection()
        self._send_json({
            "rentvine": rv_result,
            "quo": quo_result,
            "gmail": {
                "status": "simulation" if settings.GMAIL_MOCK_MODE else "configured",
                "mode": "mock" if settings.GMAIL_MOCK_MODE else "live"
            }
        })

    def _test_quo_connection(self) -> Dict[str, Any]:
        """Pings Quo API's /phone-numbers endpoint to verify the key and reports configured sender lines."""
        configured_lines = {
            "tenants": {
                "id": settings.QUO_PHONE_TENANTS_ID,
                "number": settings.QUO_PHONE_TENANTS_FORMATTED,
                "e164": settings.QUO_PHONE_TENANTS_E164,
                "inbox": "Tenants & Vendors",
                "purpose": "Tenant rent reminders & payment follow-ups"
            },
            "landlords": {
                "id": settings.QUO_PHONE_LANDLORDS_ID,
                "number": settings.QUO_PHONE_LANDLORDS_FORMATTED,
                "e164": settings.QUO_PHONE_LANDLORDS_E164,
                "inbox": "Landlords",
                "purpose": "Landlord communications & updates"
            },
            "primary": {
                "id": settings.QUO_PHONE_PRIMARY_ID,
                "number": settings.QUO_PHONE_PRIMARY_FORMATTED,
                "e164": settings.QUO_PHONE_PRIMARY_E164,
                "inbox": "Primary",
                "purpose": "General inbox (isolated from rent reminders)"
            }
        }
        if settings.QUO_MOCK_MODE:
            return {
                "status": "simulation",
                "mode": "mock",
                "endpoint": settings.QUO_API_URL,
                "configured_lines": configured_lines
            }
        import urllib.request as _ur, urllib.error as _ue
        auth_header = settings.QUO_API_KEY if settings.QUO_API_KEY.startswith("Bearer ") else f"Bearer {settings.QUO_API_KEY}"
        try:
            req = _ur.Request(
                f"{settings.QUO_API_URL}/phone-numbers",
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
            )
            with _ur.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                phones = data.get("data", [])
                return {
                    "status": "connected",
                    "endpoint": settings.QUO_API_URL,
                    "configured_lines": configured_lines,
                    "phone_numbers": [
                        {"id": p.get("id"), "number": p.get("phoneNumber"), "name": p.get("name")}
                        for p in (phones if isinstance(phones, list) else [])
                    ]
                }
        except _ue.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {"status": "error", "http_code": e.code, "detail": body, "configured_lines": configured_lines}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    def _handle_post_connection_test(self, body: Dict[str, Any]):
        """Accepts override credentials for a one-time connection probe."""
        if "quo_api_key" in body:
            settings.QUO_API_KEY = body["quo_api_key"]
            settings.QUO_MOCK_MODE = False
        if "rentvine_username" in body:
            settings.RENTVINE_USERNAME = body["rentvine_username"]
        if "rentvine_password" in body:
            settings.RENTVINE_PASSWORD = body["rentvine_password"]
        if "rentvine_bearer_token" in body:
            settings.RENTVINE_BEARER_TOKEN = body["rentvine_bearer_token"]
            settings.RENTVINE_MOCK_MODE = False
        rv_client = RentVineMCPClient()
        rv_result = rv_client.connection_test()
        quo_result = self._test_quo_connection()
        self._send_json({"rentvine": rv_result, "quo": quo_result})

    def _handle_get_rentvine_data(self, query: Dict[str, Any]):
        """
        Provides paginated, searchable access to live RentVine records:
        - section: 'properties' | 'tenants' | 'leases' | 'counts'
        - page: int (1-based, default 1)
        - pageSize: int (default 15, max 100)
        - search: optional string filter
        """
        rv_client = RentVineMCPClient()
        section = query.get("section", ["properties"])[0]
        try:
            page = max(1, int(query.get("page", [1])[0]))
        except (ValueError, TypeError):
            page = 1
        try:
            page_size = min(max(1, int(query.get("pageSize", [15])[0])), 100)
        except (ValueError, TypeError):
            page_size = 15
        search = query.get("search", [""])[0].strip()
        search_param = f"&search={quote(search)}" if search else ""

        def _fetch_with_retry(endpoint: str):
            for attempt in range(2):
                try:
                    return rv_client._rest_get_with_headers(endpoint)
                except Exception as ex:
                    if attempt == 1:
                        raise
                    import time; time.sleep(0.5)

        cache_key = f"rv_{section}_{page}_{page_size}_{search}"
        now_ts = datetime.utcnow().timestamp()
        if cache_key in _API_CACHE:
            cached_time, cached_val = _API_CACHE[cache_key]
            if now_ts - cached_time < 30:
                return self._send_json(cached_val)

        try:
            if section == "counts":
                # Quick probe of totals via pageSize=1
                _, p_h = _fetch_with_retry("properties?pageSize=1")
                _, t_h = _fetch_with_retry("tenants?pageSize=1")
                _, l_h = _fetch_with_retry("leases?pageSize=1")
                res = {
                    "mode": "live",
                    "properties": int(p_h.get("Pagination-Total-Items", 0)),
                    "tenants": int(t_h.get("Pagination-Total-Items", 0)),
                    "leases": int(l_h.get("Pagination-Total-Items", 0))
                }
                _API_CACHE[cache_key] = (now_ts, res)
                return self._send_json(res)

            elif section == "properties":
                raw_data, hdrs = _fetch_with_retry(f"properties?page={page}&pageSize={page_size}{search_param}")
                items = [
                    {
                        "property_id": p.get("property", {}).get("propertyID"),
                        "name": p.get("property", {}).get("name") or p.get("property", {}).get("address") or "N/A",
                        "address": p.get("property", {}).get("address") or "-",
                        "city": p.get("property", {}).get("city") or "-",
                        "state": p.get("property", {}).get("stateID") or "-",
                        "postal_code": p.get("property", {}).get("postalCode") or "-",
                        "is_active": p.get("property", {}).get("isActive") == "1",
                    }
                    for p in (raw_data if isinstance(raw_data, list) else [])
                ]
            elif section == "tenants":
                raw_data, hdrs = _fetch_with_retry(f"tenants?page={page}&pageSize={page_size}{search_param}")
                items = [
                    {
                        "contact_id": t.get("contact", {}).get("contactID"),
                        "name": t.get("contact", {}).get("name") or "Unnamed",
                        "email": t.get("contact", {}).get("email") or "",
                        "phone": t.get("contact", {}).get("phone") or "",
                    }
                    for t in (raw_data if isinstance(raw_data, list) else [])
                ]
            elif section == "leases":
                raw_data, hdrs = _fetch_with_retry(f"leases?page={page}&pageSize={page_size}{search_param}")
                items = [
                    {
                        "lease_id": l.get("lease", {}).get("leaseID"),
                        "tenants": l.get("lease", {}).get("tenants", []),
                        "property_name": l.get("unit", {}).get("name") or l.get("unit", {}).get("address") or "N/A",
                        "unit_id": l.get("unit", {}).get("unitID"),
                        "rent": float(l.get("unit", {}).get("rent") or 0.0),
                        "start_date": l.get("lease", {}).get("startDate") or "-",
                        "end_date": l.get("lease", {}).get("endDate") or "Month-to-Month",
                        "status": "Active" if l.get("lease", {}).get("primaryLeaseStatusID") == "2" else (
                            "Draft" if l.get("lease", {}).get("primaryLeaseStatusID") == "1" else "Past / Inactive"
                        )
                    }
                    for l in (raw_data if isinstance(raw_data, list) else [])
                ]
            else:
                return self._send_json({"error": f"Unknown section '{section}'"}, 400)

            total_items = int(hdrs.get("Pagination-Total-Items", len(items)))
            total_pages = int(hdrs.get("Pagination-Total-Pages", 1))
            current_page = int(hdrs.get("Pagination-Current-Page", page))

            self._send_json({
                "mode": "live",
                "section": section,
                "page": current_page,
                "pageSize": page_size,
                "totalItems": total_items,
                "totalPages": total_pages,
                "items": items
            })
        except Exception as e:
            logger.error(f"Error in _handle_get_rentvine_data ({section}): {e}")
            self._send_json({"mode": "live", "error": str(e), "items": [], "totalItems": 0, "totalPages": 1, "page": 1})

    def _handle_get_rentvine_tenants(self, query: Optional[Dict[str, Any]] = None):
        """Returns overview counts and page 1 data from RentVine REST API."""
        rv_client = RentVineMCPClient()
        try:
            properties_data, p_hdrs = rv_client._rest_get_with_headers("properties?page=1&pageSize=25")
            tenants_data, t_hdrs = rv_client._rest_get_with_headers("tenants?page=1&pageSize=25")
            leases_data, l_hdrs = rv_client._rest_get_with_headers("leases?page=1&pageSize=25")

            total_properties = int(p_hdrs.get("Pagination-Total-Items", len(properties_data)))
            total_tenants = int(t_hdrs.get("Pagination-Total-Items", len(tenants_data)))
            total_leases = int(l_hdrs.get("Pagination-Total-Items", len(leases_data)))

            properties = [
                {
                    "property_id": p.get("property", {}).get("propertyID"),
                    "name": p.get("property", {}).get("name") or p.get("property", {}).get("address"),
                    "address": p.get("property", {}).get("address"),
                    "city": p.get("property", {}).get("city"),
                    "state": p.get("property", {}).get("stateID"),
                    "postal_code": p.get("property", {}).get("postalCode"),
                    "is_active": p.get("property", {}).get("isActive") == "1",
                }
                for p in (properties_data if isinstance(properties_data, list) else [])
            ]

            tenants = [
                {
                    "contact_id": t.get("contact", {}).get("contactID"),
                    "name": t.get("contact", {}).get("name"),
                    "email": t.get("contact", {}).get("email"),
                    "phone": t.get("contact", {}).get("phone"),
                }
                for t in (tenants_data if isinstance(tenants_data, list) else [])
            ]

            leases = [
                {
                    "lease_id": l.get("lease", {}).get("leaseID"),
                    "tenants": l.get("lease", {}).get("tenants", []),
                    "start_date": l.get("lease", {}).get("startDate"),
                    "end_date": l.get("lease", {}).get("endDate"),
                }
                for l in (leases_data if isinstance(leases_data, list) else [])
            ]

            self._send_json({
                "mode": "live",
                "counts": {
                    "properties": total_properties,
                    "tenants": total_tenants,
                    "leases": total_leases,
                    "loaded_properties": len(properties),
                    "loaded_tenants": len(tenants),
                    "loaded_leases": len(leases)
                },
                "properties": properties,
                "tenants": tenants,
                "leases": leases
            })
        except Exception as e:
            self._send_json({"mode": "live", "error": str(e)}, 500)

    def _handle_get_rentvine_report(self, query: Optional[Dict[str, list]] = None):
        """Returns the authoritative Lease Balances Report #72 data directly from RentVine."""
        report_id = 72
        if query and "report_id" in query:
            try:
                report_id = int(query["report_id"][0])
            except Exception:
                pass

        rv_client = RentVineMCPClient()
        try:
            data = rv_client.fetch_lease_balance_report(report_id=report_id)
            self._send_json({"success": True, "report": data})
        except Exception as e:
            logger.error(f"Error fetching RentVine report #{report_id}: {e}")
            self._send_json({"success": False, "error": str(e)}, 500)

    def _handle_get_quo_live(self, query: Optional[Dict[str, list]] = None):
        """
        Live Quo (OpenPhone) feed handler.
        - Supports line selection via ?line=tenants (default), ?line=landlords, ?line=primary
        - Fetches real conversations + messages from OpenPhone REST API ONLY for tenants
          identified as overdue/unpaid in RentVine Report #72.
        - Builds reminders queue from real workflow decisions strictly mapped to Tenants & Vendors (980) 300-7559
        - Adds HOLD_PROMISE tenants to the log with promise status and next eligible reminder date
        - Merges HITL-approved sends from local DB
        """
        selected_line = "tenants"
        if query and "line" in query and query["line"]:
            param = str(query["line"][0]).lower().strip()
            if param in ("landlord", "landlords", "owner"):
                selected_line = "landlords"
            elif param in ("primary", "general"):
                selected_line = "primary"
            else:
                selected_line = "tenants"

        now_ts = datetime.utcnow().timestamp()
        cache_key = f"quo_live_{selected_line}"
        force_refresh = bool(query and ("refresh" in query or "force" in query or "_t" in query))
        if not force_refresh and cache_key in _API_CACHE:
            cached_time, cached_val = _API_CACHE[cache_key]
            if now_ts - cached_time < 10:
                return self._send_json(cached_val)

        # Resolve phone line identity based on selection
        phone_id, phone_e164, phone_number, inbox_name = settings.get_quo_sender(selected_line)
        line_purpose = (
            "Tenant rent reminders & payment follow-ups" if selected_line == "tenants"
            else ("Landlord communications & updates" if selected_line == "landlords"
                  else "General inbox (isolated)")
        )

        all_tenants = db.get_all_tenants()
        tenant_map = {t.id: t for t in all_tenants}

        # ─── 0. Build overdue-tenant phone whitelist from Report #72 ───
        # This is the authoritative filter: only fetch Quo conversations for these tenants.
        overdue_phones: set = set()          # set of cleaned 10-digit phone strings
        overdue_tenant_ids: set = set()      # set of tenant IDs in overdue report
        report_phone_to_tenant: Dict[str, object] = {}  # phone → Tenant object

        if not settings.RENTVINE_MOCK_MODE and settings.RENTVINE_API_KEY:
            try:
                rv_client = RentVineMCPClient()
                report_data = rv_client.fetch_lease_balance_report(report_id=72)
                for rec in report_data.get("records", []):
                    # Only include tenants with a real outstanding balance
                    if float(rec.get("past_due_total", 0) or 0) <= 0:
                        continue
                    raw_phone = rec.get("phone") or ""
                    clean_p = "".join([c for c in raw_phone if c.isdigit()])
                    rec_lid = str(rec.get("lease_id") or "")
                    if clean_p:
                        p10 = clean_p[-10:]
                        overdue_phones.add(p10)
                    else:
                        p10 = ""
                    # Match to a local tenant object by phone or lease_id
                    for t in all_tenants:
                        t_clean = "".join([c for c in (t.phone or "") if c.isdigit()])
                        if (p10 and t_clean and t_clean[-10:] == p10) or (rec_lid and t.lease_id and str(t.lease_id) == rec_lid):
                            overdue_tenant_ids.add(t.id)
                            if p10 and p10 not in report_phone_to_tenant:
                                report_phone_to_tenant[p10] = t
                logger.info(
                    f"[Quo - Overdue Filter] {len(overdue_phones)} overdue tenant phones from Report #72. "
                    f"Quo conversations will be restricted to these contacts."
                )
            except Exception as e:
                logger.warning(f"[Quo - Overdue Filter] Could not fetch Report #72 for phone whitelist: {e}. Falling back to all tenants.")
                # Fallback: build from local DB tenants with positive balances
                for t in all_tenants:
                    rv = db.get_rentvine_record(t.id)
                    if rv and float(rv.balance or 0) > 0:
                        clean_p = "".join([c for c in (t.phone or "") if c.isdigit()])
                        if clean_p:
                            overdue_phones.add(clean_p[-10:])
                            overdue_tenant_ids.add(t.id)
        else:
            # Mock/dev mode: use local DB balance as fallback
            for t in all_tenants:
                rv = db.get_rentvine_record(t.id)
                if rv and float(rv.balance or 0) > 0:
                    clean_p = "".join([c for c in (t.phone or "") if c.isdigit()])
                    if clean_p:
                        overdue_phones.add(clean_p[-10:])
                        overdue_tenant_ids.add(t.id)

        # ─── 1. Fetch real OpenPhone messages ONLY for overdue tenants ───
        openphone_messages = []
        matched_overdue_convs = 0
        quo_api_status = "CONNECTED"
        quo_api_error = None
        quo_probe = self._test_quo_connection()

        if not settings.QUO_MOCK_MODE and settings.QUO_API_KEY:
            import urllib.request as _ur, urllib.error as _ue, urllib.parse as _up
            auth = settings.QUO_API_KEY if settings.QUO_API_KEY.startswith("Bearer ") else f"Bearer {settings.QUO_API_KEY}"
            headers = {
                "Authorization": auth,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }

            classifier = AIIntentClassifier()
            from src.clients.quo_sms import normalize_phone_e164, phone_to_10digit

            # Build lease-to-tenants mapping to support multi-tenant group conversations
            lease_to_tenants = {}
            for cur_t in all_tenants:
                cur_lid = cur_t.lease_id or cur_t.id
                lease_to_tenants.setdefault(cur_lid, []).append(cur_t)

            # Determine all target overdue leases
            target_leases = set()
            for cur_tid in overdue_tenant_ids:
                cur_t = tenant_map.get(cur_tid)
                if cur_t:
                    target_leases.add(cur_t.lease_id or cur_t.id)

            # Build fetch targets: (lid, list_of_tenants, list_of_e164_phones)
            fetch_targets = []
            seen_target_keys = set()
            for lid in target_leases:
                ts = lease_to_tenants.get(lid, [])
                if not ts:
                    continue
                phones = []
                seen_p = set()
                for t_item in ts:
                    e164_val = normalize_phone_e164(t_item.phone)
                    if e164_val and e164_val not in seen_p:
                        seen_p.add(e164_val)
                        phones.append(e164_val)
                if phones:
                    target_key = f"{lid}_{'_'.join(sorted(phones))}"
                    if target_key not in seen_target_keys:
                        seen_target_keys.add(target_key)
                        fetch_targets.append((lid, ts, phones))

            # Query OpenPhone /messages in parallel for all overdue lease groups
            def _fetch_lease_msgs(target):
                lid, ts, phones = target
                query_parts = "&".join(f"participants[]={_up.quote(p)}" for p in phones)
                msg_url = f"{settings.QUO_API_URL}/messages?phoneNumberId={phone_id}&{query_parts}&maxResults=10"
                try:
                    req = _ur.Request(msg_url, headers=headers)
                    with _ur.urlopen(req, timeout=5) as resp:
                        msg_data = json.loads(resp.read().decode("utf-8"))
                        return (lid, ts, phones, msg_data.get("data", []) or [])
                except Exception as e:
                    return (lid, ts, phones, [])

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=10) as executor:
                fetch_results = list(executor.map(_fetch_lease_msgs, fetch_targets))

            seen_msg_ids = set()
            for lid, ts, phones, raw_msgs in fetch_results:
                if not raw_msgs:
                    continue
                matched_overdue_convs += 1
                primary_t = ts[0] if ts else None
                t_name = ", ".join([x.full_name for x in ts]) if len(ts) > 1 else (primary_t.full_name if primary_t else "Tenant")
                prop_unit = f"{primary_t.property_name} {primary_t.unit_number}".strip() if primary_t else ""
                phones_disp = ", ".join(phones)

                comm_msgs = []
                for m in raw_msgs:
                    mid = m.get("id", "")
                    if mid and mid in seen_msg_ids:
                        continue
                    if mid:
                        seen_msg_ids.add(mid)

                    txt = m.get("text", m.get("body", ""))
                    is_inbound = (m.get("direction") == "incoming")
                    created_ts = m.get("createdAt", m.get("deliveredAt", ""))
                    msg_status = m.get("status", "delivered").upper()

                    # Detect if outbound message is an RBPM rent reminder
                    is_rent_reminder = False
                    if not is_inbound and ("Friendly reminder from Red Bricks" in txt or "RBPM BOT" in txt or "Your rent of" in txt or "rent was due" in txt):
                        is_rent_reminder = True

                    meta_dict = {
                        "source": "OpenPhone",
                        "inbox": inbox_name,
                        "is_workflow_reminder": is_rent_reminder,
                        "reminder_type": "RENT_REMINDER" if is_rent_reminder else None,
                        "is_group": len(phones) > 1,
                        "participants": phones
                    }

                    c_msg = CommunicationMessage(
                        id=mid,
                        tenant_id=primary_t.id if primary_t else f"lease_{lid}",
                        channel=Channel.SMS,
                        direction=MessageDirection.INBOUND if is_inbound else MessageDirection.OUTBOUND,
                        sender=m.get("from", ""),
                        recipient=phones_disp,
                        content=txt,
                        timestamp=created_ts,
                        status="SENT_HITL_APPROVED" if is_rent_reminder else msg_status,
                        metadata=meta_dict
                    )
                    comm_msgs.append(c_msg)
                    db.save_message(c_msg)

                    # Sync to workflow_reminders for ALL tenants on this lease so 24-hr cooldown is active
                    if is_rent_reminder:
                        for cur_t in ts:
                            cur_p = normalize_phone_e164(cur_t.phone) or (phones[0] if phones else "")
                            db.record_workflow_reminder(
                                reminder_id=f"quo_{mid}_{cur_t.id[-4:]}",
                                tenant_id=cur_t.id,
                                reminder_type="RENT_REMINDER",
                                message_id=mid,
                                workflow_id="openphone_sync",
                                phone=cur_p,
                                content=txt,
                                sender_phone=m.get("from", ""),
                                delivery_status="DELIVERED",
                                sent_at=created_ts
                            )

                    openphone_messages.append({
                        "id": mid,
                        "conversation_id": m.get("conversationId", ""),
                        "direction": "INBOUND" if is_inbound else "OUTBOUND",
                        "content": txt,
                        "timestamp": created_ts,
                        "status": "RECEIVED" if is_inbound else msg_status,
                        "tenant_name": t_name,
                        "phone": phones_disp,
                        "tenant_id": primary_t.id if primary_t else "",
                        "property_unit": prop_unit,
                        "source": "openphone_live",
                        "line": selected_line,
                        "inbox": inbox_name
                    })

                # Check tenant communications for payment reply, date-based promises, or disputes
                if primary_t and comm_msgs:
                    ai_res = classifier.classify_tenant_communications(comm_msgs, reference_date=datetime.utcnow())
                    today_str = datetime.utcnow().strftime("%Y-%m-%d")

                    # Scenario 1: Specific payment date provided -> On Hold (Payment Promised)
                    if ai_res.intent == IntentType.PROMISE_MADE and ai_res.extracted_date:
                        p_date = ai_res.extracted_date
                        if p_date >= today_str:
                            rv_bal = db.get_rentvine_record(primary_t.id)
                            bal_val = float(rv_bal.balance) if rv_bal else 0.0
                            for cur_t in ts:
                                hold_dec = WorkflowDecision(
                                    tenant_id=cur_t.id,
                                    tenant_name=cur_t.full_name,
                                    property_unit=prop_unit,
                                    balance=bal_val,
                                    outcome=DecisionOutcome.HOLD_PROMISE,
                                    reason=f"Payment promised via Quo SMS for {p_date}: \"{ai_res.raw_text}\" — reminder held",
                                    details={
                                        "intent": "PROMISE_MADE",
                                        "promise_date": p_date,
                                        "extracted_date": p_date,
                                        "raw_text": ai_res.raw_text,
                                        "channel": "SMS"
                                    },
                                    timestamp=datetime.utcnow().isoformat(),
                                    run_id="quo_sync"
                                )
                                db.save_decision(hold_dec)
                            logger.info(f"[Quo Promise] Payment commitment recorded for {t_name}: pay by {p_date}")

                    # Scenario 2: Payment-related reply WITHOUT specific date -> Manual Review / Disputes
                    elif ai_res.intent == IntentType.PAYMENT_UNSPECIFIED:
                        rv_bal = db.get_rentvine_record(primary_t.id)
                        bal_val = float(rv_bal.balance) if rv_bal else 0.0
                        for cur_t in ts:
                            mr_dec = WorkflowDecision(
                                tenant_id=cur_t.id,
                                tenant_name=cur_t.full_name,
                                property_unit=prop_unit,
                                balance=bal_val,
                                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                                reason=f"Payment reply without specific date (\"{ai_res.raw_text}\") — moved to Manual Review / Disputes",
                                details={
                                    "intent": "PAYMENT_UNSPECIFIED",
                                    "raw_text": ai_res.raw_text,
                                    "channel": "SMS",
                                    "classification_reason": "Payment reply without confirmed date"
                                },
                                timestamp=datetime.utcnow().isoformat(),
                                run_id="quo_sync"
                            )
                            db.save_decision(mr_dec)
                        logger.info(f"[Quo Manual Review] Payment reply without date for {t_name}: \"{ai_res.raw_text}\"")

                    # Scenario 3: Unable or unwilling to pay -> Manual Review / Disputes for staff follow-up
                    elif ai_res.intent == IntentType.UNABLE_TO_PAY:
                        rv_bal = db.get_rentvine_record(primary_t.id)
                        bal_val = float(rv_bal.balance) if rv_bal else 0.0
                        for cur_t in ts:
                            mr_dec = WorkflowDecision(
                                tenant_id=cur_t.id,
                                tenant_name=cur_t.full_name,
                                property_unit=prop_unit,
                                balance=bal_val,
                                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                                reason=f"Unable/unwilling to pay (\"{ai_res.raw_text}\") — moved to Manual Review / Disputes for staff follow-up",
                                details={
                                    "intent": "UNABLE_TO_PAY",
                                    "raw_text": ai_res.raw_text,
                                    "channel": "SMS",
                                    "classification_reason": "Unable or unwilling to pay"
                                },
                                timestamp=datetime.utcnow().isoformat(),
                                run_id="quo_sync"
                            )
                            db.save_decision(mr_dec)
                        logger.info(f"[Quo Unable to Pay] Flagged for staff follow-up for {t_name}: \"{ai_res.raw_text}\"")

                    # Scenario 4: Dispute or maintenance withholding
                    elif ai_res.intent == IntentType.DISPUTE:
                        rv_bal = db.get_rentvine_record(primary_t.id)
                        bal_val = float(rv_bal.balance) if rv_bal else 0.0
                        for cur_t in ts:
                            mr_dec = WorkflowDecision(
                                tenant_id=cur_t.id,
                                tenant_name=cur_t.full_name,
                                property_unit=prop_unit,
                                balance=bal_val,
                                outcome=DecisionOutcome.FLAG_MANUAL_REVIEW,
                                reason=f"Dispute detected via Quo SMS: \"{ai_res.raw_text}\"",
                                details={
                                    "intent": "DISPUTE",
                                    "raw_text": ai_res.raw_text,
                                    "channel": "SMS",
                                    "classification_reason": "Dispute / maintenance withholding"
                                },
                                timestamp=datetime.utcnow().isoformat(),
                                run_id="quo_sync"
                            )
                            db.save_decision(mr_dec)
                        logger.info(f"[Quo Dispute] Dispute detected for {t_name}: \"{ai_res.raw_text}\"")

                    # Scenario 5: Already paid claim
                    elif ai_res.intent == IntentType.ALREADY_PAID:
                        rv_bal = db.get_rentvine_record(primary_t.id)
                        bal_val = float(rv_bal.balance) if rv_bal else 0.0
                        for cur_t in ts:
                            paid_dec = WorkflowDecision(
                                tenant_id=cur_t.id,
                                tenant_name=cur_t.full_name,
                                property_unit=prop_unit,
                                balance=bal_val,
                                outcome=DecisionOutcome.SKIP_PAID,
                                reason=f"Tenant claims payment completed via Quo SMS (\"{ai_res.raw_text}\")",
                                details={
                                    "intent": "ALREADY_PAID",
                                    "raw_text": ai_res.raw_text,
                                    "channel": "SMS"
                                },
                                timestamp=datetime.utcnow().isoformat(),
                                run_id="quo_sync"
                            )
                            db.save_decision(paid_dec)
                        logger.info(f"[Quo Paid Claim] Tenant claims payment completed: {t_name}")

                    # Scenario 6: Unrelated messages or general questions do not modify reminder status

        # ─── 2. Build reminders queue from REAL workflow decisions and live overdue accounts ───
        # Strictly associated with Tenants & Vendors line ((980) 300-7559)
        # Also includes HOLD_PROMISE tenants so they remain visible with their promise status
        latest_decisions = db.get_latest_decisions(limit=2000)
        seen_decision_tids = {d.tenant_id for d in latest_decisions}
        all_eval_items = list(latest_decisions)

        # Ensure all overdue tenants are evaluated even if no daily batch run was saved
        for t_obj in all_tenants:
            if t_obj.id not in seen_decision_tids:
                rv_rec = db.get_rentvine_record(t_obj.id)
                b_val = float(rv_rec.balance) if rv_rec else 0.0
                if b_val > 0.0:
                    all_eval_items.append(WorkflowDecision(
                        tenant_id=t_obj.id,
                        tenant_name=t_obj.full_name,
                        property_unit=f"{t_obj.property_name} {t_obj.unit_number}".strip(),
                        balance=b_val,
                        outcome=DecisionOutcome.SEND_REMINDER,
                        reason="Unpaid balance past grace period",
                        details={},
                        timestamp=datetime.utcnow().isoformat(),
                        run_id="live_evaluation"
                    ))
                    seen_decision_tids.add(t_obj.id)

        reminders_needed = []
        promises_on_hold = []
        manual_review_tenants = []
        cooldown_tenants = []
        seen_tenants = set()

        for d in all_eval_items:
            if d.tenant_id in seen_tenants:
                continue
            seen_tenants.add(d.tenant_id)

            t = tenant_map.get(d.tenant_id)
            if not t:
                continue  # skip orphaned decision records

            # Skip co-tenant secondary decision records so only one consolidated group row appears per lease
            d_details = d.details if isinstance(d.details, dict) else {}
            if isinstance(d.details, str):
                try:
                    import json as _j
                    d_details = _j.loads(d.details)
                except Exception:
                    d_details = {}
            if d_details.get("is_co_tenant"):
                continue

            # Only include tenants from the overdue whitelist (when available)
            if overdue_tenant_ids and d.tenant_id not in overdue_tenant_ids:
                continue

            # Safety check: ensure current balance is outstanding (> $0.00)
            rv = db.get_rentvine_record(d.tenant_id)
            effective_balance = float(rv.balance) if rv else float(d.balance or 0)
            if effective_balance <= 0.0:
                continue  # Never display or queue reminders for $0 balance

            clean_prop = clean_property_address(t.property_name or d.property_unit or "")
            unit_val = (t.unit_number or "").strip()
            clean_unit = clean_property_address(unit_val)
            if (
                unit_val
                and unit_val != clean_prop
                and clean_unit != clean_prop
                and unit_val not in clean_prop
                and not unit_val.startswith("NONE_")
            ):
                prop_display = f"{clean_prop} {unit_val}".strip()
            else:
                prop_display = clean_prop
            fu_text = getattr(rv, "follow_up_notes", "") or ""

            # ── Multi-Tenant Group Handling ──
            group_tenants = []
            lease_id_val = t.lease_id
            if not lease_id_val:
                lease_id_val = d_details.get("lease_id") or d_details.get("group_lease_id")
            if not lease_id_val and d.tenant_id.startswith("rv_lease_"):
                lease_id_val = d.tenant_id.replace("rv_lease_", "").split("_")[0]

            if lease_id_val:
                group_tenants = db.get_co_tenants_for_lease(lease_id_val)
            if not group_tenants:
                group_tenants = [t]

            from src.clients.quo_sms import normalize_phone_e164, phone_to_10digit
            # Normalize and deduplicate destination phone numbers across the lease
            phones_list = []
            seen_p10 = set()
            for x in group_tenants:
                if x.phone and x.phone.strip():
                    p10 = phone_to_10digit(x.phone)
                    e164 = normalize_phone_e164(x.phone) or x.phone.strip()
                    if p10 and p10 not in seen_p10:
                        seen_p10.add(p10)
                        phones_list.append(e164)

            # Skip only if lease has no valid phone at all
            if not phones_list and (not t.phone or t.phone.startswith("+1-415-555")):
                continue

            phone_display = ", ".join(phones_list) if phones_list else t.phone
            is_multi = len(group_tenants) > 1
            if is_multi:
                from src.workflow import WorkflowOrchestrator
                greeting_names = WorkflowOrchestrator._format_tenant_names_list([x.greeting_name for x in group_tenants])
                tenant_display_name = ", ".join(x.full_name.title() for x in group_tenants)
            else:
                greeting_names = t.greeting_name
                tenant_display_name = d.tenant_name or t.full_name

            # ── 1. Manual Review / Disputes: reminders disabled per Follow-Up Notes, AI dispute, or Low Balance ($0 < bal < $200) ──
            d_reason_lower = (d.reason or "").lower()
            is_api_glitch = "quo sms retrieval failed" in d_reason_lower or "api_failure" in str(d.details or "").lower()
            is_low_balance = (0.0 < effective_balance < 200.0)
            is_manual_review = (
                (d.outcome == DecisionOutcome.FLAG_MANUAL_REVIEW and not is_api_glitch) or 
                (fu_text and "manual review" in fu_text.lower()) or
                is_low_balance
            )
            if is_manual_review:
                if is_low_balance:
                    mr_reason = f"Total outstanding balance (${effective_balance:,.2f}) is greater than $0 but less than $200 — moved to Manual Review / Disputes."
                elif d.outcome == DecisionOutcome.FLAG_MANUAL_REVIEW:
                    mr_reason = d.reason
                else:
                    mr_reason = "RentVine Follow-Up Notes: Manual Review"

                manual_review_tenants.append({
                    "tenant_id": d.tenant_id,
                    "tenant_name": tenant_display_name,
                    "property_unit": prop_display,
                    "phone": phone_display,
                    "phones": phones_list,
                    "is_group": is_multi,
                    "balance": effective_balance,
                    "status": "MANUAL_REVIEW_DISABLED",
                    "reason": mr_reason,
                    "follow_up_notes": fu_text,
                    "timestamp": d.timestamp,
                    "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                    "sender_inbox": "Tenants & Vendors",
                })
                continue  # Disabled from automated reminder queue

            # ── 2. HOLD_PROMISE tenants: visible in log with hold status ──
            if d.outcome == DecisionOutcome.HOLD_PROMISE:
                promise_date = None
                next_eligible = None
                details = d.details or {}
                if isinstance(details, str):
                    try:
                        import json as _json
                        details = _json.loads(details)
                    except Exception:
                        details = {}
                promise_date = details.get("promise_date") or details.get("extracted_date") or details.get("note_pdate")

                is_promise_active = True
                if promise_date:
                    try:
                        from datetime import timedelta as _td
                        pd_obj = datetime.strptime(str(promise_date)[:10], "%Y-%m-%d").date()
                        today_date = rules.get_pm_datetime().date()
                        if pd_obj < today_date:
                            is_promise_active = False  # Promised date has expired; resume reminder eligibility!
                        next_eligible = (pd_obj + _td(days=1)).strftime("%Y-%m-%d")
                    except Exception:
                        next_eligible = promise_date

                if is_promise_active:
                    promises_on_hold.append({
                        "tenant_id": d.tenant_id,
                        "tenant_name": tenant_display_name,
                        "property_unit": prop_display,
                        "phone": phone_display,
                        "phones": phones_list,
                        "is_group": is_multi,
                        "balance": effective_balance,
                        "status": "ON_HOLD_PAYMENT_PROMISED",
                        "hold_reason": d.reason,
                        "promise_date": promise_date,
                        "next_eligible_reminder_date": next_eligible,
                        "timestamp": d.timestamp,
                        "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                        "sender_inbox": "Tenants & Vendors",
                    })
                    continue  # Active promise: suppress reminder
                else:
                    logger.info(
                        f"Payment promise for {d.tenant_id} expired on {promise_date}; "
                        "resuming reminder eligibility."
                    )

            # ── 3. 24-Hour Deduplication Cooldown Check ──
            # When a reminder was dispatched within the last 24 hours, tenant is in cooldown
            is_cooldown, last_rem, hours_elapsed, hours_remaining, expires_at = db.get_cooldown_status(
                d.tenant_id, current_time=datetime.utcnow(), cooldown_hours=24.0
            )
            if is_cooldown and last_rem:
                dispatch_ts = last_rem.timestamp
                try:
                    dt_obj = datetime.fromisoformat(dispatch_ts.replace("Z", "+00:00"))
                    disp_fmt = dt_obj.strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    disp_fmt = dispatch_ts
                exp_fmt = expires_at.strftime("%Y-%m-%d %I:%M %p") if expires_at else "In 24 hours"
                last_txt = (last_rem.details or {}).get("text") or (last_rem.details or {}).get("content") or ""

                cooldown_tenants.append({
                    "tenant_id": d.tenant_id,
                    "tenant_name": tenant_display_name,
                    "property_unit": prop_display,
                    "due_date": rv.due_date if rv and rv.due_date else "November 1, 2026",
                    "phone": phone_display,
                    "phones": phones_list,
                    "is_group": is_multi,
                    "balance": effective_balance,
                    "status": "IN_COOLDOWN",
                    "dispatch_time": dispatch_ts,
                    "dispatch_time_display": disp_fmt,
                    "hours_elapsed": hours_elapsed,
                    "hours_remaining": hours_remaining,
                    "expires_at": expires_at.isoformat() if expires_at else "",
                    "expires_at_display": exp_fmt,
                    "reason": f"Reminder sent {hours_elapsed:.1f}h ago ({disp_fmt}). In 24h deduplication cooldown ({hours_remaining:.1f}h remaining until {exp_fmt}).",
                    "last_message": last_txt,
                    "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                    "sender_inbox": "Tenants & Vendors"
                })
                continue

            # ── 4. Automatic Grace Period evaluation (Days 1–4 of month) ──
            from src.engine.rules import BusinessRulesEngine
            rules = BusinessRulesEngine()
            is_in_grace, grace_msg = rules.check_grace_period(rv, datetime.utcnow())
            if is_in_grace or d.outcome == DecisionOutcome.SKIP_GRACE_PERIOD:
                cooldown_tenants.append({
                    "tenant_id": d.tenant_id,
                    "tenant_name": tenant_display_name,
                    "property_unit": prop_display,
                    "due_date": rv.due_date if rv and rv.due_date else "November 1, 2026",
                    "phone": phone_display,
                    "phones": phones_list,
                    "is_group": is_multi,
                    "balance": effective_balance,
                    "status": "IN_GRACE_PERIOD",
                    "dispatch_time": None,
                    "dispatch_time_display": "—",
                    "hours_elapsed": 0.0,
                    "hours_remaining": 0.0,
                    "expires_at": "",
                    "expires_at_display": "Day 5 of month",
                    "reason": grace_msg or "Within 5-day grace period (Days 1–4 of billing cycle)",
                    "last_message": "",
                    "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                    "sender_inbox": "Tenants & Vendors"
                })
                continue

            # ── 5. Reminders Needed: Eligible tenants who have not received a reminder in 24 hours ──
            current_pm_dt = rules.get_pm_datetime()
            stage = rules.get_reminder_stage(current_pm_dt)
            portal = settings.RENTVINE_PORTAL_PAYMENT_URL
            due = rv.due_date if rv and rv.due_date else "November 1, 2026"
            effective_bal = float(d.balance if d.balance is not None else (rv.balance if rv else 0.0))

            if stage == "RENT_REMINDER_STAGE_2_ESCALATION":
                # Day 6: Second-stage escalation reminder (10-day notice). Must NEVER fall back to standard template.
                is_esc, esc_why = rules.check_escalation_eligibility(t, rv, current_pm_dt)
                if not is_esc:
                    logger.info("Escalation suppressed on Day 6 for %s: %s", t.id, esc_why)
                    continue

                if is_multi:
                    deduped_esc_names = []
                    for n in [x.greeting_name for x in group_tenants]:
                        if n and n not in deduped_esc_names:
                            deduped_esc_names.append(n)
                    esc_names_str = ", ".join(deduped_esc_names)
                    sms_text = settings.GROUP_ESCALATION_REMINDER_TEMPLATE.format(
                        tenant_names=esc_names_str,
                        balance=effective_bal,
                        property_name=clean_prop,
                        unit_number=t.unit_number or "",
                        property_address=prop_display,
                        due_date=due,
                        portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL
                    )
                else:
                    sms_text = settings.ESCALATION_REMINDER_TEMPLATE.format(
                        tenant_name=t.greeting_name,
                        balance=effective_bal,
                        property_name=clean_prop,
                        unit_number=t.unit_number or "",
                        property_address=prop_display,
                        due_date=due,
                        portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL
                    )
                rem_type = "RENT_REMINDER_STAGE_2_ESCALATION"
                stage_num = 2
                stage_label = "STAGE 2 - ESCALATION (10-DAY NOTICE)"
            else:
                if is_multi:
                    sms_text = settings.GROUP_REMINDER_TEMPLATE.format(
                        tenant_names=greeting_names,
                        balance=effective_bal,
                        property_name=clean_prop,
                        unit_number=t.unit_number or "",
                        property_address=prop_display,
                        due_date=due,
                        portal_link=portal
                    )
                else:
                    sms_text = settings.REMINDER_TEMPLATE.format(
                        tenant_name=t.greeting_name,
                        balance=effective_bal,
                        property_name=clean_prop,
                        unit_number=t.unit_number or "",
                        property_address=prop_display,
                        due_date=due,
                        portal_link=portal
                    )
                rem_type = "RENT_REMINDER_STAGE_1" if stage == "RENT_REMINDER_STAGE_1" else "RENT_REMINDER"
                stage_num = 1
                stage_label = "STAGE 1 - STANDARD REMINDER" if stage == "RENT_REMINDER_STAGE_1" else "STANDARD REMINDER"

            reminder_id = f"rem_{d.tenant_id}_{datetime.utcnow().strftime('%Y%m%d')}"
            reminders_needed.append({
                "reminder_id": reminder_id,
                "reminder_type": rem_type,
                "stage": stage_num,
                "stage_label": stage_label,
                "workflow_id": d.run_id or "daily_workflow",
                "tenant_id": d.tenant_id,
                "tenant_name": tenant_display_name,
                "property_unit": prop_display,
                "due_date": due,
                "phone": phone_display,
                "phones": phones_list,
                "is_group": is_multi,
                "balance": effective_bal,
                "status": "AWAITING_HITL_APPROVAL",
                "sms_preview": sms_text,
                "sender_phone": settings.QUO_PHONE_TENANTS_FORMATTED,
                "sender_inbox": "Tenants & Vendors",
                "sender_phone_id": settings.QUO_PHONE_TENANTS_ID,
                "recipient_type": "tenant",
                "reason": d.reason,
                "timestamp": d.timestamp
            })

        # ─── 3. Build SMS message history: HITL-approved + any OpenPhone live msgs ───
        # Local DB messages (HITL approved or recorded) via single bulk query
        # Only include messages for overdue tenants (when whitelist is available)
        local_sms = []
        recent_db_msgs = db.get_all_recent_messages(limit=150, channel=Channel.SMS)
        for m in recent_db_msgs:
            if m.status == "REMINDER_NEEDED (TESTING_MODE)":
                continue  # Never display internal suppressed testing simulations in live message history
            # Filter to overdue tenants when whitelist is available
            if overdue_tenant_ids and m.tenant_id and m.tenant_id not in overdue_tenant_ids:
                continue
            t = tenant_map.get(m.tenant_id)
            t_name = t.full_name if t else m.tenant_id
            prop_unit = f"{t.property_name} {t.unit_number}".strip() if t else ""
            local_sms.append({
                "id": m.id,
                "tenant_id": m.tenant_id,
                "tenant_name": t_name,
                "property_unit": prop_unit,
                "direction": m.direction.value,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "timestamp": m.timestamp,
                "status": m.status,
                "metadata": m.metadata or {},
                "source": "local_db"
            })

        # Merge: live OpenPhone msgs first (most recent), then local DB
        all_messages = openphone_messages + local_sms
        # Deduplicate by content+timestamp
        seen_msgs = set()
        deduped = []
        for m in all_messages:
            key = (m.get("content", "")[:80], m.get("timestamp", "")[:16])
            if key not in seen_msgs:
                seen_msgs.add(key)
                deduped.append(m)

        deduped.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        inbound_count = len([m for m in deduped if m.get("direction") == "INBOUND"])
        hitl_sent = len([m for m in local_sms if "HITL_APPROVED" in (m.get("status") or "") or "SENT_HITL" in (m.get("status") or "")])

        available_lines = [
            {
                "key": "tenants",
                "name": "Tenants & Vendors",
                "number": settings.QUO_PHONE_TENANTS_FORMATTED,
                "id": settings.QUO_PHONE_TENANTS_ID,
                "purpose": "Tenant rent reminders & payment follow-ups",
                "is_active_for_rent_reminders": True,
                "is_current": selected_line == "tenants"
            },
            {
                "key": "landlords",
                "name": "Landlords",
                "number": settings.QUO_PHONE_LANDLORDS_FORMATTED,
                "id": settings.QUO_PHONE_LANDLORDS_ID,
                "purpose": "Landlord communications & updates",
                "is_active_for_rent_reminders": False,
                "is_current": selected_line == "landlords"
            },
            {
                "key": "primary",
                "name": "Primary",
                "number": settings.QUO_PHONE_PRIMARY_FORMATTED,
                "id": settings.QUO_PHONE_PRIMARY_ID,
                "purpose": "General inbox (isolated from rent reminders)",
                "is_active_for_rent_reminders": False,
                "is_current": selected_line == "primary"
            }
        ]

        resp_data = {
            "mode": "hitl_testing",
            "hitl_status": "LOCKED — Explicit HITL approval required per reminder",
            "selected_line": selected_line,
            "overdue_filter": {
                "source": "RentVine Report #72 (Rent Rem Praveen)",
                "overdue_tenant_count": len(overdue_phones),
                "conversations_matched": matched_overdue_convs,
                "total_messages_retrieved": len(openphone_messages),
                "api_status": quo_api_status,
                "api_error": quo_api_error,
                "filtering_active": bool(overdue_phones)
            },
            "channel": {
                "name": f"Quo (OpenPhone) SMS — {inbox_name}",
                "inbox_name": inbox_name,
                "phone_number": phone_number,
                "phone_number_id": phone_id,
                "purpose": line_purpose,
                "api_endpoint": settings.QUO_API_URL,
                "live_status": quo_probe.get("status", "simulation"),
                "openphone_conversations": matched_overdue_convs,
                "openphone_messages_fetched": len(openphone_messages)
            },
            "available_lines": available_lines,
            "counts": {
                "reminders_needed": len(reminders_needed),
                "promises_on_hold": len(promises_on_hold),
                "manual_review": len(manual_review_tenants),
                "in_cooldown": len(cooldown_tenants),
                "sms_messages": len(deduped),
                "inbound_replies": inbound_count,
                "hitl_approved_sent": hitl_sent
            },
            "reminders_needed": reminders_needed,
            "promises_on_hold": promises_on_hold,
            "manual_review_tenants": manual_review_tenants,
            "cooldown_tenants": cooldown_tenants,
            "messages": deduped,
            "probe": quo_probe
        }
        _API_CACHE[cache_key] = (now_ts, resp_data)
        self._send_json(resp_data)


    def _handle_post_run_workflow(self, body: Dict[str, Any]):
        """Executes the daily automated workflow run."""
        is_dry_run = bool(body.get("is_dry_run", False))
        ref_date_str = body.get("reference_date")
        ref_time = None
        if ref_date_str:
            try:
                ref_time = datetime.strptime(ref_date_str, "%Y-%m-%d")
            except Exception:
                pass

        summary = self.orchestrator.execute_daily_run(
            is_dry_run=is_dry_run,
            reference_time=ref_time
        )
        self._send_json({
            "success": True,
            "summary": summary.to_dict()
        })

    def _handle_post_simulate_reply(self, body: Dict[str, Any]):
        """
        Simulates an incoming tenant reply (via Quo SMS or Gmail), runs AI classifier,
        and optionally updates RentVine notes and holds reminders.
        """
        tenant_id = body.get("tenant_id")
        text = body.get("text", "").strip()
        channel_str = body.get("channel", "SMS").upper()
        apply_to_db = bool(body.get("apply_to_db", True))

        if not tenant_id or not text:
            return self._send_json({"error": "tenant_id and text are required"}, 400)

        tenants = db.get_all_tenants()
        matched = [t for t in tenants if t.id == tenant_id]
        if not matched:
            return self._send_json({"error": "Tenant not found"}, 404)
        tenant = matched[0]

        channel = Channel.SMS if channel_str == "SMS" else Channel.EMAIL
        direction = MessageDirection.INBOUND

        sim_msg = CommunicationMessage(
            id=f"sim_{int(datetime.utcnow().timestamp() * 1000)}",
            tenant_id=tenant_id,
            channel=channel,
            direction=direction,
            sender=tenant.phone if channel == Channel.SMS else tenant.email,
            recipient="+1-800-REDBRICKS" if channel == Channel.SMS else "rentals@redbrickpm.com",
            content=text,
            timestamp=datetime.utcnow().isoformat(),
            status="DELIVERED",
            metadata={"simulated": True}
        )

        if apply_to_db:
            db.save_message(sim_msg)

        # Run AI classification
        classifier = AIIntentClassifier()
        result = classifier.classify_tenant_communications([sim_msg])

        decision_preview = None
        rv_note_text = None

        if result.intent == IntentType.PROMISE_MADE:
            pdate = result.extracted_date or (datetime.utcnow().date()).strftime("%Y-%m-%d")
            rv_note_text = f"[AI PAYMENT PROMISE ON FILE: Will pay by {pdate}] Tenant confirmed via {channel.value}: \"{text}\""
            decision_preview = {
                "outcome": DecisionOutcome.HOLD_PROMISE.value,
                "reason": f"Payment promise detected: will pay by {pdate}",
                "action": "Hold SMS reminders until promised date and update RentVine notes"
            }
            if apply_to_db:
                RentVineMCPClient().write_tenant_note(tenant_id, rv_note_text, author="AI Rent Reminder Agent")

        elif result.intent == IntentType.PAYMENT_UNSPECIFIED:
            rv_note_text = f"[MANUAL REVIEW - NO PAYMENT DATE] Tenant replied: \"{text}\". Flagged for Manual Review / Disputes."
            decision_preview = {
                "outcome": DecisionOutcome.FLAG_MANUAL_REVIEW.value,
                "reason": f"Payment reply without specific date (\"{text}\") — moved to Manual Review / Disputes",
                "action": "Escalate to staff for manual review"
            }
            if apply_to_db:
                RentVineMCPClient().write_tenant_note(tenant_id, rv_note_text, author="AI Rent Reminder Agent")

        elif result.intent == IntentType.UNABLE_TO_PAY:
            rv_note_text = f"[MANUAL REVIEW - UNABLE TO PAY] Tenant stated unable/unwilling to pay: \"{text}\". Flagged for staff follow-up."
            decision_preview = {
                "outcome": DecisionOutcome.FLAG_MANUAL_REVIEW.value,
                "reason": f"Unable/unwilling to pay (\"{text}\") — moved to Manual Review / Disputes for staff follow-up",
                "action": "Staff follow-up required"
            }
            if apply_to_db:
                RentVineMCPClient().write_tenant_note(tenant_id, rv_note_text, author="AI Rent Reminder Agent")

        elif result.intent == IntentType.DISPUTE:
            rv_note_text = f"[DISPUTE DETECTED] Tenant response: \"{text}\". Pausing auto-reminders."
            decision_preview = {
                "outcome": DecisionOutcome.FLAG_MANUAL_REVIEW.value,
                "reason": f"Dispute detected: {result.reasoning}",
                "action": "Escalate to property management staff"
            }
            if apply_to_db:
                RentVineMCPClient().write_tenant_note(tenant_id, rv_note_text, author="AI Rent Reminder Agent")

        elif result.intent == IntentType.ALREADY_PAID:
            rv_note_text = f"[PAYMENT CLAIMED] Tenant states rent already paid: \"{text}\"."
            decision_preview = {
                "outcome": DecisionOutcome.SKIP_PAID.value,
                "reason": "Tenant claims payment completed - awaiting clearing",
                "action": "Hold reminder pending bank verification"
            }
            if apply_to_db:
                RentVineMCPClient().write_tenant_note(tenant_id, rv_note_text, author="AI Rent Reminder Agent")

        else:
            decision_preview = {
                "outcome": "CONTINUE_STANDARD_RULES",
                "reason": "Unrelated conversation or generic message; does not change reminder eligibility",
                "action": "Evaluate standard balance and grace period"
            }

        self._send_json({
            "success": True,
            "classification": {
                "intent": result.intent.value,
                "confidence": result.confidence,
                "extracted_date": result.extracted_date,
                "extracted_amount": result.extracted_amount,
                "reasoning": result.reasoning
            },
            "decision_preview": decision_preview,
            "note_written": bool(rv_note_text and apply_to_db),
            "note_text": rv_note_text
        })

    def _handle_post_send_sms(self, body: Dict[str, Any]):
        tenant_id = body.get("tenant_id")
        text = body.get("text")
        recipient_type = body.get("recipient_type", "tenant")
        if not tenant_id or not text:
            return self._send_json({"error": "tenant_id and text required"}, 400)

        tenants = db.get_all_tenants()
        matched = [t for t in tenants if t.id == tenant_id]
        if not matched:
            return self._send_json({"error": "Tenant not found"}, 404)
        t = matched[0]

        client = QuoSMSClient()
        res = client.send_sms(tenant_id, t.phone, text, recipient_type=recipient_type)
        self._send_json(res)

    def _handle_post_pipeline_dispatch_approved(self, body: Dict[str, Any]):
        try:
            approved = body.get("approved_tenants", [])
            from src.pipeline import run_pipeline_dispatch
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
                json.dump(approved, tf)
                temp_path = tf.name
            receipt = run_pipeline_dispatch(input_file=temp_path)
            try:
                os.remove(temp_path)
            except Exception:
                pass
            self._send_json(receipt)
        except Exception as e:
            logger.error(f"Failed to dispatch approved: {e}", exc_info=True)
            self._send_json({"error": str(e)}, 500)

    def _handle_post_pipeline_dispatch_approved(self, body: Dict[str, Any]):
        try:
            approved = body.get("approved_tenants", [])
            from src.pipeline import run_pipeline_dispatch
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
                json.dump(approved, tf)
                temp_path = tf.name
            receipt = run_pipeline_dispatch(input_file=temp_path)
            try:
                os.remove(temp_path)
            except Exception:
                pass
            self._send_json(receipt)
        except Exception as e:
            logger.error(f"Failed to dispatch approved: {e}", exc_info=True)
            self._send_json({"error": str(e)}, 500)

    def _handle_post_send_reminder(self, body: Dict[str, Any]):
        """
        HITL-approved SMS reminder dispatch.
        Only called when a property manager explicitly clicks 'Send Reminder' in the dashboard.
        Dispatches to Quo (OpenPhone) API using the appropriate sender line:
          - Tenants: (980) 300-7559 [PNnMjdTFVO] (Tenants & Vendors)
          - Landlords: (980) 300-1196 [PNTDARdwBh] (Landlords)
        """
        tenant_id = body.get("tenant_id")
        phone = body.get("phone")
        phones = body.get("phones")
        if not phone and phones:
            phone = phones[0] if isinstance(phones, list) else str(phones)
        text = body.get("text")
        recipient_type = body.get("recipient_type", "tenant")
        sender_phone_override = body.get("sender_phone_id")
        hitl_approved = bool(body.get("hitl_approved", False))
        reminder_id = body.get("reminder_id") or f"rem_{tenant_id}_{int(__import__('datetime').datetime.utcnow().timestamp() * 1000)}"
        reminder_type = body.get("reminder_type", "RENT_REMINDER")
        workflow_id = body.get("workflow_id", "hitl_send")

        if not tenant_id or not phone or not text:
            return self._send_json({"error": "tenant_id, phone, and text are required"}, 400)

        if not hitl_approved:
            return self._send_json({"error": "HITL approval flag must be set to dispatch reminder"}, 403)

        # Payment status verification: ensure rent is still unpaid
        rv_rec = db.get_rentvine_record(tenant_id)
        if rv_rec and float(rv_rec.balance or 0.0) <= 0.0:
            return self._send_json({
                "error": "Payment verified: Tenant balance is $0.00 (paid in full). No reminder sent.",
                "status": "PAID"
            }, 400)

        # Manual Review verification: ensure tenant is not marked for Manual Review
        if rv_rec and rv_rec.follow_up_notes and "manual review" in rv_rec.follow_up_notes.lower():
            return self._send_json({
                "error": "Cannot dispatch automated reminder: Tenant is marked for Manual Review in RentVine Follow-Up Notes.",
                "status": "FLAG_MANUAL_REVIEW"
            }, 400)

        cur_t = db.get_tenant_by_id(tenant_id)

        # Low-balance threshold verification: $0 < balance < $200 automatically moved to Manual Review / Disputes
        check_bal = float(rv_rec.balance) if rv_rec else (float(cur_t.balance or 0.0) if hasattr(cur_t, 'balance') else 0.0)
        if 0.0 < check_bal < 200.0:
            return self._send_json({
                "error": f"Cannot dispatch automated reminder: Tenant balance (${check_bal:,.2f}) is greater than $0 but less than $200 and belongs in Manual Review / Disputes.",
                "status": "FLAG_MANUAL_REVIEW"
            }, 400)

        # Automatic Grace Period verification for current-month rent dues
        from src.engine.rules import BusinessRulesEngine
        rules = BusinessRulesEngine()
        is_in_grace, grace_msg = rules.check_grace_period(rv_rec, datetime.utcnow())
        if is_in_grace:
            return self._send_json({
                "error": f"Cannot send reminder: {grace_msg}",
                "status": "IN_GRACE_PERIOD"
            }, 400)

        # ── Stage 2 Escalation & Cooldown Verification ──
        is_escalation = (
            "STAGE_2" in reminder_type.upper()
            or "ESCALATION" in reminder_type.upper()
            or "10-day notice" in text.lower()
        )
        stage_val = 2 if is_escalation else 1

        if is_escalation:
            # 1. Verification: tenant received first-stage reminder, and dispatch was confirmed
            now_dt = datetime.utcnow()
            has_stage_1 = db.has_received_stage_1_reminder(tenant_id, now_dt.year, now_dt.month)
            if not has_stage_1:
                return self._send_json({
                    "error": "Cannot dispatch escalation reminder: Tenant has not received a confirmed Stage 1 rent reminder for this billing cycle.",
                    "status": "STAGE_1_REQUIRED"
                }, 400)

            # 2. Verification: On Day 6 specifically, prevent duplicate escalation messages on the same day
            pm_dt = rules.get_pm_datetime()
            if pm_dt.day == 6:
                has_escalated = db.has_received_escalation_reminder(tenant_id, now_dt.year, now_dt.month)
                if has_escalated:
                    return self._send_json({
                        "error": "Cannot dispatch escalation reminder: Stage 2 escalation has already been dispatched for this billing cycle.",
                        "status": "DUPLICATE_ESCALATION"
                    }, 400)

        # Check 24-hour deduplication cooldown
        is_cooldown, last_rem, cd_reason = rules.check_recent_reminder_deduplication(tenant_id, datetime.utcnow())
        if is_cooldown:
            return self._send_json({
                "error": f"Cannot dispatch reminder: {cd_reason}",
                "status": "IN_COOLDOWN"
            }, 400)

        # Resolve sender phone number and inbox based on recipient type
        if sender_phone_override:
            if sender_phone_override == settings.QUO_PHONE_LANDLORDS_ID:
                sender_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender("landlord")
            elif sender_phone_override == settings.QUO_PHONE_PRIMARY_ID:
                sender_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender("primary")
            else:
                sender_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender("tenant")
        else:
            sender_id, sender_e164, sender_fmt, inbox_name = settings.get_quo_sender(recipient_type)

        # Collect all verified destination phone numbers (support multi-tenant lease group)
        dest_phones = []
        raw_phones = body.get("phones")
        if isinstance(raw_phones, list):
            dest_phones.extend([p.strip() for p in raw_phones if str(p).strip()])
        elif isinstance(raw_phones, str):
            dest_phones.extend([p.strip() for p in raw_phones.split(",") if p.strip()])

        if not dest_phones and phone:
            dest_phones.extend([p.strip() for p in str(phone).split(",") if p.strip()])

        # Also check database for co-tenants on the same lease
        cur_t = db.get_tenant_by_id(tenant_id)
        prop_display = ""
        if cur_t:
            clean_p = clean_property_address(cur_t.property_name or "")
            unit_v = (cur_t.unit_number or "").strip()
            clean_u = clean_property_address(unit_v)
            if unit_v and unit_v != clean_p and clean_u != clean_p and unit_v not in clean_p and not unit_v.startswith("NONE_"):
                prop_display = f"{clean_p} {unit_v}".strip()
            else:
                prop_display = clean_p
        co_members = []
        if cur_t and cur_t.lease_id:
            co_members = db.get_co_tenants_for_lease(cur_t.lease_id)
            for cm in co_members:
                if cm.phone and cm.phone.strip() and cm.phone.strip() not in dest_phones:
                    dest_phones.append(cm.phone.strip())

        if not dest_phones and phone:
            dest_phones = [phone.strip()]

        try:
            now_iso = datetime.utcnow().isoformat()
            sent_msg_ids = []
            last_ext_id = None
            last_api_resp = {}
            from src.models import CommunicationMessage, Channel, MessageDirection, WorkflowDecision, DecisionOutcome

            from src.clients.quo_sms import normalize_phone_e164
            # Normalize and deduplicate all destination phone numbers to E.164
            normalized_recipients = []
            for dp in dest_phones:
                e164_p = normalize_phone_e164(dp)
                if e164_p and e164_p not in normalized_recipients:
                    normalized_recipients.append(e164_p)
            if not normalized_recipients and dest_phones:
                normalized_recipients = dest_phones

            is_group_chat = len(normalized_recipients) > 1
            recip_display = ", ".join(normalized_recipients)

            if not settings.QUO_MOCK_MODE and settings.QUO_API_KEY:
                # ── LIVE DISPATCH via Quo (OpenPhone) REST API ──
                import urllib.request as _ur, urllib.error as _ue
                auth = settings.QUO_API_KEY if settings.QUO_API_KEY.startswith("Bearer ") else f"Bearer {settings.QUO_API_KEY}"

                # DISPATCH ONE GROUP MESSAGE containing all tenant numbers in `to` array!
                # In OpenPhone, passing multiple numbers in `to` creates / uses the GROUP MMS thread.
                sub_msg_id = f"hitl_{int(datetime.utcnow().timestamp() * 1000)}"
                payload = json.dumps({
                    "content": text,
                    "from": sender_id,
                    "to": normalized_recipients
                }).encode("utf-8")
                req = _ur.Request(
                    f"{settings.QUO_API_URL}/messages",
                    data=payload,
                    headers={
                        "Authorization": auth,
                        "Content-Type": "application/json",
                        "User-Agent": "RedBricks-HITL/1.0"
                    },
                    method="POST"
                )
                ext_id = sub_msg_id
                try:
                    with _ur.urlopen(req, timeout=10) as resp:
                        api_resp = json.loads(resp.read().decode("utf-8"))
                        ext_id = api_resp.get("data", {}).get("id", sub_msg_id)
                        last_ext_id = ext_id
                        last_api_resp = api_resp
                        sent_msg_ids.append(ext_id)
                except Exception as he:
                    logger.error(f"[HITL Error] Failed dispatching group message to {normalized_recipients}: {he}")
                    return self._send_json({
                        "error": f"Failed to dispatch SMS via Quo OpenPhone API: {he}",
                        "status": "DISPATCH_FAILED"
                    }, 502)

                # Record the group message in local audit log
                outbound = CommunicationMessage(
                    id=ext_id,
                    tenant_id=tenant_id,
                    channel=Channel.SMS,
                    direction=MessageDirection.OUTBOUND,
                    sender=sender_fmt,
                    recipient=recip_display,
                    content=text,
                    timestamp=now_iso,
                    status="SENT_HITL_APPROVED",
                    metadata={
                        "source": "Quo REST API",
                        "provider": "OpenPhone",
                        "inbox": inbox_name,
                        "sender_number": sender_fmt,
                        "sender_phone_id": sender_id,
                        "recipient_type": recipient_type,
                        "hitl_approved": True,
                        "sms_dispatched": True,
                        "ext_message_id": ext_id,
                        "reminder_id": reminder_id,
                        "reminder_type": reminder_type,
                        "workflow_id": workflow_id,
                        "is_workflow_reminder": True,
                        "is_group_reminder": is_group_chat,
                        "participants": normalized_recipients,
                        "delivery_status": "DELIVERED"
                    }
                )
                db.save_message(outbound)
                logger.info(f"[HITL APPROVED] Group SMS sent to {recip_display} from {sender_fmt} ({inbox_name}) for lease {tenant_id}. ext_id={ext_id}")

                # Record in dedicated workflow_reminders tracking table for each participant
                for dest_p in normalized_recipients:
                    match_tid = tenant_id
                    for cm in co_members:
                        if cm.phone and normalize_phone_e164(cm.phone) == dest_p:
                            match_tid = cm.id
                            break
                    db.record_workflow_reminder(
                        reminder_id=f"{reminder_id}_{dest_p[-4:]}",
                        tenant_id=match_tid,
                        reminder_type=reminder_type,
                        message_id=ext_id,
                        workflow_id=workflow_id,
                        phone=dest_p,
                        content=text,
                        sender_phone=sender_fmt,
                        delivery_status="DELIVERED",
                        sent_at=now_iso,
                        stage=stage_val
                    )

                # Record decision so cooldown activates for primary and all co-tenants
                expires_at = datetime.utcnow() + timedelta(hours=24)
                all_target_tids = [tenant_id] + [cm.id for cm in co_members if cm.id != tenant_id]
                for tid in all_target_tids:
                    t_obj = db.get_tenant_by_id(tid)
                    t_name = t_obj.full_name if t_obj else tid
                    t_bal = float(rv_rec.balance or 0.0) if rv_rec else 0.0
                    db.save_decision(WorkflowDecision(
                        tenant_id=tid,
                        tenant_name=t_name,
                        property_unit=prop_display,
                        balance=t_bal,
                        outcome=DecisionOutcome.SKIP_ALREADY_REMINDED,
                        reason=f"HITL Group Reminder SMS approved and sent to group {recip_display} from {sender_fmt} ({inbox_name})",
                        details={
                            "text": text,
                            "phones": normalized_recipients,
                            "sender_phone": sender_fmt,
                            "inbox": inbox_name,
                            "hitl_approved": True,
                            "reminder_id": reminder_id,
                            "reminder_type": reminder_type,
                            "workflow_id": workflow_id,
                            "is_workflow_reminder": True,
                            "is_group_reminder": is_group_chat,
                            "participants": normalized_recipients,
                            "delivery_status": "DELIVERED"
                        },
                        timestamp=now_iso,
                        sms_sent=True,
                        sms_message_id=last_ext_id,
                        note_written=False,
                        run_id=workflow_id
                    ))

                # Log consolidated group note in RentVine
                if cur_t and sent_msg_ids:
                    rv_note_text = (
                        f"[GROUP RENT REMINDER APPROVED & SENT] Group SMS delivered to lease group "
                        f"({len(normalized_recipients)} recipients in one group thread: {recip_display}) "
                        f"from {sender_fmt} ({inbox_name}) via Quo OpenPhone API."
                    )
                    try:
                        RentVineMCPClient().write_tenant_note(tenant_id, rv_note_text, author="Property Manager (HITL)")
                    except Exception as ne:
                        logger.warning(f"Failed writing RentVine note after HITL group dispatch: {ne}")

                return self._send_json({
                    "success": True,
                    "mock_mode": False,
                    "message_id": last_ext_id or f"hitl_{now_iso}",
                    "sent_count": len(normalized_recipients),
                    "recipients": normalized_recipients,
                    "reminder_id": reminder_id,
                    "reminder_type": reminder_type,
                    "workflow_id": workflow_id,
                    "timestamp": now_iso,
                    "recipient": recip_display,
                    "is_group": is_group_chat,
                    "sender": sender_fmt,
                    "sender_phone_id": sender_id,
                    "inbox": inbox_name,
                    "status": "SENT_HITL_APPROVED",
                    "ext_api_response": last_api_resp
                })
            else:
                # ── MOCK / TESTING MODE — Log with HITL_APPROVED for all group phones ──
                for dest_p in dest_phones:
                    sub_msg_id = f"mock_{int(datetime.utcnow().timestamp() * 1000)}_{dest_p[-4:]}"
                    match_tid = tenant_id
                    for cm in co_members:
                        if cm.phone and cm.phone.strip() == dest_p:
                            match_tid = cm.id
                            break

                    outbound = CommunicationMessage(
                        id=sub_msg_id,
                        tenant_id=match_tid,
                        channel=Channel.SMS,
                        direction=MessageDirection.OUTBOUND,
                        sender=sender_fmt,
                        recipient=dest_p,
                        content=text,
                        timestamp=now_iso,
                        status="HITL_APPROVED_LOGGED (MOCK)",
                        metadata={
                            "source": "Quo REST API",
                            "provider": "OpenPhone",
                            "inbox": inbox_name,
                            "sender_number": sender_fmt,
                            "sender_phone_id": sender_id,
                            "recipient_type": recipient_type,
                            "hitl_approved": True,
                            "sms_dispatched": False,
                            "mock_mode": True,
                            "reminder_id": reminder_id,
                            "reminder_type": reminder_type,
                            "workflow_id": workflow_id,
                            "is_workflow_reminder": True,
                            "is_group_reminder": len(dest_phones) > 1,
                            "delivery_status": "SENT_HITL_APPROVED",
                            "note": f"HITL approved — Group SMS would be sent to {dest_p} from {sender_fmt} ({inbox_name}) if QUO_MOCK_MODE=false"
                        }
                    )
                    db.save_message(outbound)
                    logger.info(f"[HITL APPROVED - MOCK] Group SMS logged for {dest_p} from {sender_fmt} ({inbox_name})")

                    db.record_workflow_reminder(
                        reminder_id=f"{reminder_id}_{dest_p[-4:]}",
                        tenant_id=match_tid,
                        reminder_type=reminder_type,
                        message_id=sub_msg_id,
                        workflow_id=workflow_id,
                        phone=dest_p,
                        content=text,
                        sender_phone=sender_fmt,
                        delivery_status="SENT_HITL_APPROVED",
                        sent_at=now_iso,
                        stage=stage_val
                    )

                expires_at = datetime.utcnow() + timedelta(hours=24)
                all_target_tids = [tenant_id] + [cm.id for cm in co_members if cm.id != tenant_id]
                for tid in all_target_tids:
                    t_obj = db.get_tenant_by_id(tid)
                    t_name = t_obj.full_name if t_obj else tid
                    t_bal = float(rv_rec.balance or 0.0) if rv_rec else 0.0
                    db.save_decision(WorkflowDecision(
                        tenant_id=tid,
                        tenant_name=t_name,
                        property_unit=prop_display,
                        balance=t_bal,
                        outcome=DecisionOutcome.SKIP_ALREADY_REMINDED,
                        reason=f"HITL Group Reminder approved and recorded for {', '.join(dest_phones)} from {sender_fmt} ({inbox_name})",
                        details={
                            "text": text,
                            "phones": dest_phones,
                            "sender_phone": sender_fmt,
                            "inbox": inbox_name,
                            "hitl_approved": True,
                            "mock_mode": True,
                            "reminder_id": reminder_id,
                            "reminder_type": reminder_type,
                            "workflow_id": workflow_id,
                            "is_workflow_reminder": True,
                            "is_group_reminder": len(dest_phones) > 1,
                            "delivery_status": "SENT_HITL_APPROVED"
                        },
                        timestamp=now_iso,
                        sms_sent=True,
                        sms_message_id=f"mock_{now_iso}",
                        note_written=False,
                        run_id=workflow_id
                    ))

                return self._send_json({
                    "success": True,
                    "mock_mode": True,
                    "message_id": f"mock_group_{now_iso}",
                    "sent_count": len(dest_phones),
                    "recipients": dest_phones,
                    "reminder_id": reminder_id,
                    "reminder_type": reminder_type,
                    "workflow_id": workflow_id,
                    "timestamp": now_iso,
                    "recipient": ", ".join(dest_phones),
                    "sender": sender_fmt,
                    "sender_phone_id": sender_id,
                    "inbox": inbox_name,
                    "status": "HITL_APPROVED_LOGGED (MOCK)",
                    "note": f"Recorded with sender {sender_fmt} ({inbox_name})"
                })
        except _ue.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"HITL send HTTPError {e.code} for {tenant_id}: {body}")
            if e.code == 401:
                msg = "Quo (OpenPhone) Error 401: Unauthorized. The QUO_API_KEY in your .env has expired. Please copy a fresh token/API key from OpenPhone and update .env."
            elif e.code == 403:
                msg = f"Quo (OpenPhone) Error 403: Access forbidden ({body[:150]})."
            else:
                msg = f"Quo API Error {e.code}: {body[:200]}"
            return self._send_json({"error": msg, "http_code": e.code}, 400)
        except Exception as e:
            logger.error(f"HITL send error for {tenant_id}: {e}")
            return self._send_json({"error": str(e)}, 500)

    def _handle_post_add_note(self, body: Dict[str, Any]):
        tenant_id = body.get("tenant_id")
        text = body.get("text")
        author = body.get("author", "Property Manager")
        if not tenant_id or not text:
            return self._send_json({"error": "tenant_id and text required"}, 400)

        client = RentVineMCPClient()
        note = client.write_tenant_note(tenant_id, text, author)
        self._send_json({
            "success": True,
            "note": {
                "id": note.id,
                "text": note.text,
                "author": note.author,
                "created_at": note.created_at
            }
        })

    def _handle_post_reset_seed(self):
        # Purge all seed/demo tenants (T- prefix) from the local database
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(settings.DATABASE_PATH)
        _c = conn.cursor()
        for tbl in ("tenants", "rentvine_records", "workflow_decisions", "communication_messages", "rentvine_notes"):
            col = "id" if tbl == "tenants" else "tenant_id"
            _c.execute(f"DELETE FROM {tbl} WHERE {col} LIKE 'T-%'")
        conn.commit()
        conn.close()
        # Sync full live portfolio from RentVine (all 629+ leases)
        synced = RentVineMCPClient().sync_portfolio_from_rentvine(limit=1000)
        self._send_json({
            "success": True,
            "message": f"Demo data removed. Synced {synced} live RentVine leases into the database."
        })

    def _handle_post_settings(self, body: Dict[str, Any]):
        if "grace_period_days" in body:
            settings.GRACE_PERIOD_DAYS = int(body["grace_period_days"])
        if "deduplication_window_hours" in body:
            settings.DEDUPLICATION_WINDOW_HOURS = int(body["deduplication_window_hours"])
        if "promise_expiry_days" in body:
            settings.PROMISE_EXPIRY_DAYS = int(body["promise_expiry_days"])
        if "ai_model" in body:
            settings.AI_MODEL = str(body["ai_model"])
        self._send_json({"success": True, "message": "Settings updated"})

    def _serve_static_file(self, path: str):
        """Serves static files from the `web` folder."""
        if path == "/" or path == "":
            path = "/index.html"

        # Sanitize path to prevent directory traversal
        rel_path = path.lstrip("/").replace("/", os.sep)
        full_path = os.path.join(WEB_DIR, rel_path)

        if not os.path.exists(full_path) or os.path.isdir(full_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return

        mime_type, _ = mimetypes.guess_type(full_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        try:
            with open(full_path, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"500 Internal Server Error: {e}".encode("utf-8"))

def start_server(host: str = "127.0.0.1", port: int = 8000):
    server = ThreadingHTTPServer((host, port), RedBrickAPIHandler)
    print(f"================================================================")
    print(f" RED BRICKS PROPERTY MANAGEMENT - AI AUTOMATION CONTROL CENTER")
    print(f" Serving at: http://{host}:{port}/")
    print(f" RentVine MCP: {settings.RENTVINE_MCP_ENDPOINT}")
    print(f" Quo API:      {settings.QUO_API_URL}")
    print(f" AI Model:     {settings.AI_MODEL}")
    print(f"================================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    start_server()
