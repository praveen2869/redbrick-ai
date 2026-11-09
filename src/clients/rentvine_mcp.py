import re
from concurrent.futures import ThreadPoolExecutor, as_completed
"""
Red Bricks Property Management - RentVine Client
Supports two integration paths:

  1. REST API (programmatic, preferred):
     Basic auth with Access Key + Secret from Settings > Users, Roles and API.
     Endpoint: https://{your-subdomain}.rentvine.com/api/manager/...

  2. MCP endpoint (for Claude/ChatGPT OAuth browser flows):
     https://mcp.rentvine.ai/mcp — requires browser OAuth, not supported in headless Python.
     Falls back to REST API if credentials are available, otherwise uses local DB.

Authentication priority:
  Bearer token (DevTools capture) > Basic auth (API Key + Secret) > Local DB fallback
"""

import os
import json
import base64
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from src.config import settings
from src.models import Tenant, RentVineRecord, RentVineNote
from src.database import db

logger = logging.getLogger("RentVineMCP")

def clean_property_address(prop_name: Optional[str]) -> str:
    """
    Cleans internal RentVine portfolio and community prefixes.
    e.g. 'NONE_5100 @ Central_4124 Salient Street' -> '4124 Salient Street'
         'Woodcroft(Davis Lake)_4709 Morning Dew Ct' -> '4709 Morning Dew Ct'
    """
    if not prop_name:
        return ""
    p = str(prop_name).strip()
    if "_" in p:
        candidate = p.split("_")[-1].strip()
        if any(c.isdigit() for c in candidate):
            return candidate
    return p

def format_friendly_due_date(val: Optional[str]) -> str:
    """
    Formats dates into friendly human-readable format.
    e.g. '2026-11-01' -> 'November 1, 2026'
         '2026-10-09' -> 'October 9, 2026'
         'November 1, 2026' -> 'November 1, 2026'
    """
    if not val:
        return ""
    val_str = str(val).strip()
    months_pat = r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\b'
    if re.search(months_pat, val_str, re.IGNORECASE):
        return val_str
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', val_str)
    if m:
        try:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime(year, month, day)
            return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
        except Exception:
            pass
    return val_str

