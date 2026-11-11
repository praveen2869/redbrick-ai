"""
End-to-end tests for HITL Dispatch, Verification, Cooldown Isolation,
Final Reconciliation, and Automated Final Excel Report Generation.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import openpyxl

TEST_DB = "redbrick_final_report_test.db"
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["QUO_MOCK_MODE"] = "true"
os.environ["RENTVINE_MOCK_MODE"] = "true"

from src.config import settings
from src.database import db
from src.models import (
    Channel,
    CommunicationMessage,
    MessageDirection,
    RentVineRecord,
    Tenant,
)
from src.pipeline import run_pipeline_dispatch, run_pipeline_evaluate


class TestFinalReportPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig_db_path = db.db_path
        cls.orig_quo_mock = settings.QUO_MOCK_MODE
        cls.orig_rv_mock = settings.RENTVINE_MOCK_MODE
        settings.QUO_MOCK_MODE = True
        settings.RENTVINE_MOCK_MODE = True
        db.db_path = TEST_DB
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.db_path = cls.orig_db_path
        settings.QUO_MOCK_MODE = cls.orig_quo_mock
        settings.RENTVINE_MOCK_MODE = cls.orig_rv_mock
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass

    def setUp(self):
        settings.QUO_MOCK_MODE = True
        settings.RENTVINE_MOCK_MODE = True
        self.tmp_dir = tempfile.mkdtemp(prefix="rbpm_test_")
        # Reset tables
        with db.get_connection() as conn:
            for tbl in (
                "tenants",
                "rentvine_records",
                "communication_messages",
                "workflow_decisions",
                "workflow_reminders",
                "daily_runs",
                "retry_queue",
            ):
                conn.execute(f"DELETE FROM {tbl}")
            conn.commit()

        past_due = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")

        # 1. Group lease (approved in HITL -> should enter cooldown for BOTH co-tenants)
        db.upsert_tenant(Tenant(
            id="t_grp_1",
            lease_id="lease_100",
            first_name="Alice",
            last_name="One",
            phone="+18625550101",
            email="alice@example.com",
            property_name="100 Main St",
            unit_number="Unit A",
        ))
        db.upsert_tenant(Tenant(
            id="t_grp_2",
            lease_id="lease_100",
            first_name="Bob",
            last_name="One",
            phone="+18625550102",
            email="bob@example.com",
            property_name="100 Main St",
            unit_number="Unit A",
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_grp_1", balance=1500.0, monthly_rent=1500.0, due_date=past_due, lease_status="ACTIVE"
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_grp_2", balance=1500.0, monthly_rent=1500.0, due_date=past_due, lease_status="ACTIVE"
        ))

        # 2. Rejected tenant in HITL (must NOT receive SMS and must NOT enter cooldown)
        db.upsert_tenant(Tenant(
            id="t_rej_1",
            lease_id="lease_200",
            first_name="Charlie",
            last_name="Rejected",
            phone="+18625550201",
            email="charlie@example.com",
            property_name="200 Oak Ave",
            unit_number="Unit B",
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_rej_1", balance=1200.0, monthly_rent=1200.0, due_date=past_due, lease_status="ACTIVE"
        ))

        # 3. Failed dispatch tenant (must NOT enter cooldown, reported accurately as FAILED)
        db.upsert_tenant(Tenant(
            id="t_fail_1",
            lease_id="lease_300",
            first_name="Diana",
            last_name="Failed",
            phone="+18625550301",
            email="diana@example.com",
            property_name="300 Pine Rd",
            unit_number="Unit C",
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_fail_1", balance=950.0, monthly_rent=950.0, due_date=past_due, lease_status="ACTIVE"
        ))

        # 4. Tenant with normal Quo conversation only (must NOT trigger cooldown!)
        db.upsert_tenant(Tenant(
            id="t_chat_1",
            lease_id="lease_400",
            first_name="Evan",
            last_name="Chatty",
            phone="+18625550401",
            email="evan@example.com",
            property_name="400 Elm Ct",
            unit_number="Unit D",
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_chat_1", balance=1100.0, monthly_rent=1100.0, due_date=past_due, lease_status="ACTIVE"
        ))
        db.save_message(CommunicationMessage(
            id="normal_chat_1",
            tenant_id="t_chat_1",
            channel=Channel.SMS,
            direction=MessageDirection.OUTBOUND,
            sender="+19803007559",
            recipient="+18625550401",
            content="Hi Evan, maintenance will stop by at 2pm today!",
            timestamp=datetime.utcnow().isoformat(),
            status="DELIVERED",
            metadata={"source": "Quo REST API", "is_workflow_reminder": False},
        ))

        # 5. Paid / Cleared tenant
        db.upsert_tenant(Tenant(
            id="t_paid_1",
            lease_id="lease_500",
            first_name="Fiona",
            last_name="Paid",
            phone="+18625550501",
            email="fiona@example.com",
            property_name="500 Maple Dr",
            unit_number="Unit E",
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_paid_1", balance=0.0, monthly_rent=1400.0, due_date=past_due, lease_status="ACTIVE"
        ))

        # 6. Manual review (< $200) tenant
        db.upsert_tenant(Tenant(
            id="t_mr_1",
            lease_id="lease_600",
            first_name="George",
            last_name="LowBal",
            phone="+18625550601",
            email="george@example.com",
            property_name="600 Cedar Ln",
            unit_number="Unit F",
        ))
        db.upsert_rentvine_record(RentVineRecord(
            tenant_id="t_mr_1", balance=125.0, monthly_rent=1300.0, due_date=past_due, lease_status="ACTIVE"
        ))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_hitl_dispatch_cooldown_reconciliation_and_final_excel(self):
        """Verifies full HITL -> Quo Dispatch & Failure Handling -> Lease Cooldown -> Final Excel Report."""
        from src.clients.quo_sms import QuoSMSClient

        # Stage 1: Evaluate & stage drafts (run on fixed Day 10 so not in Days 1-4 pause)
        fixed_day10 = datetime(2026, 11, 10, 14, 0, 0)
        with patch("src.engine.rules.get_pm_datetime", return_value=fixed_day10):
            eval_out_dir = os.path.join(self.tmp_dir, "stage1")
            eval_metrics = run_pipeline_evaluate(output_dir=eval_out_dir)

        pending_path = os.path.join(eval_out_dir, "hitl_pending_reminders.json")
        self.assertTrue(os.path.exists(pending_path))
        with open(pending_path, "r", encoding="utf-8") as f:
            staged = json.load(f)

        # Normal chat tenant Evan MUST still be staged (normal Quo msg never triggers cooldown!)
        staged_names = [s["tenant_name"] for s in staged]
        self.assertTrue(any("Evan" in n for n in staged_names), f"Evan missing from staged: {staged_names}")

        # Simulate operator HITL selection:
        # Approve Alice/Bob group lease + Diana (who will fail carrier send) + Evan; Reject Charlie
        approved = [s for s in staged if "Rejected" not in s["tenant_name"]]
        rejected = [s for s in staged if "Rejected" in s["tenant_name"]]
        self.assertGreaterEqual(len(rejected), 1)

        hitl_selection_path = os.path.join(self.tmp_dir, "hitl_selection.json")
        with open(hitl_selection_path, "w", encoding="utf-8") as f:
            json.dump({"approved_tenants": approved, "rejected_tenants": rejected}, f)

        # Simulate Quo failure strictly for Diana Failed
        orig_send = QuoSMSClient.send_sms

        def mocked_send_sms(client_self, tenant_id, to_number, text, recipient_type="tenant", **kwargs):
            if tenant_id == "t_fail_1":
                raise RuntimeError("Quo API error 503: Carrier gateway timeout")
            return orig_send(
                client_self,
                tenant_id=tenant_id,
                to_number=to_number,
                text=text,
                recipient_type=recipient_type,
                **kwargs,
            )

        receipt_out_dir = os.path.join(self.tmp_dir, "stage2")
        with patch.object(QuoSMSClient, "send_sms", mocked_send_sms), \
             patch("src.engine.rules.get_pm_datetime", return_value=fixed_day10):
            receipt = run_pipeline_dispatch(
                input_file=hitl_selection_path,
                output_dir=receipt_out_dir,
                reconcile=True,
            )

        # Verify dispatch counts
        self.assertEqual(receipt["successfully_sent"], 2)  # lease_100 + lease_400
        self.assertEqual(receipt["skipped_by_operator"], 1)  # lease_200 (Charlie)
        self.assertEqual(receipt["failed"], 1)  # lease_300 (Diana)

        # Verify Final Excel path exists
        final_excel_path = receipt.get("final_report_path")
        self.assertTrue(final_excel_path and os.path.exists(final_excel_path))

        # Verify Cooldown Isolation Rules in DB:
        # 1. Group lease (BOTH t_grp_1 AND t_grp_2) MUST be in 24h cooldown!
        now_utc = datetime.utcnow()
        cool1, rem1, _, _, _ = db.get_cooldown_status("t_grp_1", current_time=now_utc, cooldown_hours=24.0)
        cool2, rem2, _, _, _ = db.get_cooldown_status("t_grp_2", current_time=now_utc, cooldown_hours=24.0)
        self.assertTrue(cool1, "Primary tenant of approved group lease must enter cooldown")
        self.assertTrue(cool2, "Co-tenant of approved group lease must enter cooldown")
        self.assertIsNotNone(rem1.timestamp)

        # 2. Rejected tenant MUST NOT enter cooldown
        cool_rej, _, _, _, _ = db.get_cooldown_status("t_rej_1", current_time=now_utc, cooldown_hours=24.0)
        self.assertFalse(cool_rej, "Rejected HITL tenant must NOT enter cooldown")

        # 3. Failed dispatch tenant MUST NOT enter cooldown
        cool_fail, _, _, _, _ = db.get_cooldown_status("t_fail_1", current_time=now_utc, cooldown_hours=24.0)
        self.assertFalse(cool_fail, "Failed SMS dispatch must NOT enter cooldown")

        # Verify Final Excel Workbook structure & content
        wb = openpyxl.load_workbook(final_excel_path)
        expected_sheets = [
            "Summary",
            "🚀 This Run — Dispatch",
            "All Active",
            "📩 Reminders Needed",
            "🛡️ Promises on File",
            "✅ Paid & Cleared",
            "🔴 Manual Review & Disputes",
            "🚫 In Cooldown & Grace",
        ]
        for sname in expected_sheets:
            self.assertIn(sname, wb.sheetnames, f"Missing expected sheet {sname} in {wb.sheetnames}")

        # Verify Cooldown sheet has Alice/Bob (lease_100) and Evan (lease_400), NOT Charlie or Diana
        ws_cool = wb["🚫 In Cooldown & Grace"]
        cool_rows_text = " ".join(
            str(ws_cool.cell(r, 4).value or "") for r in range(2, ws_cool.max_row + 1)
        )
        self.assertIn("Alice", cool_rows_text)
        self.assertIn("Evan", cool_rows_text)
        self.assertNotIn("Charlie", cool_rows_text)
        self.assertNotIn("Diana", cool_rows_text)

        # Verify Reminders Needed still contains Charlie (Rejected) & Diana (Failed) with accurate status
        ws_rem = wb["📩 Reminders Needed"]
        rem_rows_statuses = {
            str(ws_rem.cell(r, 4).value or ""): str(ws_rem.cell(r, 17).value or "")
            for r in range(2, ws_rem.max_row + 1)
        }
        self.assertIn("Charlie Rejected", rem_rows_statuses)
        self.assertIn("REJECTED", rem_rows_statuses["Charlie Rejected"])
        self.assertIn("Diana Failed", rem_rows_statuses)
        self.assertIn("FAILED", rem_rows_statuses["Diana Failed"])


if __name__ == "__main__":
    unittest.main()
