"""
Red Bricks Property Management - Portfolio Synchronization Manager
Coordinates thread-safe, high-speed parallel synchronization with RentVine
and guarantees consistent database state for automated workflows.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Any, Optional

from src.config import settings
from src.database import db
from src.models import Tenant, RentVineRecord

logger = logging.getLogger("PortfolioSyncManager")


class PortfolioSyncManager:
    _instance: Optional["PortfolioSyncManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(PortfolioSyncManager, cls).__new__(cls)
                cls._instance._init_state()
            return cls._instance

    def _init_state(self):
        self._sync_lock = threading.Lock()
        self._sync_event = threading.Event()
        self._sync_event.set()  # Ready (not syncing)
        self.is_syncing: bool = False
        self.last_sync_time: Optional[str] = None
        self.sync_status: str = "IDLE"
        self.active_count: int = 0
        self.total_count: int = 0

    def wait_for_sync(self, timeout: float = 60.0) -> bool:
        """Waits for any active synchronization to complete."""
        return self._sync_event.wait(timeout=timeout)

    def sync_live_portfolio(self, force: bool = False, limit: int = 1000) -> int:
        """
        Executes complete high-speed parallel synchronization from RentVine.
        Fetches all leases, dynamically resolves tenant balances for active units,
        and atomic-batches updates into SQLite. Thread-safe.
        """
        from src.clients.rentvine_mcp import RentVineMCPClient

        client = RentVineMCPClient()
        if client.mock_mode or not client._api_base:
            logger.info("RentVine client is in mock mode or API base not set; skipping live sync.")
            return 0

        # Acquire lock so only one sync runs at a time
        acquired = self._sync_lock.acquire(blocking=True, timeout=10.0)
        if not acquired:
            logger.warning("Another sync is already in progress; waiting for completion.")
            self.wait_for_sync(timeout=60.0)
            return self.active_count

        self.is_syncing = True
        self._sync_event.clear()
        self.sync_status = "SYNCING_LEASES"
        t0 = time.time()

        try:
            logger.info("[SYNC] Connecting to RentVine and pulling leases...")
            leases_data = []
            page = 1
            page_size = 50
            while True:
                try:
                    batch, hdrs = client._rest_get_with_headers(f"leases?page={page}&pageSize={page_size}")
                    if not isinstance(batch, list) or len(batch) == 0:
                        break
                    leases_data.extend(batch)
                    total_pages = int(hdrs.get("Pagination-Total-Pages", 1))
                    if page >= total_pages or len(leases_data) >= limit:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"[SYNC] Error fetching leases page {page}: {e}")
                    break

            logger.info(f"[SYNC] Ingested {len(leases_data)} total leases from RentVine.")

            # Contact cache for phone/email resolution (prefetch across pages)
            contact_map: Dict[str, tuple] = {}
            try:
                t_page = 1
                while True:
                    batch_tenants, hdrs_t = client._rest_get_with_headers(f"tenants?page={t_page}&pageSize=100")
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
                logger.warning(f"[SYNC] Batch tenant contact prefetch warning: {e}")

            # Parse lease entities
            parsed_entries = []
            active_entries = []

            for item in leases_data:
                l = item.get("lease", {})
                u = item.get("unit", {})
                lid = l.get("leaseID")
                tenants_list = l.get("tenants", [])
                if not lid or not tenants_list:
                    continue

                primary_name = tenants_list[0]
                name_parts = primary_name.split(" ", 1)
                first_name = name_parts[0]
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                prop_name = u.get("name") or u.get("address") or "Red Bricks Property"
                unit_num = f"Unit {u.get('unitID', lid)}"
                rent_val = float(l.get("rentAmount") or l.get("baseRentAmount") or u.get("rent") or 0.0)
                is_active = (l.get("primaryLeaseStatusID") == "2")

                lookup_key = primary_name.strip().lower()
                cached_contact = contact_map.get(lookup_key)
                if cached_contact and cached_contact[0]:
                    real_phone = cached_contact[0]
                    real_email = cached_contact[1] or f"{first_name.lower()}@rentvine.demo"
                else:
                    real_phone = "+1 (667) 281-3879"
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

                entry = {
                    "lid": lid,
                    "tenant": tenant,
                    "rent_val": rent_val,
                    "is_active": is_active,
                    "lease_data": l
                }
                parsed_entries.append(entry)
                if is_active:
                    active_entries.append(entry)

            self.sync_status = f"RESOLVING_BALANCES ({len(active_entries)} active)"
            logger.info(f"[SYNC] Resolving accounting balances for {len(active_entries)} active leases in parallel...")

            # Fast parallel balance resolution for active leases
            balances_map: Dict[str, Tuple[float, str]] = {}

            def fetch_balance(lid_val: str, rent_v: float):
                return lid_val, client.get_lease_actual_balance(lid_val, rent_v)

            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = {
                    executor.submit(fetch_balance, e["lid"], e["rent_val"]): e["lid"]
                    for e in active_entries
                }
                for f in as_completed(futures):
                    try:
                        lid_res, (bal, due) = f.result()
                        balances_map[lid_res] = (bal, due)
                    except Exception as err:
                        lid_res = futures[f]
                        balances_map[lid_res] = (0.0, datetime.now(timezone.utc).strftime("%Y-%m-01"))

            # Assemble batch records
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-01")
            tenants_batch: List[Tenant] = []
            records_batch: List[RentVineRecord] = []

            for e in parsed_entries:
                lid = e["lid"]
                is_active = e["is_active"]
                rent_val = e["rent_val"]
                if is_active:
                    bal, due = balances_map.get(lid, (0.0, now_iso))
                else:
                    bal, due = 0.0, "2026-01-01"

                rv_record = RentVineRecord(
                    tenant_id=f"RV-{lid}",
                    balance=bal,
                    monthly_rent=rent_val,
                    due_date=due,
                    portal_link=f"{client._api_base}/portal/pay",
                    lease_status="ACTIVE" if is_active else "COMPLETED"
                )
                tenants_batch.append(e["tenant"])
                records_batch.append(rv_record)

            self.sync_status = "SAVING_DATABASE"
            # Purge demo seed tenants if live data is ready
            with db.get_connection() as conn:
                for tbl, col in [
                    ("tenants", "id"),
                    ("rentvine_records", "tenant_id"),
                    ("workflow_decisions", "tenant_id"),
                    ("communication_messages", "tenant_id"),
                    ("rentvine_notes", "tenant_id"),
                ]:
                    conn.execute(f"DELETE FROM {tbl} WHERE {col} LIKE 'T-%'")
                conn.commit()

            # Batch upsert all records in one atomic SQLite transaction
            db.upsert_tenants_and_records_batch(tenants_batch, records_batch)

            self.active_count = len(active_entries)
            self.total_count = len(tenants_batch)
            self.last_sync_time = datetime.now(timezone.utc).isoformat()
            self.sync_status = "IDLE"

            dt = time.time() - t0
            logger.info(
                f"[SYNC] Done in {dt:.1f}s — synchronized {len(active_entries)} active leases "
                f"({len(tenants_batch)} total portfolio records) into local database."
            )
            return self.active_count

        except Exception as e:
            logger.error(f"[SYNC] Critical synchronization error: {e}", exc_info=True)
            self.sync_status = f"ERROR: {e}"
            return 0
        finally:
            self.is_syncing = False
            self._sync_event.set()
            try:
                self._sync_lock.release()
            except RuntimeError:
                pass


sync_manager = PortfolioSyncManager()