def parse_billing_period_and_due(desc: str, date_posted: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts the billing period name and due date from RentVine charge descriptions and datePosted.
    e.g. 'Rent Income (11-2026)', '2026-11-01' -> ('November 2026', '2026-11-01')
    """
    billing_period = None
    if desc:
        m = re.search(r'\((\d{1,2})[-/](\d{4})\)', desc)
        if m:
            month, year = int(m.group(1)), int(m.group(2))
            try:
                billing_period = datetime(year, month, 1).strftime("%B %Y")
            except Exception:
                pass
        else:
            m2 = re.search(r'\((\d{4})[-/](\d{1,2})\)', desc)
            if m2:
                year, month = int(m2.group(1)), int(m2.group(2))
                try:
                    billing_period = datetime(year, month, 1).strftime("%B %Y")
                except Exception:
                    pass

    if not billing_period and date_posted:
        try:
            dt = datetime.strptime(date_posted[:10], "%Y-%m-%d")
            billing_period = dt.strftime("%B %Y")
        except Exception:
            pass

    due_date = date_posted[:10] if date_posted else None
    return billing_period, due_date



class RentVineMCPClient:
    """
    Client for RentVine financial data.
    Uses REST API with Basic auth (Access Key + Secret) or Bearer token.
    Gracefully falls back to local SQLite store on any connectivity issue.
    """

    def __init__(self, endpoint: Optional[str] = None, api_key: Optional[str] = None, mock_mode: Optional[bool] = None):
        self.mcp_endpoint = endpoint or settings.RENTVINE_MCP_ENDPOINT
        self.mock_mode = settings.RENTVINE_MOCK_MODE if mock_mode is None else mock_mode
        self._request_id = 0
        self._bearer_token: str = settings.RENTVINE_BEARER_TOKEN or ""
        self._api_key: str = api_key or settings.RENTVINE_API_KEY
        self._api_secret: str = settings.RENTVINE_API_SECRET or ""
        self._api_base: str = (settings.RENTVINE_API_BASE_URL or "").rstrip("/")

    # ─── Auth Helpers ────────────────────────────────────────────────────────

    def _bearer_header(self) -> Dict[str, str]:
        """Returns Authorization header using bearer token."""
        return {"Authorization": f"Bearer {self._bearer_token}"}

    def _basic_auth_header(self) -> Dict[str, str]:
        """Returns Authorization header using Basic auth (API Key + Secret)."""
        creds = base64.b64encode(f"{self._api_key}:{self._api_secret}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    def _auth_headers(self) -> Dict[str, str]:
        """Returns the best available auth header."""
        if self._bearer_token:
            return self._bearer_header()
        if self._api_secret:
            return self._basic_auth_header()
        return {}

    # ─── REST API Calls ──────────────────────────────────────────────────────

    def _rest_get(self, path: str) -> Dict[str, Any]:
        """Makes an authenticated GET request to the RentVine REST API."""
        data, _ = self._rest_get_with_headers(path)
        return data

    def _rest_get_with_headers(self, path: str):
        """Makes an authenticated GET request to the RentVine REST API and returns (json_data, headers_dict)."""
        if not self._api_base:
            raise RuntimeError("RENTVINE_API_BASE_URL not configured.")
        url = f"{self._api_base}/api/manager/{path.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RedBrick-Python-Agent/1.0",
            **self._auth_headers()
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                hdrs = dict(resp.getheaders())
                return data, hdrs
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.warning(f"RentVine REST API HTTP {e.code} on {url}: {body}")
            raise
        except urllib.error.URLError as e:
            logger.warning(f"RentVine REST API unreachable ({url}): {e}")
            raise

    def sync_portfolio_from_rentvine(self, limit: int = 1000) -> int:
        """
        Synchronizes live RentVine active leases and tenant records into the local SQLite database.
        Paginates through leases and dynamically resolves tenant contact details.
        """
        if self.mock_mode or not self._api_base:
            return 0

        leases_data = []
        page = 1
        page_size = 50
        while True:
            try:
                batch, hdrs = self._rest_get_with_headers(f"leases?page={page}&pageSize={page_size}")
                if not isinstance(batch, list) or len(batch) == 0:
                    break
                leases_data.extend(batch)
                total_pages = int(hdrs.get("Pagination-Total-Pages", 1))
                if page >= total_pages or len(leases_data) >= limit:
                    break
                page += 1
            except Exception as e:
                logger.error(f"Error fetching leases page {page} from RentVine: {e}")
                break

        # Contact cache for phone/email resolution (prefetch across pages)
        contact_map: Dict[str, tuple] = {}
        try:
            t_page = 1
            while True:
                batch_tenants, hdrs_t = self._rest_get_with_headers(f"tenants?page={t_page}&pageSize=100")
                if not isinstance(batch_tenants, list) or len(batch_tenants) == 0:
                    break
                for item in batch_tenants:
                    co = item.get("contact", {})
                    c_name = (co.get("name") or "").strip().lower()
                    if c_name:
                        contact_map[c_name] = (co.get("phone"), co.get("email"))
                t_total_pages = int(hdrs_t.get("Pagination-Total-Pages", 1))
                if t_page >= t_total_pages or t_page >= 10:
                    break
                t_page += 1
        except Exception as e:
            logger.warning(f"Batch tenant contact pre-fetch failed: {e}")

        synced_count = 0
        import urllib.parse
        for item in leases_data:
            l = item.get("lease", {})
            u = item.get("unit", {})
            lid = l.get("leaseID")
            tenants = l.get("tenants", [])
            if not lid or not tenants:
                continue

            primary_name = tenants[0]
            name_parts = primary_name.split(" ", 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            prop_name = u.get("name") or u.get("address") or "Red Bricks Property"
            unit_num = f"Unit {u.get('unitID', lid)}"
            rent_val = float(l.get("rentAmount") or l.get("baseRentAmount") or u.get("rent") or 0.0)

            # If rent_val is 0, fetch individual lease details
            if rent_val == 0.0:
                try:
                    l_detail, _ = self._rest_get_with_headers(f"leases/{lid}")
                    if isinstance(l_detail, dict):
                        ld = l_detail.get("lease", {})
                        rent_val = float(ld.get("rentAmount") or ld.get("baseRentAmount") or 0.0)
                except Exception:
                    pass

            # Query cached contact map first; fallback to exact tenant search API
            lookup_key = primary_name.strip().lower()
            cached_contact = contact_map.get(lookup_key)
            if not cached_contact or not cached_contact[0]:
                try:
                    encoded_name = urllib.parse.quote(primary_name)
                    t_search, _ = self._rest_get_with_headers(f"tenants?search={encoded_name}")
                    if isinstance(t_search, list) and len(t_search) > 0:
                        co = t_search[0].get("contact", {})
                        contact_map[lookup_key] = (co.get("phone"), co.get("email"))
                        cached_contact = contact_map[lookup_key]
                except Exception:
                    pass

            if cached_contact and cached_contact[0]:
                real_phone = cached_contact[0]
                real_email = cached_contact[1] or f"{first_name.lower()}@rentvine.demo"
            else:
                real_phone = ""
                real_email = f"{first_name.lower()}@rentvine.demo"

            tenant = Tenant(
                id=f"RV-{lid}",
                first_name=first_name,
                last_name=last_name,
                phone=real_phone,
                email=real_email,
                property_name=prop_name,
                unit_number=unit_num
            )
            db.upsert_tenant(tenant)

            # Calculate true unpaid balance & current-month due date via RentVine accounting transactions
            actual_balance, actual_due = self.get_lease_actual_balance(lid, rent_val)

            rv_record = RentVineRecord(
                tenant_id=f"RV-{lid}",
                balance=actual_balance,
                monthly_rent=rent_val,
                due_date=actual_due,
                portal_link=settings.RENTVINE_PORTAL_PAYMENT_URL,
                lease_status="ACTIVE" if l.get("primaryLeaseStatusID") == "2" else "COMPLETED"
            )
            db.upsert_rentvine_record(rv_record)
            synced_count += 1

        logger.info(f"Synchronized {synced_count} live RentVine leases into local database.")
        return synced_count

    def extract_unpaid_rent_details(self, transactions: List[Dict[str, Any]], total_balance: float = 0.0) -> Dict[str, Any]:
        """
        Analyzes RentVine accounting transactions to identify unpaid rent charges,
        their respective billing periods, and lease-specific due dates.
        Distinguishes single-month vs. multi-month dues and formats human-readable dates.
        """
        unpaid_rent_charges = []
        seen_periods = set()

        for item in transactions:
            t = item.get("transaction", {}) if isinstance(item, dict) else {}
            if str(t.get("isVoided", "0")) == "1" or str(t.get("isSuppressed", "0")) == "1":
                continue

            ttype = str(t.get("transactionTypeID", ""))
            desc = (t.get("description") or "").strip()
            desc_lower = desc.lower()
            date_posted = str(t.get("datePosted") or "")[:10]

            amt = float(t.get("amount") or 0.0)
            paid = float(t.get("amountPaid") or 0.0) if t.get("amountPaid") is not None else 0.0
            remaining = round(amt - paid, 2)

            # Genuine rent charges (excluding insurance, utilities, convenience/reminder fees)
            is_rent = ("rent" in desc_lower or "prorated" in desc_lower) and ("insurance" not in desc_lower)

            if ttype == "1" and is_rent and remaining > 0:
                period, due_date = parse_billing_period_and_due(desc, date_posted)
                key = (period, due_date)
                if key not in seen_periods:
                    seen_periods.add(key)
                    friendly = format_friendly_due_date(due_date) if due_date else ""
                    unpaid_rent_charges.append({
                        "description": desc,
                        "amount": amt,
                        "unpaid": remaining,
                        "date_posted": due_date,
                        "billing_period": period or (due_date[:7] if due_date else ""),
                        "friendly_due": friendly
                    })

        # Sort chronological by date_posted
        unpaid_rent_charges.sort(key=lambda x: x["date_posted"] or "")

        if len(unpaid_rent_charges) == 1:
            c = unpaid_rent_charges[0]
            due_date_str = c["friendly_due"]
            raw_due_date = c["date_posted"]
            billing_period_str = c["billing_period"]
            billing_periods = [c["billing_period"]]
        elif len(unpaid_rent_charges) > 1:
            # Multi-month outstanding rent: identify and distinguish each billing period
            parts = [
                f"{c['friendly_due']} ({c['billing_period']})" if c['billing_period'] else c['friendly_due']
                for c in unpaid_rent_charges
            ]
            if len(parts) == 2:
                due_date_str = f"{parts[0]} and {parts[1]}"
            else:
                due_date_str = ", ".join(parts[:-1]) + f", and {parts[-1]}"
            raw_due_date = unpaid_rent_charges[0]["date_posted"]
            billing_period_str = " & ".join(c["billing_period"] for c in unpaid_rent_charges if c["billing_period"])
            billing_periods = [c["billing_period"] for c in unpaid_rent_charges if c["billing_period"]]
        else:
            now = datetime.utcnow()
            raw_due_date = now.strftime("%Y-%m-01")
            due_date_str = format_friendly_due_date(raw_due_date)
            billing_period_str = now.strftime("%B %Y")
            billing_periods = [billing_period_str]

        return {
            "due_date": due_date_str,
            "raw_due_date": raw_due_date,
            "billing_period": billing_period_str,
            "billing_periods": billing_periods,
            "unpaid_rent_charges": unpaid_rent_charges,
            "unpaid_rent_total": sum(c["unpaid"] for c in unpaid_rent_charges)
        }

    def get_lease_actual_balance(self, lease_id: str, default_rent: float) -> tuple:
        """
        Pulls actual accounting transactions from RentVine to determine whether the tenant
        has already paid (balance = 0.0) or has an outstanding overdue balance.
        Identifies the exact billing period and lease-specific due date.
        """
        now = datetime.now(timezone.utc)
        current_month_due = format_friendly_due_date(now.strftime("%Y-%m-01"))
        if self.mock_mode or not self._api_base:
            return default_rent, current_month_due

        try:
            txs, _ = self._rest_get_with_headers(f"accounting/transactions?leaseID={lease_id}&pageSize=25")
            if not isinstance(txs, list) or len(txs) == 0:
                return default_rent, current_month_due

            details = self.extract_unpaid_rent_details(txs, total_balance=default_rent)
            unpaid = details["unpaid_rent_total"] if details["unpaid_rent_charges"] else (default_rent if default_rent > 0 else 0.0)
            return round(unpaid, 2), details["due_date"]
        except Exception as e:
            logger.warning(f"Failed to check accounting transactions for lease {lease_id}: {e}")
            return default_rent, current_month_due


    # ─── MCP JSON-RPC (Bearer only) ──────────────────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _call_mcp_rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Sends a JSON-RPC 2.0 payload to the RentVine MCP endpoint (requires Bearer token)."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RedBrick-Python-Agent/1.0",
            **self._auth_headers()
        }
        if not self._bearer_token:
            raise RuntimeError(
                "RentVine MCP requires a Bearer token. "
                "Set RENTVINE_BEARER_TOKEN in .env (capture from DevTools while logged in to rentvine.com)."
            )
        req = urllib.request.Request(
            self.mcp_endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "error" in data:
                    raise RuntimeError(f"RentVine MCP Error: {data['error']}")
                return data.get("result", {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.warning(f"RentVine MCP HTTP {e.code}: {body}")
            raise
        except urllib.error.URLError as e:
            logger.warning(f"RentVine MCP unreachable: {e}")
            raise

    # ─── Public Methods ──────────────────────────────────────────────────────

    def get_cached_or_api_contact(self, name: str) -> Dict[str, str]:
        """
        Retrieves phone and email for a tenant by name.
        Uses local scratch/tenant_contacts_cache.json first, falls back to RentVine search API when live.
        """
        if not name:
            return {"phone": "", "email": ""}
        clean_name = name.strip().lower()

        # Known fallback dictionary for test/demo accounts
        static_fallbacks = {
            "second kukunoor tenant": {"phone": "+19802941035", "email": ""},
            "kukunoor tenant": {"phone": "+18622205006", "email": ""},
            "stephany ikolo": {"phone": "+17046743831", "email": "fannyikolo65@gmail.com"}
        }
        if clean_name in static_fallbacks:
            return static_fallbacks[clean_name]

        # Check local JSON cache file
        cache_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scratch", "tenant_contacts_cache.json"
        )
        cached = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if clean_name in cached:
                    return cached[clean_name]
            except Exception:
                pass

        # If live mode, search RentVine REST API
        if not self.mock_mode and self._api_base:
            try:
                import urllib.parse
                encoded = urllib.parse.quote(name.strip())
                t_search, _ = self._rest_get_with_headers(f"tenants?search={encoded}")
                if isinstance(t_search, list) and len(t_search) > 0:
                    for item in t_search:
                        co = item.get("contact", {})
                        c_name = (co.get("name") or "").strip().lower()
                        if c_name == clean_name or clean_name in c_name:
                            info = {
                                "phone": co.get("phone") or "",
                                "email": co.get("email") or "",
                                "contact_id": str(co.get("contactID") or "")
                            }
                            cached[clean_name] = info
                            try:
                                with open(cache_path, "w", encoding="utf-8") as f:
                                    json.dump(cached, f, indent=2)
                            except Exception:
                                pass
                            return info
            except Exception as e:
                logger.debug(f"RentVine search for co-tenant {name} failed: {e}")

        return {"phone": "", "email": ""}

    def get_tenant_record(self, tenant_id: str) -> Optional[RentVineRecord]:
        """Pulls tenant rent balance, due date, and payment status."""
        cached = db.get_rentvine_record(tenant_id)
        if cached:
            return cached

        if not self.mock_mode:
            # Try MCP (requires Bearer token) if available
            if self._bearer_token:
                try:
                    result = self._call_mcp_rpc("tools/call", {
                        "name": "get_tenant_financial_summary",
                        "arguments": {"tenant_id": tenant_id}
                    })
                    notes_raw = result.get("notes", [])
                    notes = [
                        RentVineNote(
                            id=n.get("id", f"note_{datetime.utcnow().timestamp()}"),
                            created_at=n.get("created_at", datetime.utcnow().isoformat()),
                            author=n.get("author", "RentVine System"),
                            text=n.get("text", ""),
                            is_automated=n.get("is_automated", True)
                        )
                        for n in notes_raw
                    ]
                    record = RentVineRecord(
                        tenant_id=tenant_id,
                        balance=float(result.get("balance", 0.0)),
                        monthly_rent=float(result.get("monthly_rent", 0.0)),
                        due_date=result.get("due_date", "2026-10-01"),
                        last_payment_date=result.get("last_payment_date"),
                        last_payment_amount=float(result.get("last_payment_amount", 0.0)),
                        notes=notes,
                        portal_link=result.get("portal_link", "https://redbrick.rentvine.com/portal/pay"),
                        lease_status=result.get("lease_status", "ACTIVE")
                    )
                    db.upsert_rentvine_record(record)
                    return record
                except Exception as e:
                    logger.info(f"RentVine MCP unavailable for {tenant_id}: {e}. Falling back to local DB.")

        return db.get_rentvine_record(tenant_id)

    def write_tenant_note(self, tenant_id: str, note_text: str, author: str = "AI Rent Reminder Agent") -> RentVineNote:
        """Writes a note to the tenant profile (and caches locally)."""
        note_id = f"rv_note_{int(datetime.utcnow().timestamp() * 1000)}"
        note = RentVineNote(
            id=note_id,
            created_at=datetime.utcnow().isoformat(),
            author=author,
            text=note_text,
            is_automated=True
        )
        if not self.mock_mode and self._bearer_token:
            try:
                self._call_mcp_rpc("tools/call", {
                    "name": "create_tenant_note",
                    "arguments": {"tenant_id": tenant_id, "text": note_text, "author": author}
                })
            except Exception as e:
                logger.warning(f"RentVine MCP write_note failed ({e}). Saving locally.")

        db.add_rentvine_note(tenant_id, note)
        return note

    def connection_test(self) -> Dict[str, Any]:
        """Tests RentVine connectivity. Returns status and auth method used."""
        if self.mock_mode:
            return {"status": "simulation", "mode": "mock", "endpoint": self.mcp_endpoint}

        result: Dict[str, Any] = {
            "endpoint": self.mcp_endpoint,
            "api_base": self._api_base or "(not configured)",
            "auth_method": "bearer_token" if self._bearer_token else ("basic_auth" if self._api_secret else "none")
        }

        # Test REST API
        if self._api_base and (self._api_secret or self._bearer_token):
            try:
                data = self._rest_get("properties?limit=1")
                result["status"] = "connected"
                result["integration"] = "REST API (Basic auth)"
                result["properties_sample"] = data
                return result
            except Exception as e:
                result["rest_api_error"] = str(e)

        # Test MCP (Bearer only)
        if self._bearer_token:
            try:
                tools = self._call_mcp_rpc("tools/list", {})
                result["status"] = "connected"
                result["integration"] = "MCP (Bearer token)"
                result["tools_available"] = [t.get("name") for t in tools.get("tools", [])]
                return result
            except Exception as e:
                result["mcp_error"] = str(e)

        result["status"] = "error"
        result["message"] = (
            "No valid credentials. Add RENTVINE_API_KEY + RENTVINE_API_SECRET + RENTVINE_API_BASE_URL "
            "to .env, OR set RENTVINE_BEARER_TOKEN (captured from DevTools while logged into rentvine.com)."
        )
        return result

    def list_all_tenant_balances(self) -> Dict[str, RentVineRecord]:
        """Pulls the entire active rent roll from RentVine."""
        records = {}
        tenants = db.get_all_tenants()
        for t in tenants:
            rec = self.get_tenant_record(t.id)
            if rec:
                records[t.id] = rec
        return records

    def fetch_lease_balance_report(self, report_id: int = 72) -> Dict[str, Any]:
        """
        Fetches the saved Lease Balances report directly from RentVine REST API
        using endpoint /api/manager/reports/lease-balance?reportID={report_id}.
        This report is the single source of truth for overdue tenants and active filters.
        """
        raw_data = None
        if not self.mock_mode and self._api_base:
            try:
                raw_data = self._rest_get(f"reports/lease-balance?reportID={report_id}")
                logger.info(f"Successfully fetched live RentVine reportID={report_id}: {raw_data.get('title')}")
                # Update local cache with live data
                cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scratch", f"report_{report_id}.json")
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(raw_data, f, indent=2)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Live RentVine report fetch failed for reportID={report_id} ({e}). Checking local cache.")

        if not raw_data:
            # Fallback to cached scratch/report_72.json if available
            cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scratch", f"report_{report_id}.json")
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        raw_data = json.load(f)
                    logger.info(f"Loaded RentVine reportID={report_id} from cached file {cache_path}")
                except Exception as e:
                    logger.warning(f"Failed to read cached report {cache_path}: {e}")

        if not raw_data:
            raise RuntimeError(f"RentVine reportID={report_id} could not be retrieved from API or local cache.")

        # Parse applied filters
        filters_applied = []
        for flt in raw_data.get("displayFilters", []):
            filters_applied.append({
                "name": flt.get("name"),
                "label": flt.get("label"),
                "comparator": flt.get("comparator"),
                "details": flt.get("details", "").strip(),
                "values": flt.get("value")
            })

        # Parse totals
        totals = raw_data.get("totals", {}).get("data", {})

        # Parse tenant rows
        rows = []
        for r in raw_data.get("rows", []):
            d = r.get("data", {})
            lease_id = str(d.get("leaseID", "")).strip()
            tenant_name = d.get("primaryTenantName") or "Unknown Tenant"
            phone = d.get("primaryTenantPhone") or ""
            email = d.get("primaryTenantEmail") or ""
            prop_name = d.get("propertyName") or ""
            unit_name = d.get("unitName") or ""
            unit_addr = d.get("unitAddress") or ""
            lease_status = d.get("leaseStatusName") or "Active"

            rent_amt = float(d.get("rentAmount") or 0.0)
            past_due_rent = float(d.get("pastDueRentAmount") or 0.0)
            past_due_total = float(d.get("pastDueTotalAmount") or 0.0)
            total_balance = float(d.get("totalBalanceDueAmount") or 0.0)

            # Extract Follow-Up Notes - Automation Status (RentVine custom field lease_text_2)
            automation_status = (d.get("lease_text_2") or "").strip()
            lp1 = (d.get("lease_paragraph_1") or "").strip()
            note_val = (d.get("note") or "").strip()
            lease_notes = (d.get("leaseNotes") or "").strip()

            # Exact field matching RentVine's "Follow-Up Notes - Automation Status" column
            follow_up = automation_status if automation_status else (lp1 or note_val or lease_notes)
            has_manual_review = (
                "manual review" in automation_status.lower() 
                or "manual review" in lp1.lower() 
                or "manual review" in note_val.lower()
            )

            # Determine reminder eligibility:
            # - Must be Active lease with overdue balance
            # - When Follow-Up Notes contains "Manual Review": Disabled (Manual Review / Disputes)
            if (past_due_total <= 0) and (total_balance <= 0):
                is_eligible = False
                eligibility_reason = "Paid / Settled: $0.00 balance"
            elif has_manual_review:
                is_eligible = False
                eligibility_reason = "Manual Review / Disputes: Automated reminders disabled per RentVine Follow-Up Notes"
            else:
                is_eligible = True
                eligibility_reason = f"Eligible: Total balance ${total_balance:,.2f} | Total past due ${past_due_total:,.2f}"

            # Extract co-tenants: RentVine includes all lease tenants in 'tenants' field (e.g. "Kukunoor Tenant, second Kukunoor Tenant")
            raw_tenants = d.get("tenants") or d.get("tenantNames") or d.get("additionalTenants") or ""
            if isinstance(raw_tenants, str):
                all_tenant_names = [n.strip() for n in raw_tenants.split(",") if n.strip()]
            elif isinstance(raw_tenants, list):
                all_tenant_names = [str(n).strip() for n in raw_tenants if str(n).strip()]
            else:
                all_tenant_names = []
            # Remove the primary from the co-tenant list
            co_tenants = [n for n in all_tenant_names if n.strip().lower() != tenant_name.strip().lower()]
            # Build contact map for co-tenants
            co_tenant_contacts: Dict[str, Any] = {}
            for ct_name in co_tenants:
                ct_name = ct_name.strip()
                c_info = self.get_cached_or_api_contact(ct_name)
                co_tenant_contacts[ct_name] = {
                    "phone": d.get(f"coTenantPhone_{ct_name}") or c_info.get("phone", ""),
                    "email": d.get(f"coTenantEmail_{ct_name}") or c_info.get("email", "")
                }

            rows.append({
                "lease_id": lease_id,
                "tenant_name": tenant_name,
                "first_name": d.get("primaryTenantFirstName") or tenant_name.split()[0],
                "last_name": d.get("primaryTenantLastName") or (tenant_name.split()[-1] if len(tenant_name.split()) > 1 else ""),
                "phone": phone,
                "email": email,
                "co_tenants": co_tenants,
                "co_tenant_contacts": co_tenant_contacts,
                "property_name": prop_name,
                "unit_name": unit_name,
                "unit_address": unit_addr,
                "property_address": d.get("propertyAddress") or "",
                "lease_status": lease_status,
                "monthly_rent": rent_amt,
                "past_due_rent": past_due_rent,
                "past_due_total": past_due_total,
                "total_balance": total_balance,
                "follow_up_notes": automation_status,
                "automation_status": automation_status,
                "has_manual_review": has_manual_review,
                "start_date": d.get("startDate"),
                "end_date": d.get("endDate"),
                "portal_link": f"https://redbricksproperties.rentvine.com/portal/pay",
                "is_eligible": is_eligible,
                "eligibility_reason": eligibility_reason
            })

        return {
            "report_id": raw_data.get("savedReportID", str(report_id)),
            "report_name": raw_data.get("savedReport", {}).get("name", "Rent Rem Praveen"),
            "title": raw_data.get("title", "Lease Balances"),
            "report_url": f"https://redbricksproperties.rentvine.com/reports/lease-balances?reportID={report_id}",
            "record_count": len(rows),
            "filters": filters_applied,
            "totals": {
                "total_balance_due": float(totals.get("totalBalanceDueAmount") or 0.0),
                "past_due_rent": float(totals.get("pastDueRentAmount") or 0.0),
                "past_due_total": float(totals.get("pastDueTotalAmount") or 0.0),
                "rent_amount": float(totals.get("rentAmount") or 0.0),
                "security_deposit_balance": float(totals.get("securityDepositBalance") or 0.0)
            },
            "records": rows,
            "fetched_at": datetime.utcnow().isoformat()
        }

    def sync_report_to_database(self, report_id: int = 72) -> Dict[str, Any]:
        """
        Synchronizes the authoritative Report #72 into the local SQLite database.
        - Updates or creates tenant records with clean property street addresses.
        - Resolves exact billing periods and lease-specific due dates from RentVine transactions.
        - Sets exact past due balances on rentvine_records.
        - Dynamically updates Manual Review status based on RentVine Follow-Up Notes.
        - When Manual Review is removed and field becomes blank, automatically clears the restriction.
        - Sets all other non-report tenants to balance = 0.00 so only Report #72 overdue tenants remain active.
        """
        report_data = self.fetch_lease_balance_report(report_id)
        records = report_data.get("records", [])

        synced_count = 0
        report_tenant_ids = set()

        # Parallel fetch transactions for all report leases to extract exact billing periods and due dates
        lease_tx_map = {}
        if not self.mock_mode and self._api_base:
            def _fetch_tx(lid):
                txs, _ = self._rest_get_with_headers(f"accounting/transactions?leaseID={lid}&pageSize=25")
                return lid, txs if isinstance(txs, list) else []

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(_fetch_tx, r["lease_id"]): r["lease_id"] for r in records}
                for f in as_completed(futures):
                    try:
                        lid, tx_list = f.result()
                        lease_tx_map[lid] = tx_list
                    except Exception as err:
                        logger.warning(f"Failed to fetch transactions for lease {futures[f]}: {err}")

        for rec in records:
            lease_id = rec["lease_id"]
            phone = rec["phone"]
            name = rec["tenant_name"]
            first_name = rec["first_name"]
            last_name = rec["last_name"]
            email = rec["email"]
            prop_name = rec["property_name"]
            prop_addr = rec.get("property_address") or prop_name
            clean_prop = clean_property_address(prop_addr)
            unit_name = rec["unit_name"] or rec["unit_address"]
            follow_up = rec.get("follow_up_notes", "")
            has_manual_review = "manual review" in (follow_up or "").lower()

            # Check if tenant exists by phone or lease_id
            existing_tenant = None
            if phone:
                existing_tenant = db.get_tenant_by_phone(phone)

            tenant_id = existing_tenant.id if existing_tenant else f"rv_lease_{lease_id}"
            report_tenant_ids.add(tenant_id)

            # Dynamic sync:
            # - When Follow-Up Notes contains "Manual Review", mark is_excluded = True
            # - When Follow-Up Notes is blank or Manual Review is removed, mark is_excluded = False
            if has_manual_review:
                is_excluded = True
                exclusion_reason = "Manual Review in RentVine Follow-Up Notes"
            else:
                is_excluded = False
                exclusion_reason = None

            t_obj = Tenant(
                id=tenant_id,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                email=email,
                property_name=clean_prop,
                unit_number=unit_name,
                is_excluded=is_excluded,
                exclusion_reason=exclusion_reason,
                lease_id=lease_id
            )
            db.upsert_tenant(t_obj)

            # Purge any obsolete duplicate RV-{lease_id} record to prevent duplicate primary records
            with db.get_connection() as conn:
                conn.execute("DELETE FROM tenants WHERE id = ? AND lease_id = ?", (f"RV-{lease_id}", str(lease_id)))
                conn.execute("DELETE FROM rentvine_records WHERE tenant_id = ?", (f"RV-{lease_id}",))
                conn.commit()

            # Sync co-tenants: additional tenants listed on the same lease in RentVine
            co_tenant_names = rec.get("co_tenants", [])  # populated by fetch_lease_balance_report
            co_tenant_contacts = rec.get("co_tenant_contacts", {})  # {name: {phone, email}}
            for idx, co_name in enumerate(co_tenant_names):
                co_name = co_name.strip()
                if not co_name or co_name.lower() == name.lower():
                    continue  # Skip blank or duplicate of primary
                co_name_parts = co_name.split(" ", 1)
                co_first = co_name_parts[0]
                co_last = co_name_parts[1] if len(co_name_parts) > 1 else ""
                co_contact = co_tenant_contacts.get(co_name, {})
                co_phone = co_contact.get("phone", "")
                if not co_phone:
                    c_info = self.get_cached_or_api_contact(co_name)
                    co_phone = c_info.get("phone", "")
                co_email = co_contact.get("email") or f"{co_name.lower().replace(' ', '.')}@rentvine.demo"
                # Co-tenant exclusion inherits the lease exclusion status
                co_id = f"rv_lease_{lease_id}_co{idx + 1}"
                # Check if this exact co-tenant already exists by phone (must not overwrite primary or different tenant)
                if co_phone:
                    existing_co = db.get_tenant_by_phone(co_phone)
                    if existing_co and existing_co.id != tenant_id and existing_co.full_name.strip().lower() == co_name.strip().lower():
                        co_id = existing_co.id
                co_obj = Tenant(
                    id=co_id,
                    first_name=co_first,
                    last_name=co_last,
                    phone=co_phone,
                    email=co_email,
                    property_name=clean_prop,
                    unit_number=unit_name,
                    is_excluded=is_excluded,
                    exclusion_reason=exclusion_reason,
                    lease_id=lease_id
                )
                db.upsert_tenant(co_obj)
                # Co-tenants share the same RentVine record (same lease balance)
                co_rv = RentVineRecord(
                    tenant_id=co_id,
                    balance=0.0,  # Co-tenant balance tracked under primary tenant
                    monthly_rent=rec["monthly_rent"],
                    due_date="",
                    portal_link=rec["portal_link"],
                    lease_status="ACTIVE",
                    follow_up_notes=follow_up
                )
                db.upsert_rentvine_record(co_rv)
                logger.info(f"[Co-Tenant] Synced co-tenant {co_name} ({co_phone}) for lease {lease_id}")

            # Resolve exact billing period and due date from transactions
            tx_list = lease_tx_map.get(lease_id, [])
            billing_details = self.extract_unpaid_rent_details(tx_list, total_balance=rec["total_balance"])
            actual_due = billing_details["due_date"]

            # Upsert RentVine record with authoritative balance and actual due date
            rv_rec = RentVineRecord(
                tenant_id=tenant_id,
                balance=rec["total_balance"],
                monthly_rent=rec["monthly_rent"],
                due_date=actual_due,
                last_payment_date=rec.get("last_payment_date"),
                last_payment_amount=float(rec.get("last_payment_amount") or 0.0),
                portal_link=rec["portal_link"],
                lease_status="ACTIVE",
                follow_up_notes=follow_up
            )
            db.upsert_rentvine_record(rv_rec)
            synced_count += 1

        # Clear balances of any tenants not in Report #72
        all_tenants = db.get_all_tenants()
        cleared_count = 0
        for t in all_tenants:
            if t.id not in report_tenant_ids:
                existing_rv = db.get_rentvine_record(t.id)
                if existing_rv and existing_rv.balance > 0:
                    existing_rv.balance = 0.0
                    db.upsert_rentvine_record(existing_rv)
                    cleared_count += 1

        logger.info(
            f"[Report 72 Sync] Synced {synced_count} authoritative overdue tenants from Report #{report_id}. "
            f"Cleared {cleared_count} non-report tenants to $0.00."
        )

        return {
            "report_id": report_id,
            "synced_tenants": synced_count,
            "cleared_tenants": cleared_count,
            "totals": report_data.get("totals", {})
        }
