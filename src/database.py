"""
Red Bricks Property Management - SQLite Database & Persistence Layer
Tracks tenants, RentVine financials, SMS/Email message history, deduplication logs,
audit trail of decisions, and retry queue.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from src.config import settings
from src.models import (
    Tenant, RentVineRecord, RentVineNote, CommunicationMessage,
    Channel, MessageDirection, DecisionOutcome, WorkflowDecision,
    DailyRunSummary, RetryQueueItem
)

class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DATABASE_PATH
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Tenants table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                property_name TEXT NOT NULL,
                unit_number TEXT NOT NULL,
                is_excluded INTEGER DEFAULT 0,
                exclusion_reason TEXT,
                created_at TEXT NOT NULL
            )
            """)

            # 2. RentVine records table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS rentvine_records (
                tenant_id TEXT PRIMARY KEY,
                balance REAL NOT NULL,
                monthly_rent REAL NOT NULL,
                due_date TEXT NOT NULL,
                last_payment_date TEXT,
                last_payment_amount REAL DEFAULT 0.0,
                portal_link TEXT,
                lease_status TEXT DEFAULT 'ACTIVE',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            )
            """)

            # 3. RentVine notes table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS rentvine_notes (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                author TEXT NOT NULL,
                text TEXT NOT NULL,
                is_automated INTEGER DEFAULT 1,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            )
            """)

            # 4. Communication messages (Quo SMS & Gmail)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS communication_messages (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                direction TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT DEFAULT 'DELIVERED',
                metadata_json TEXT DEFAULT '{}',
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            )
            """)

            # 5. Workflow decisions audit trail (for deduplication and history)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS workflow_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                tenant_id TEXT NOT NULL,
                tenant_name TEXT NOT NULL,
                property_unit TEXT NOT NULL,
                balance REAL NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                details_json TEXT DEFAULT '{}',
                sms_sent INTEGER DEFAULT 0,
                sms_message_id TEXT,
                note_written INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            )
            """)

            # 6. Daily runs summary
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                is_dry_run INTEGER DEFAULT 0,
                total_tenants INTEGER NOT NULL,
                count_paid INTEGER NOT NULL,
                count_promised INTEGER NOT NULL,
                count_reminded INTEGER NOT NULL,
                count_skipped INTEGER NOT NULL,
                count_flagged INTEGER NOT NULL,
                count_failed INTEGER NOT NULL,
                total_balance_unpaid REAL NOT NULL,
                summary_json TEXT NOT NULL
            )
            """)

            # 7. Retry queue for failed sends
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempt_count INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                last_error TEXT,
                next_retry_at TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            )
            """)

            conn.commit()

    def reset_database(self):
        """Clears all records from tables to reseed cleanly."""
        with self.get_connection() as conn:
            for table in [
                "workflow_decisions", "daily_runs", "retry_queue",
                "communication_messages", "rentvine_notes", "rentvine_records", "tenants"
            ]:
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    # --- Tenant & RentVine Operations ---

    def upsert_tenant(self, tenant: Tenant):
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO tenants (id, first_name, last_name, phone, email, property_name, unit_number, is_excluded, exclusion_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                phone=excluded.phone,
                email=excluded.email,
                property_name=excluded.property_name,
                unit_number=excluded.unit_number,
                is_excluded=excluded.is_excluded,
                exclusion_reason=excluded.exclusion_reason
            """, (
                tenant.id, tenant.first_name, tenant.last_name, tenant.phone,
                tenant.email, tenant.property_name, tenant.unit_number,
                1 if tenant.is_excluded else 0, tenant.exclusion_reason, tenant.created_at
            ))
            conn.commit()

    def upsert_rentvine_record(self, record: RentVineRecord):
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO rentvine_records (tenant_id, balance, monthly_rent, due_date, last_payment_date, last_payment_amount, portal_link, lease_status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                balance=excluded.balance,
                monthly_rent=excluded.monthly_rent,
                due_date=excluded.due_date,
                last_payment_date=excluded.last_payment_date,
                last_payment_amount=excluded.last_payment_amount,
                portal_link=excluded.portal_link,
                lease_status=excluded.lease_status,
                updated_at=excluded.updated_at
            """, (
                record.tenant_id, record.balance, record.monthly_rent,
                record.due_date, record.last_payment_date, record.last_payment_amount,
                record.portal_link, record.lease_status, datetime.utcnow().isoformat()
            ))
            conn.commit()

    def add_rentvine_note(self, tenant_id: str, note: RentVineNote):
        with self.get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO rentvine_notes (id, tenant_id, created_at, author, text, is_automated)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                note.id, tenant_id, note.created_at, note.author,
                note.text, 1 if note.is_automated else 0
            ))
            conn.commit()

    def get_rentvine_notes(self, tenant_id: str) -> List[RentVineNote]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT id, created_at, author, text, is_automated
            FROM rentvine_notes
            WHERE tenant_id = ?
            ORDER BY created_at DESC
            """, (tenant_id,))
            rows = cursor.fetchall()
            return [
                RentVineNote(
                    id=r["id"],
                    created_at=r["created_at"],
                    author=r["author"],
                    text=r["text"],
                    is_automated=bool(r["is_automated"])
                ) for r in rows
            ]

    def get_all_tenants(self) -> List[Tenant]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tenants ORDER BY property_name, unit_number")
            rows = cursor.fetchall()
            return [
                Tenant(
                    id=r["id"],
                    first_name=r["first_name"],
                    last_name=r["last_name"],
                    phone=r["phone"],
                    email=r["email"],
                    property_name=r["property_name"],
                    unit_number=r["unit_number"],
                    is_excluded=bool(r["is_excluded"]),
                    exclusion_reason=r["exclusion_reason"],
                    created_at=r["created_at"]
                ) for r in rows
            ]

    def get_rentvine_record(self, tenant_id: str) -> Optional[RentVineRecord]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM rentvine_records WHERE tenant_id = ?", (tenant_id,))
            row = cursor.fetchone()
            if not row:
                return None
            notes = self.get_rentvine_notes(tenant_id)
            return RentVineRecord(
                tenant_id=row["tenant_id"],
                balance=row["balance"],
                monthly_rent=row["monthly_rent"],
                due_date=row["due_date"],
                last_payment_date=row["last_payment_date"],
                last_payment_amount=row["last_payment_amount"],
                notes=notes,
                portal_link=row["portal_link"],
                lease_status=row["lease_status"]
            )

    # --- Communication Messages (SMS / Email) ---

    def save_message(self, message: CommunicationMessage):
        with self.get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO communication_messages (id, tenant_id, channel, direction, sender, recipient, content, timestamp, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message.id, message.tenant_id, message.channel.value,
                message.direction.value, message.sender, message.recipient,
                message.content, message.timestamp, message.status,
                json.dumps(message.metadata)
            ))
            conn.commit()

    def get_messages_for_tenant(self, tenant_id: str, limit: int = 50) -> List[CommunicationMessage]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM communication_messages
            WHERE tenant_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
            """, (tenant_id, limit))
            rows = cursor.fetchall()
            messages = []
            for r in rows:
                try:
                    meta = json.loads(r["metadata_json"])
                except Exception:
                    meta = {}
                messages.append(
                    CommunicationMessage(
                        id=r["id"],
                        tenant_id=r["tenant_id"],
                        channel=Channel(r["channel"]),
                        direction=MessageDirection(r["direction"]),
                        sender=r["sender"],
                        recipient=r["recipient"],
                        content=r["content"],
                        timestamp=r["timestamp"],
                        status=r["status"],
                        metadata=meta
                    )
                )
            return messages

    def get_all_recent_messages(self, limit: int = 200, channel: Optional[Channel] = None) -> List[CommunicationMessage]:
        """Bulk retrieves recent messages in a single fast query."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if channel:
                cursor.execute("""
                SELECT * FROM communication_messages
                WHERE channel = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """, (channel.value, limit))
            else:
                cursor.execute("""
                SELECT * FROM communication_messages
                ORDER BY timestamp DESC
                LIMIT ?
                """, (limit,))
            rows = cursor.fetchall()
            messages = []
            for r in rows:
                try:
                    meta = json.loads(r["metadata_json"])
                except Exception:
                    meta = {}
                messages.append(
                    CommunicationMessage(
                        id=r["id"],
                        tenant_id=r["tenant_id"],
                        channel=Channel(r["channel"]),
                        direction=MessageDirection(r["direction"]),
                        sender=r["sender"],
                        recipient=r["recipient"],
                        content=r["content"],
                        timestamp=r["timestamp"],
                        status=r["status"],
                        metadata=meta
                    )
                )
            return messages

    # --- Deduplication & Decision Tracking ---

    def get_last_successful_reminder(self, tenant_id: str) -> Optional[WorkflowDecision]:
        """
        Finds the most recent reminder sent to this tenant to prevent duplicate messages.
        Cooldown is ONLY active once a reminder was ACTUALLY dispatched (SENT_HITL_APPROVED, DELIVERED, or SENT).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM communication_messages
            WHERE tenant_id = ? AND direction = 'OUTBOUND' 
              AND status IN ('SENT_HITL_APPROVED', 'DELIVERED', 'SENT')
            ORDER BY timestamp DESC
            LIMIT 1
            """, (tenant_id,))
            msg_row = cursor.fetchone()
            if msg_row:
                return WorkflowDecision(
                    tenant_id=tenant_id,
                    tenant_name=tenant_id,
                    property_unit="",
                    balance=0.0,
                    outcome=DecisionOutcome.SEND_REMINDER,
                    reason=f"Actual Reminder SMS dispatched ({msg_row['status']})",
                    details={"message_id": msg_row["id"], "content": msg_row["content"]},
                    timestamp=msg_row["timestamp"],
                    sms_sent=True,
                    sms_message_id=msg_row["id"],
                    note_written=False,
                    run_id="actual_dispatch"
                )

            return None

    def save_decision(self, decision: WorkflowDecision):
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO workflow_decisions (
                run_id, tenant_id, tenant_name, property_unit, balance,
                outcome, reason, details_json, sms_sent, sms_message_id,
                note_written, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision.run_id, decision.tenant_id, decision.tenant_name,
                decision.property_unit, decision.balance, decision.outcome.value,
                decision.reason, json.dumps(decision.details),
                1 if decision.sms_sent else 0, decision.sms_message_id,
                1 if decision.note_written else 0, decision.timestamp
            ))
            conn.commit()

    def get_latest_decisions(self, limit: int = 100) -> List[WorkflowDecision]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM workflow_decisions
            ORDER BY id DESC
            LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            decisions = []
            for r in rows:
                try:
                    details = json.loads(r["details_json"])
                except Exception:
                    details = {}
                decisions.append(
                    WorkflowDecision(
                        tenant_id=r["tenant_id"],
                        tenant_name=r["tenant_name"],
                        property_unit=r["property_unit"],
                        balance=r["balance"],
                        outcome=DecisionOutcome(r["outcome"]),
                        reason=r["reason"],
                        details=details,
                        timestamp=r["timestamp"],
                        sms_sent=bool(r["sms_sent"]),
                        sms_message_id=r["sms_message_id"],
                        note_written=bool(r["note_written"]),
                        run_id=r["run_id"]
                    )
                )
            return decisions

    def get_decisions_for_tenant(self, tenant_id: str, limit: int = 50) -> List[WorkflowDecision]:
        """Returns all decision records for a specific tenant in reverse chronological order."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM workflow_decisions
            WHERE tenant_id = ?
            ORDER BY id DESC
            LIMIT ?
            """, (tenant_id, limit))
            rows = cursor.fetchall()
            decisions = []
            for r in rows:
                try:
                    details = json.loads(r["details_json"])
                except Exception:
                    details = {}
                decisions.append(
                    WorkflowDecision(
                        tenant_id=r["tenant_id"],
                        tenant_name=r["tenant_name"],
                        property_unit=r["property_unit"],
                        balance=r["balance"],
                        outcome=DecisionOutcome(r["outcome"]),
                        reason=r["reason"],
                        details=details,
                        timestamp=r["timestamp"],
                        sms_sent=bool(r["sms_sent"]),
                        sms_message_id=r["sms_message_id"],
                        note_written=bool(r["note_written"]),
                        run_id=r["run_id"]
                    )
                )
            return decisions

    # --- Runs & Summaries ---

    def save_run_summary(self, summary: DailyRunSummary):
        with self.get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO daily_runs (
                run_id, started_at, completed_at, is_dry_run,
                total_tenants, count_paid, count_promised, count_reminded,
                count_skipped, count_flagged, count_failed,
                total_balance_unpaid, summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary.run_id, summary.started_at, summary.completed_at,
                1 if summary.is_dry_run else 0, summary.total_tenants,
                summary.count_paid, summary.count_promised, summary.count_reminded,
                summary.count_skipped, summary.count_flagged, summary.count_failed,
                summary.total_balance_unpaid, json.dumps(summary.to_dict())
            ))
            conn.commit()

    def get_recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT summary_json FROM daily_runs ORDER BY started_at DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [json.loads(r["summary_json"]) for r in rows]

    # --- Retry Queue Operations ---

    def add_retry_queue_item(self, tenant_id: str, payload: Dict[str, Any], error: str) -> int:
        next_retry = (datetime.utcnow() + timedelta(seconds=settings.RETRY_BACKOFF_SECONDS)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO retry_queue (tenant_id, payload_json, attempt_count, max_attempts, last_error, next_retry_at, status, created_at)
            VALUES (?, ?, 1, ?, ?, ?, 'PENDING', ?)
            """, (tenant_id, json.dumps(payload), settings.MAX_RETRY_ATTEMPTS, error, next_retry, datetime.utcnow().isoformat()))
            conn.commit()
            return cursor.lastrowid

    def get_pending_retries(self) -> List[RetryQueueItem]:
        now_str = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM retry_queue
            WHERE status = 'PENDING' AND next_retry_at <= ? AND attempt_count < max_attempts
            """, (now_str,))
            rows = cursor.fetchall()
            items = []
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"])
                except Exception:
                    payload = {}
                items.append(
                    RetryQueueItem(
                        id=r["id"],
                        tenant_id=r["tenant_id"],
                        payload=payload,
                        attempt_count=r["attempt_count"],
                        max_attempts=r["max_attempts"],
                        last_error=r["last_error"],
                        next_retry_at=r["next_retry_at"],
                        status=r["status"]
                    )
                )
            return items

    def update_retry_item(self, item_id: int, status: str, error: Optional[str] = None):
        with self.get_connection() as conn:
            if status == "SUCCESS":
                conn.execute("UPDATE retry_queue SET status = 'SUCCESS' WHERE id = ?", (item_id,))
            else:
                next_retry = (datetime.utcnow() + timedelta(seconds=settings.RETRY_BACKOFF_SECONDS * 2)).isoformat()
                conn.execute("""
                UPDATE retry_queue
                SET attempt_count = attempt_count + 1,
                    last_error = ?,
                    next_retry_at = ?,
                    status = CASE WHEN attempt_count + 1 >= max_attempts THEN 'FAILED' ELSE 'PENDING' END
                WHERE id = ?
                """, (error or "Retry failed", next_retry, item_id))
            conn.commit()

# Global database instance
db = Database()
