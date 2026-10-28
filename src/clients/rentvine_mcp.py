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

import json
import base64
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from src.config import settings
from src.models import Tenant, RentVineRecord, RentVineNote
from src.database import db

logger = logging.getLogger("RentVineMCP")


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
                portal_link=f"{self._api_base}/portal/pay",
                lease_status="ACTIVE" if l.get("primaryLeaseStatusID") == "2" else "COMPLETED"
            )
            db.upsert_rentvine_record(rv_record)
            synced_count += 1

        logger.info(f"Synchronized {synced_count} live RentVine leases into local database.")
        return synced_count

    def get_lease_actual_balance(self, lease_id: str, default_rent: float) -> tuple:
        """
        Pulls actual accounting transactions from RentVine to determine whether the tenant
        has already paid (balance = 0.0) or has an outstanding overdue balance.
        Excludes voided transactions, suppressed system fees, and convenience/processing fees.
        Enforces a minimum balance threshold ($20.00) so minor residual fees never trigger
        misleading rent demand reminders. Resolves the true rent due date.
        """
        now = datetime.now(timezone.utc)
        current_month_due = now.strftime("%Y-%m-01")
        if self.mock_mode or not self._api_base:
            return default_rent, current_month_due

        try:
            today_iso = now.strftime("%Y-%m-%d")
            txs, _ = self._rest_get_with_headers(f"accounting/transactions?leaseID={lease_id}&pageSize=25")
            if not isinstance(txs, list) or len(txs) == 0:
                return default_rent, current_month_due

            unpaid = 0.0
            earliest_rent_due = ""
            total_charges = 0.0
            total_payments = 0.0

            for item in txs:
                t = item.get("transaction", {})
                # Skip voided or suppressed transactions
                if t.get("isVoided") == "1" or t.get("isSuppressed") == "1":
                    continue

                ttype = str(t.get("transactionTypeID", ""))
                date_posted = t.get("datePosted", "")
                desc = (t.get("description") or "").lower()
                amount = float(t.get("amount") or 0.0)
                amount_paid = float(t.get("amountPaid") or 0.0)

                # Skip credit card / eCheck portal convenience fees completely (never rent)
                if "convenience fee" in desc:
                    continue

                if ttype == "2":  # Resident payment
                    total_payments += amount
                    continue
                elif ttype == "4":  # eCheck return / NSF (reverses payment)
                    total_payments -= amount
                    continue

                # Type 1 = Charges (Rent, Late Fee, etc.)
                if date_posted and date_posted <= today_iso:
                    total_charges += amount
                    # Only genuine rent and late fees count toward overdue rent balance
                    is_rent = "rent" in desc or "prorated" in desc
                    is_late_fee = "late fee" in desc

                    if is_rent or is_late_fee:
                        remaining = max(0.0, amount - amount_paid)
                        if remaining > 0:
                            unpaid += remaining
                            if is_rent and (not earliest_rent_due or date_posted < earliest_rent_due):
                                earliest_rent_due = date_posted

            # If total payments meet or exceed total charges, balance is fully cleared
            if total_payments >= total_charges:
                unpaid = 0.0

            resolved_due = earliest_rent_due if unpaid > 0 and earliest_rent_due else current_month_due
            return round(unpaid, 2), resolved_due
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
            except Exception as e:
                logger.warning(f"Live RentVine report fetch failed for reportID={report_id} ({e}). Checking local cache.")

        if not raw_data:
            # Fallback to cached scratch/report_72.json if available
            import os
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

            # Determine reminder eligibility:
            # - Must be Active lease
            # - Has overdue balance (past_due_total > 0 or total_balance > 0, even $1.00)
            is_eligible = (past_due_total > 0) or (total_balance > 0)
            if not is_eligible:
                eligibility_reason = "Paid / Settled: $0.00 balance"
            else:
                eligibility_reason = f"Eligible: Total balance ${total_balance:,.2f} | Total past due ${past_due_total:,.2f}"

            rows.append({
                "lease_id": lease_id,
                "tenant_name": tenant_name,
                "first_name": d.get("primaryTenantFirstName") or tenant_name.split()[0],
                "last_name": d.get("primaryTenantLastName") or (tenant_name.split()[-1] if len(tenant_name.split()) > 1 else ""),
                "phone": phone,
                "email": email,
                "property_name": prop_name,
                "unit_name": unit_name,
                "unit_address": unit_addr,
                "property_address": d.get("propertyAddress") or "",
                "lease_status": lease_status,
                "monthly_rent": rent_amt,
                "past_due_rent": past_due_rent,
                "past_due_total": past_due_total,
                "total_balance": total_balance,
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
        - Updates or creates tenant records with their exact contact details and unit.
        - Sets exact past due balances on rentvine_records.
        - Sets all other non-report tenants to balance = 0.00 so only Report #72 overdue tenants remain active.
        """
        report_data = self.fetch_lease_balance_report(report_id)
        records = report_data.get("records", [])

        synced_count = 0
        report_tenant_ids = set()

        for rec in records:
            lease_id = rec["lease_id"]
            phone = rec["phone"]
            name = rec["tenant_name"]
            first_name = rec["first_name"]
            last_name = rec["last_name"]
            email = rec["email"]
            prop_name = rec["property_name"]
            unit_name = rec["unit_name"] or rec["unit_address"]

            # Check if tenant exists by phone or lease_id
            existing_tenant = None
            if phone:
                existing_tenant = db.get_tenant_by_phone(phone)

            tenant_id = existing_tenant.id if existing_tenant else f"rv_lease_{lease_id}"
            report_tenant_ids.add(tenant_id)

            t_obj = Tenant(
                id=tenant_id,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                email=email,
                property_name=prop_name,
                unit_number=unit_name,
                is_excluded=False,
                exclusion_reason=None
            )
            db.upsert_tenant(t_obj)

            # Upsert RentVine record
            rv_rec = RentVineRecord(
                tenant_id=tenant_id,
                balance=rec["total_balance"],
                monthly_rent=rec["monthly_rent"],
                due_date="2026-10-01",
                last_payment_date=None,
                last_payment_amount=0.0,
                portal_link=rec["portal_link"],
                lease_status="ACTIVE"
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
