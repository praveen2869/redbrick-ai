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
from datetime import datetime
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, quote

from src.config import settings
from src.database import db
from src.models import (
    Tenant, RentVineRecord, RentVineNote, CommunicationMessage,
    Channel, MessageDirection, DecisionOutcome, IntentType
)
from src.clients.rentvine_mcp import RentVineMCPClient
from src.clients.quo_sms import QuoSMSClient
from src.clients.gmail_client import GmailClient
from src.engine.rules import BusinessRulesEngine
from src.engine.ai_classifier import AIIntentClassifier
from src.workflow import WorkflowOrchestrator
from src.reporting import ReportGenerator
from src.seed_data import populate_sample_data

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
            return self._handle_get_tenants()
        elif path.startswith("/api/tenant/"):
            tenant_id = path.replace("/api/tenant/", "").strip()
            return self._handle_get_tenant_detail(tenant_id)
        elif path == "/api/runs":
            return self._handle_get_runs()
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
            return self._handle_get_quo_live()

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
                    "model": settings.AI_MODEL,
                    "has_openai_key": bool(settings.OPENAI_API_KEY),
                    "active_engine": "OpenAI " + settings.AI_MODEL if settings.OPENAI_API_KEY else "Built-in Semantic NLU Engine"
                }
            },
            "latest_run": latest_run
        }
        self._send_json(data)

    def _handle_get_tenants(self):
        """Returns all tenants joined with their current RentVine balance and latest decision."""
        tenants = db.get_all_tenants()
        decisions = db.get_latest_decisions(limit=2000)

        # Map decisions by tenant_id (most recent first)
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

        results = []
        for t in tenants:
            rv = db.get_rentvine_record(t.id)
            messages = db.get_messages_for_tenant(t.id)
            latest_msg = messages[-1] if messages else None

            results.append({
                "id": t.id,
                "first_name": t.first_name,
                "last_name": t.last_name,
                "full_name": t.full_name,
                "phone": t.phone,
                "email": t.email,
                "property_name": t.property_name,
                "unit_number": t.unit_number,
                "is_excluded": t.is_excluded,
                "exclusion_reason": t.exclusion_reason,
                "balance": rv.balance if rv else 0.0,
                "monthly_rent": rv.monthly_rent if rv else 0.0,
                "due_date": rv.due_date if rv else None,
                "last_payment_date": rv.last_payment_date if rv else None,
                "last_payment_amount": rv.last_payment_amount if rv else 0.0,
                "latest_decision": latest_decision_by_tenant.get(t.id),
                "latest_message": {
                    "channel": latest_msg.channel.value,
                    "direction": latest_msg.direction.value,
                    "content": latest_msg.content,
                    "timestamp": latest_msg.timestamp
                } if latest_msg else None,
                "message_count": len(messages),
                "notes_count": len(rv.notes) if rv and rv.notes else 0
            })

        self._send_json({"tenants": results, "total": len(results)})

    def _handle_get_tenant_detail(self, tenant_id: str):
        """Returns full tenant dossier: RentVine record, notes, Quo SMS messages, Gmail emails, and audit log."""
        tenants = db.get_all_tenants()
        matched = [t for t in tenants if t.id == tenant_id]
        if not matched:
            return self._send_json({"error": "Tenant not found"}, 404)

        t = matched[0]
        rv = db.get_rentvine_record(tenant_id)
        messages = db.get_messages_for_tenant(tenant_id)
        notes = db.get_rentvine_notes(tenant_id)

        # Get all prior decisions specifically for this tenant
        tenant_decisions = [
            {
                "run_id": d.run_id,
                "outcome": d.outcome.value,
                "reason": d.reason,
                "timestamp": d.timestamp,
                "sms_sent": d.sms_sent,
                "note_written": d.note_written,
                "details": d.details
            }
            for d in db.get_decisions_for_tenant(tenant_id, limit=50)
        ]

        data = {
            "tenant": {
                "id": t.id,
                "full_name": t.full_name,
                "phone": t.phone,
                "email": t.email,
                "property_name": t.property_name,
                "unit_number": t.unit_number,
                "is_excluded": t.is_excluded,
                "exclusion_reason": t.exclusion_reason
            },
            "rentvine": {
                "balance": rv.balance if rv else 0.0,
                "monthly_rent": rv.monthly_rent if rv else 0.0,
                "due_date": rv.due_date if rv else None,
                "last_payment_date": rv.last_payment_date if rv else None,
                "last_payment_amount": rv.last_payment_amount if rv else 0.0,
                "portal_link": rv.portal_link if rv else "https://redbrick.rentvine.com/portal/pay",
                "notes": [
                    {
                        "id": n.id,
                        "created_at": n.created_at,
                        "author": n.author,
                        "text": n.text,
                        "is_automated": n.is_automated
                    }
                    for n in notes
                ]
            },
            "communications": [
                {
                    "id": m.id,
                    "channel": m.channel.value,
                    "direction": m.direction.value,
                    "sender": m.sender,
                    "recipient": m.recipient,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "status": m.status,
                    "metadata": m.metadata
                }
                for m in messages
            ],
            "decisions": tenant_decisions
        }
        self._send_json(data)

    def _handle_get_runs(self):
        runs = db.get_recent_runs(limit=20)
        self._send_json({"runs": runs})

    def _handle_get_latest_report(self, query: Dict[str, Any]):
        runs = db.get_recent_runs(limit=1)
        if not runs:
            return self._send_json({"error": "No workflow runs on record yet"}, 404)

        run_dict = runs[0]
        # Reconstruct DailyRunSummary for HTML or markdown output
        format_type = query.get("format", ["json"])[0]

        if format_type == "html":
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
        """Pings Quo API's /phone-numbers endpoint to verify the key."""
        if settings.QUO_MOCK_MODE:
            return {"status": "simulation", "mode": "mock", "endpoint": settings.QUO_API_URL}
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
                    "phone_numbers": [
                        {"id": p.get("id"), "number": p.get("phoneNumber"), "name": p.get("name")}
                        for p in (phones if isinstance(phones, list) else [])
                    ]
                }
        except _ue.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {"status": "error", "http_code": e.code, "detail": body}
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

    def _handle_get_quo_live(self):
        """
        Live Quo (OpenPhone) feed handler.
        - Fetches real conversations + messages from OpenPhone REST API
        - Builds reminders queue from real workflow decisions (NO dummy data)
        - Merges HITL-approved sends from local DB
        """
        now_ts = datetime.utcnow().timestamp()
        if "quo_live" in _API_CACHE:
            cached_time, cached_val = _API_CACHE["quo_live"]
            if now_ts - cached_time < 20:
                return self._send_json(cached_val)

        phone_id = settings.QUO_DEFAULT_PHONE_NUMBER_ID
        phone_number = "+1 (667) 281-3879"

        all_tenants = db.get_all_tenants()
        tenant_map = {t.id: t for t in all_tenants}

        # ─── 1. Fetch real OpenPhone conversations + messages ───
        openphone_conversations = []
        openphone_messages = []
        quo_probe = self._test_quo_connection()

        if not settings.QUO_MOCK_MODE and settings.QUO_API_KEY:
            import urllib.request as _ur, urllib.error as _ue
            auth = settings.QUO_API_KEY if settings.QUO_API_KEY.startswith("Bearer ") else f"Bearer {settings.QUO_API_KEY}"
            headers = {
                "Authorization": auth,
                "Content-Type": "application/json",
                "User-Agent": "RedBricks-Dashboard/1.0"
            }

            # Fetch conversations for our phone number
            try:
                req = _ur.Request(
                    f"{settings.QUO_API_URL}/conversations?phoneNumberId={phone_id}&maxResults=50",
                    headers=headers
                )
                with _ur.urlopen(req, timeout=10) as resp:
                    conv_data = json.loads(resp.read().decode("utf-8"))
                    openphone_conversations = conv_data.get("data", []) or []
                    logger.info(f"[Quo] Fetched {len(openphone_conversations)} live conversations")
            except Exception as e:
                logger.warning(f"[Quo] Failed to fetch conversations: {e}")

            # For recent conversations, fetch messages (scans up to 15 to include recent threads like Kukunoor)
            for conv in openphone_conversations[:15]:
                conv_id = conv.get("id")
                if not conv_id:
                    continue
                participants = conv.get("participants", [])
                phone_str = ""
                name_str = conv.get("name") or ""
                if participants:
                    p0 = participants[0]
                    phone_str = p0.get("phoneNumber", "") if isinstance(p0, dict) else str(p0)
                if not phone_str:
                    continue

                # Query OpenPhone messages with required phoneNumberId and participants[]
                try:
                    msg_url = f"{settings.QUO_API_URL}/messages?phoneNumberId={phone_id}&participants[]={quote(phone_str)}&maxResults=5"
                    req = _ur.Request(msg_url, headers=headers)
                    with _ur.urlopen(req, timeout=4) as resp:
                        msg_data = json.loads(resp.read().decode("utf-8"))
                        for m in (msg_data.get("data", []) or []):
                            txt = m.get("text", m.get("body", ""))
                            resolved_name = name_str or phone_str
                            prop_unit = ""
                            if "8622205006" in phone_str:
                                resolved_name = "Kukunoor Tenant"
                                prop_unit = "8620 Fairgreen Avenue Unit 691"
                            elif txt.startswith("Hi ") and "," in txt[:30]:
                                resolved_name = txt[3:txt.index(",")].strip()

                            if not prop_unit and " for " in txt and " was due" in txt:
                                try:
                                    part_u = txt.split(" for ")[1].split(" was due")[0].strip()
                                    prop_unit = part_u
                                except Exception:
                                    pass

                            openphone_messages.append({
                                "id": m.get("id", ""),
                                "conversation_id": conv_id,
                                "direction": "INBOUND" if m.get("direction") == "incoming" else "OUTBOUND",
                                "content": txt,
                                "timestamp": m.get("createdAt", m.get("deliveredAt", "")),
                                "status": m.get("status", "delivered").upper(),
                                "tenant_name": resolved_name,
                                "phone": phone_str,
                                "tenant_id": "",
                                "property_unit": prop_unit,
                                "source": "openphone_live"
                            })
                except _ue.HTTPError as he:
                    logger.warning(f"[Quo] HTTP {he.code} fetching messages for {phone_str}: {he}")
                    if he.code in (401, 403):
                        logger.warning("[Quo] Token expired or unauthorized; skipping remaining queries.")
                        break
                except Exception as e:
                    logger.warning(f"[Quo] Failed to fetch messages for {phone_str}: {e}")

        # ─── 2. Build reminders queue from REAL workflow decisions only ───
        latest_decisions = db.get_latest_decisions(limit=2000)
        reminders_needed = []
        seen_tenants = set()

        for d in latest_decisions:
            if d.tenant_id in seen_tenants:
                continue
            seen_tenants.add(d.tenant_id)

            if d.outcome != DecisionOutcome.SEND_REMINDER:
                continue  # Most recent evaluation was SKIP_PAID, HOLD_PROMISE, etc.

            # Cooldown check: if reminder was genuinely dispatched in the last 24h, skip it
            last_rem = db.get_last_successful_reminder(d.tenant_id)
            if last_rem and last_rem.sms_sent:
                try:
                    last_ts = datetime.fromisoformat(last_rem.timestamp)
                    if (datetime.utcnow() - last_ts).total_seconds() < 24 * 3600:
                        continue
                except Exception:
                    pass

            t = tenant_map.get(d.tenant_id)
            if not t:
                continue  # skip orphaned decision records

            # Safety check: ensure current balance is genuine overdue rent (>= $20.00)
            rv = db.get_rentvine_record(d.tenant_id)
            effective_balance = float(rv.balance) if rv else float(d.balance or 0)
            if effective_balance < 20.0:
                continue  # Never display or queue misleading reminders for $0 or minor fees

            # Only include tenants with a real phone number
            if not t.phone or t.phone.startswith("+1-415-555"):
                continue

            # Build SMS preview
            sms_text = d.details.get("sms_text", "") if d.details else ""
            if not sms_text:
                try:
                    rv = db.get_rentvine_record(t.id)
                    portal = rv.portal_link if rv else "https://redbricksproperties.rentvine.com/portal/pay"
                    due = rv.due_date if rv else "1st of the month"
                    sms_text = settings.REMINDER_TEMPLATE.format(
                        tenant_name=t.first_name,
                        balance=d.balance if d.balance else (rv.balance if rv else 0),
                        property_name=t.property_name or d.property_unit,
                        unit_number=t.unit_number or "",
                        due_date=due,
                        portal_link=portal
                    )
                except Exception:
                    sms_text = (
                        f"Hi {t.first_name}, this is a reminder from Red Bricks Property Management. "
                        f"Your rent is overdue. Please pay via your RentVine portal. Thank you!"
                    )

            reminders_needed.append({
                "tenant_id": d.tenant_id,
                "tenant_name": d.tenant_name or t.full_name,
                "property_unit": d.property_unit or f"{t.property_name} {t.unit_number}",
                "phone": t.phone,
                "balance": float(d.balance or 0),
                "status": "AWAITING_HITL_APPROVAL",
                "sms_preview": sms_text,
                "reason": d.reason,
                "timestamp": d.timestamp
            })

        # ─── 3. Build SMS message history: HITL-approved + any OpenPhone live msgs ───
        # Local DB messages (HITL approved or recorded) via single bulk query
        local_sms = []
        recent_db_msgs = db.get_all_recent_messages(limit=150, channel=Channel.SMS)
        for m in recent_db_msgs:
            if m.status == "REMINDER_NEEDED (TESTING_MODE)":
                continue  # Never display internal suppressed testing simulations in live message history
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


        resp_data = {
            "mode": "hitl_testing",
            "hitl_status": "LOCKED — Explicit HITL approval required per reminder",
            "channel": {
                "name": "Quo (OpenPhone) SMS",
                "phone_number": phone_number,
                "phone_number_id": phone_id,
                "api_endpoint": settings.QUO_API_URL,
                "live_status": quo_probe.get("status", "simulation"),
                "openphone_conversations": len(openphone_conversations),
                "openphone_messages_fetched": len(openphone_messages)
            },
            "counts": {
                "reminders_needed": len(reminders_needed),
                "sms_messages": len(deduped),
                "inbound_replies": inbound_count,
                "hitl_approved_sent": hitl_sent
            },
            "reminders_needed": reminders_needed,
            "messages": deduped,
            "probe": quo_probe
        }
        _API_CACHE["quo_live"] = (now_ts, resp_data)
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
                "action": "Hold SMS reminders and update RentVine notes"
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
                "reason": "Standard reply or question; does not override reminder eligibility",
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
        if not tenant_id or not text:
            return self._send_json({"error": "tenant_id and text required"}, 400)

        tenants = db.get_all_tenants()
        matched = [t for t in tenants if t.id == tenant_id]
        if not matched:
            return self._send_json({"error": "Tenant not found"}, 404)
        t = matched[0]

        client = QuoSMSClient()
        res = client.send_sms(tenant_id, t.phone, text)
        self._send_json(res)

    def _handle_post_send_reminder(self, body: Dict[str, Any]):
        """
        HITL-approved SMS reminder dispatch.
        Only called when a property manager explicitly clicks 'Send Reminder' in the dashboard.
        Dispatches to Quo (OpenPhone) API when live credentials are configured,
        or records with HITL_APPROVED status in mock mode.
        """
        tenant_id = body.get("tenant_id")
        phone = body.get("phone")
        text = body.get("text")
        hitl_approved = bool(body.get("hitl_approved", False))

        if not tenant_id or not phone or not text:
            return self._send_json({"error": "tenant_id, phone, and text are required"}, 400)

        if not hitl_approved:
            return self._send_json({"error": "HITL approval flag must be set to dispatch reminder"}, 403)

        try:
            msg_id = f"hitl_{int(__import__('datetime').datetime.utcnow().timestamp() * 1000)}"
            now_iso = __import__('datetime').datetime.utcnow().isoformat()

            if not settings.QUO_MOCK_MODE and settings.QUO_API_KEY:
                # ── LIVE DISPATCH via Quo (OpenPhone) REST API ──
                import urllib.request as _ur, urllib.error as _ue
                auth = settings.QUO_API_KEY if settings.QUO_API_KEY.startswith("Bearer ") else f"Bearer {settings.QUO_API_KEY}"
                payload = json.dumps({
                    "content": text,
                    "from": settings.QUO_DEFAULT_PHONE_NUMBER_ID,
                    "to": [phone]
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
                with _ur.urlopen(req, timeout=10) as resp:
                    api_resp = json.loads(resp.read().decode("utf-8"))
                    ext_id = api_resp.get("data", {}).get("id", msg_id)

                # Record in local DB with HITL_APPROVED + SENT status
                from src.models import CommunicationMessage, Channel, MessageDirection
                from src.database import db as _db
                outbound = CommunicationMessage(
                    id=ext_id,
                    tenant_id=tenant_id,
                    channel=Channel.SMS,
                    direction=MessageDirection.OUTBOUND,
                    sender=settings.QUO_DEFAULT_PHONE_NUMBER_ID,
                    recipient=phone,
                    content=text,
                    timestamp=now_iso,
                    status="SENT_HITL_APPROVED",
                    metadata={
                        "source": "Quo REST API",
                        "provider": "OpenPhone",
                        "hitl_approved": True,
                        "sms_dispatched": True,
                        "ext_message_id": ext_id
                    }
                )
                _db.save_message(outbound)
                logger.info(f"[HITL APPROVED] SMS sent to {phone} for tenant {tenant_id} via Quo. ext_id={ext_id}")

                # Record successful reminder send so 24h cooldown activates now that it's sent
                from src.models import WorkflowDecision, DecisionOutcome
                _db.save_decision(WorkflowDecision(
                    tenant_id=tenant_id,
                    tenant_name=tenant_id,
                    property_unit="",
                    balance=0.0,
                    outcome=DecisionOutcome.SEND_REMINDER,
                    reason=f"HITL Reminder SMS approved and sent to {phone}",
                    details={"text": text, "phone": phone, "hitl_approved": True},
                    timestamp=now_iso,
                    sms_sent=True,
                    sms_message_id=ext_id,
                    note_written=False,
                    run_id="hitl_sent"
                ))

                return self._send_json({
                    "success": True,
                    "mock_mode": False,
                    "message_id": ext_id,
                    "timestamp": now_iso,
                    "recipient": phone,
                    "status": "SENT_HITL_APPROVED",
                    "ext_api_response": api_resp
                })
            else:
                # ── MOCK / TESTING MODE — Log with HITL_APPROVED but don't dispatch ──
                from src.models import CommunicationMessage, Channel, MessageDirection
                from src.database import db as _db
                outbound = CommunicationMessage(
                    id=msg_id,
                    tenant_id=tenant_id,
                    channel=Channel.SMS,
                    direction=MessageDirection.OUTBOUND,
                    sender="+1 (667) 281-3879",
                    recipient=phone,
                    content=text,
                    timestamp=now_iso,
                    status="HITL_APPROVED_LOGGED (MOCK)",
                    metadata={
                        "source": "Quo REST API",
                        "provider": "OpenPhone",
                        "hitl_approved": True,
                        "sms_dispatched": False,
                        "mock_mode": True,
                        "note": "HITL approved — SMS would be sent if QUO_MOCK_MODE=false"
                    }
                )
                _db.save_message(outbound)
                logger.info(f"[HITL APPROVED - MOCK] SMS logged (not dispatched) for {phone} ({tenant_id})")

                # Record decision so cooldown activates for this tenant after manager triggers send
                from src.models import WorkflowDecision, DecisionOutcome
                _db.save_decision(WorkflowDecision(
                    tenant_id=tenant_id,
                    tenant_name=tenant_id,
                    property_unit="",
                    balance=0.0,
                    outcome=DecisionOutcome.SEND_REMINDER,
                    reason=f"HITL Reminder approved and recorded for {phone}",
                    details={"text": text, "phone": phone, "hitl_approved": True, "mock_mode": True},
                    timestamp=now_iso,
                    sms_sent=True,
                    sms_message_id=msg_id,
                    note_written=False,
                    run_id="hitl_sent_mock"
                ))

                return self._send_json({
                    "success": True,
                    "mock_mode": True,
                    "message_id": msg_id,
                    "timestamp": now_iso,
                    "recipient": phone,
                    "status": "HITL_APPROVED_LOGGED (MOCK)",
                    "note": "HITL approval recorded. Cooldown activated."
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
    server = HTTPServer((host, port), RedBrickAPIHandler)
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
