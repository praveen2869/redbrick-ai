"""
Unit tests for the Two-Stage Monthly Reminder System.
Tests monthly schedule:
  - Days 1–4: Global SMS pause (data collection continues, no automated SMS)
  - Day 5: First-stage standard rent reminder
  - Day 6: Second-stage escalation reminder (10-day notice) with strict 7-condition gate
  - Day 7+: Standard rolling reminder with 24h cooldown
  - Multi-tenant group escalation messaging
"""

import unittest
import os
from datetime import datetime, timedelta

# Use isolated test database so production records are never modified
TEST_DB = "redbrick_stage_test.db"
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["QUO_MOCK_MODE"] = "true"
os.environ["RENTVINE_MOCK_MODE"] = "true"

from src.database import db
from src.config import settings
from src.models import (
    Tenant, RentVineRecord, DecisionOutcome, LeaseTenantGroup
)
from src.engine.rules import BusinessRulesEngine, ReminderStage
from src.workflow import WorkflowOrchestrator
from src.clients.quo_sms import QuoSMSClient
from src.clients.rentvine_mcp import RentVineMCPClient


class TestTwoStageReminders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.db_path = TEST_DB
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass

    def setUp(self):
        db.reset_database()
        self.rules = BusinessRulesEngine()
        self.quo = QuoSMSClient(mock_mode=True)
        self.rv = RentVineMCPClient(mock_mode=True)
        self.orch = WorkflowOrchestrator(
            rentvine_client=self.rv,
            quo_client=self.quo,
            rules_engine=self.rules
        )

    def _create_test_tenant(
        self,
        tenant_id="T-STG-1",
        first_name="Brian",
        last_name="Fagin",
        phone="+12817022437",
        balance=3520.0,
        due_date="November 1, 2026",
        lease_id="L-STG-100"
    ):
        t = Tenant(
            id=tenant_id,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email=f"{first_name.lower()}@test.com",
            property_name="2603 Bridle Brook Way",
            unit_number="Unit 401",
            lease_id=lease_id
        )
        db.upsert_tenant(t)
        rv_rec = RentVineRecord(
            tenant_id=tenant_id,
            balance=balance,
            monthly_rent=balance,
            due_date=due_date,
            portal_link="https://redbricksproperties.rentvine.com/portals/resident/make-payment"
        )
        db.upsert_rentvine_record(rv_rec)
        return t, rv_rec

    # ── 1. Days 1–4: Global SMS Pause ──────────────────────────────────────

    def test_days_1_to_4_global_sms_pause(self):
        """Days 1-4: Automated SMS reminders are paused. Data collection continues."""
        self._create_test_tenant()

        for day in [1, 2, 3, 4]:
            ref_dt = datetime(2026, 11, day, 10, 0, 0)
            stage = self.rules.get_reminder_stage(ref_dt)
            self.assertEqual(stage, ReminderStage.STAGE_PAUSE_GRACE.value)

            summary = self.orch.execute_daily_run(
                is_dry_run=True,
                reference_time=ref_dt,
                use_report_source_of_truth=False
            )
            self.assertEqual(summary.count_reminded, 0, f"Day {day} must not send reminders")
            self.assertEqual(summary.count_skipped, 1, f"Day {day} must skip due to grace/pause")
            self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.SKIP_GRACE_PERIOD)
            self.assertFalse(summary.decisions[0].sms_sent)

            # Ensure no reminders were logged into workflow_reminders
            rems = db.get_workflow_reminders("T-STG-1")
            self.assertEqual(len(rems), 0)

    # ── 2. Day 5: First-Stage Standard Reminder ─────────────────────────────

    def test_day_5_stage_1_standard_reminder(self):
        """Day 5: Unpaid eligible tenant receives standard Stage 1 rent reminder."""
        t, rv_rec = self._create_test_tenant()

        ref_dt = datetime(2026, 11, 5, 10, 0, 0)
        stage = self.rules.get_reminder_stage(ref_dt)
        self.assertEqual(stage, ReminderStage.STAGE_1_STANDARD.value)

        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=ref_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 1)
        dec = summary.decisions[0]
        self.assertEqual(dec.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(dec.details.get("stage"), 1)
        self.assertEqual(dec.details.get("reminder_type"), "RENT_REMINDER_STAGE_1")

        # Standard template verification
        sms_text = dec.details.get("sms_text", "")
        self.assertIn("The grace period ends today, November 5", sms_text)
        self.assertIn("avoid late fees beginning tomorrow, November 6", sms_text)
        self.assertNotIn("10-day notice", sms_text)

    # ── 3. Day 6: Second-Stage Escalation Reminder ──────────────────────────

    def test_day_6_stage_2_escalation_positive(self):
        """Day 6: If tenant received Stage 1 and remains unpaid, sends Stage 2 Escalation (10-day notice)."""
        t, rv_rec = self._create_test_tenant()

        # Seed confirmed Stage 1 dispatch on Day 5 at 9:00 AM
        day_5_dt = datetime(2026, 11, 5, 9, 0, 0)
        db.record_workflow_reminder(
            reminder_id="rem_stage1_sent",
            tenant_id="T-STG-1",
            reminder_type="RENT_REMINDER_STAGE_1",
            message_id="msg_s1_conf",
            phone=t.phone,
            content="Stage 1 reminder text",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at=day_5_dt.isoformat(),
            stage=1
        )
        self.assertTrue(db.has_received_stage_1_reminder("T-STG-1", 2026, 11))

        # Day 6 run (at 10:00 AM - 25 hours later, cooldown expired)
        day_6_dt = datetime(2026, 11, 6, 10, 0, 0)
        stage = self.rules.get_reminder_stage(day_6_dt)
        self.assertEqual(stage, ReminderStage.STAGE_2_ESCALATION.value)

        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 1)
        dec = summary.decisions[0]
        self.assertEqual(dec.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(dec.details.get("stage"), 2)
        self.assertEqual(dec.details.get("reminder_type"), "RENT_REMINDER_STAGE_2_ESCALATION")

        # Escalation template verification
        sms_text = dec.details.get("sms_text", "")
        self.assertIn("10-day notice regarding the unpaid rent", sms_text)
        self.assertIn("https://redbricksproperties.rentvine.com/portals/resident/make-payment", sms_text)
        self.assertIn("Please reply today with a specific date when you can pay", sms_text)
        self.assertIn("RBPM Bot", sms_text)

    # ── 4. Day 6: Strict Gate Conditions Blocking Escalation ────────────────

    def test_day_6_escalation_blocked_if_no_stage_1(self):
        """Condition 7: Escalation is blocked if Stage 1 reminder was never sent. Standard Stage 1 sent instead."""
        t, rv_rec = self._create_test_tenant()

        day_6_dt = datetime(2026, 11, 6, 10, 0, 0)
        # Stage 1 was NOT sent
        self.assertFalse(db.has_received_stage_1_reminder("T-STG-1", 2026, 11))

        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        # Legal notice escalation must NOT be sent, and must NOT fall back to standard template
        dec = summary.decisions[0]
        sms_text = dec.details.get("sms_text", "")
        self.assertNotIn("10-day notice", sms_text)
        self.assertNotIn("grace period ends today", sms_text)
        self.assertEqual(dec.outcome, DecisionOutcome.SKIP_ALREADY_REMINDED)
        self.assertIn("escalation suppressed", dec.reason.lower())

    def test_day_6_escalation_blocked_if_in_cooldown(self):
        """Condition 6: If cooldown (<24h) has not expired, do not dispatch escalation."""
        t, rv_rec = self._create_test_tenant()

        # Seed Day 5 dispatch at 6:00 PM
        day_5_dt = datetime(2026, 11, 5, 18, 0, 0)
        db.record_workflow_reminder(
            reminder_id="rem_stage1_cd",
            tenant_id="T-STG-1",
            reminder_type="RENT_REMINDER_STAGE_1",
            message_id="msg_s1_cd",
            phone=t.phone,
            content="Stage 1",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at=day_5_dt.isoformat(),
            stage=1
        )

        # Day 6 run at 8:00 AM (only 14 hours elapsed; cooldown active)
        day_6_early = datetime(2026, 11, 6, 8, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_early,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 0)
        self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.SKIP_ALREADY_REMINDED)
        self.assertIn("Cooldown active", summary.decisions[0].reason)

    def test_day_6_escalation_blocked_if_low_balance(self):
        """Condition 5: If balance is $0 < balance < $200, moved to Manual Review / Disputes."""
        t, rv_rec = self._create_test_tenant(balance=150.0)

        # Simulate Stage 1 was sent earlier
        now_iso = datetime(2026, 11, 5, 10, 0, 0).isoformat()
        db.record_workflow_reminder(
            reminder_id="rem_stage1_low",
            tenant_id="T-STG-1",
            reminder_type="RENT_REMINDER_STAGE_1",
            message_id="msg_stg1",
            phone=t.phone,
            content="Stage 1 reminder",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at=now_iso,
            stage=1
        )

        day_6_dt = datetime(2026, 11, 6, 11, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 0)
        self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.FLAG_MANUAL_REVIEW)
        self.assertIn("less than $200", summary.decisions[0].reason)

    def test_day_6_escalation_blocked_if_manual_review_notes(self):
        """Condition 4: If marked Manual Review in Follow-Up Notes, moved to Manual Review / Disputes."""
        t, rv_rec = self._create_test_tenant()
        rv_rec.follow_up_notes = "11/04/2026 - Manual Review: Tenant requested payment extension."
        db.upsert_rentvine_record(rv_rec)

        day_6_dt = datetime(2026, 11, 6, 11, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 0)
        self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.FLAG_MANUAL_REVIEW)

    def test_day_6_escalation_blocked_if_active_promise(self):
        """Condition 3: If active payment promise on file, held in HOLD_PROMISE."""
        t, rv_rec = self._create_test_tenant()
        rv_rec.follow_up_notes = "Tenant promised: will pay by 2026-11-10"
        db.upsert_rentvine_record(rv_rec)

        day_6_dt = datetime(2026, 11, 6, 11, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 0)
        self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.HOLD_PROMISE)

    def test_day_6_escalation_blocked_if_paid(self):
        """Condition 1 & 2: If rent paid (balance <= 0), no escalation message."""
        t, rv_rec = self._create_test_tenant(balance=0.0)

        day_6_dt = datetime(2026, 11, 6, 11, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 0)
        self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.SKIP_PAID)

    def test_day_6_duplicate_escalation_prevented(self):
        """Duplicate escalation prevention: do not send escalation twice in same billing cycle."""
        t, rv_rec = self._create_test_tenant()

        # Seed Stage 1 dispatch on Day 5
        db.record_workflow_reminder(
            reminder_id="rem_stage1",
            tenant_id="T-STG-1",
            reminder_type="RENT_REMINDER_STAGE_1",
            message_id="msg_stg1",
            phone=t.phone,
            content="Stage 1",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at=datetime(2026, 11, 5, 8, 0, 0).isoformat(),
            stage=1
        )

        # Seed Stage 2 dispatch on Day 6 at 9:00 AM
        db.record_workflow_reminder(
            reminder_id="rem_stage2",
            tenant_id="T-STG-1",
            reminder_type="RENT_REMINDER_STAGE_2_ESCALATION",
            message_id="msg_stg2",
            phone=t.phone,
            content="Stage 2",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at=datetime(2026, 11, 6, 9, 0, 0).isoformat(),
            stage=2
        )

        # Run again on Day 6 at 3:00 PM
        day_6_dt = datetime(2026, 11, 6, 15, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 0)
        self.assertEqual(summary.decisions[0].outcome, DecisionOutcome.SKIP_ALREADY_REMINDED)

    # ── 5. Multi-Tenant Group Escalation Messaging ─────────────────────────

    def test_multi_tenant_group_escalation_greeting_and_dispatch(self):
        """Multi-tenant escalation addresses all unique tenant names in greeting and sends ONE group SMS."""
        t1, rv1 = self._create_test_tenant(
            tenant_id="T-GRP-1",
            first_name="Brian",
            last_name="Fagin",
            phone="+12817022437",
            lease_id="L-GRP-99"
        )
        t2 = Tenant(
            id="T-GRP-2",
            first_name="Shelly",
            last_name="Fagin",
            phone="+12817020294",
            email="shelly@test.com",
            property_name="2603 Bridle Brook Way",
            unit_number="Unit 401",
            lease_id="L-GRP-99"
        )
        db.upsert_tenant(t2)

        # Seed Stage 1 on Day 5
        db.record_workflow_reminder(
            reminder_id="rem_grp_stage1",
            tenant_id="T-GRP-1",
            reminder_type="RENT_REMINDER_STAGE_1",
            message_id="msg_grp_s1",
            phone=t1.phone,
            content="Stage 1",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at=datetime(2026, 11, 5, 8, 0, 0).isoformat(),
            stage=1
        )

        # Day 6 run
        day_6_dt = datetime(2026, 11, 6, 10, 0, 0)
        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_6_dt,
            use_report_source_of_truth=False
        )

        primary_dec = [d for d in summary.decisions if d.tenant_id == "T-GRP-1"][0]
        self.assertEqual(primary_dec.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(primary_dec.details.get("stage"), 2)

        group_sms = primary_dec.details.get("group_sms_text", "")
        # Greeting includes both unique tenant names
        self.assertIn("Hi Brian, Shelly,", group_sms)
        self.assertIn("10-day notice", group_sms)
        self.assertIn("https://redbricksproperties.rentvine.com/portals/resident/make-payment", group_sms)

    # ── 6. Day 7+: Rolling Reminders Use New Escalation Template ─────────────

    def test_day_7_resumes_standard_rolling_reminders(self):
        """Starting on the 7th, resume normal reminder eligibility with 24h rolling cooldown using the new escalation template."""
        t, rv_rec = self._create_test_tenant()

        # Seed confirmed Stage 1 on Day 5
        db.record_workflow_reminder(
            reminder_id="rem_stage1_d5",
            tenant_id="T-STG-1",
            reminder_type="RENT_REMINDER_STAGE_1",
            message_id="msg_stage1_d5",
            phone=t.phone,
            content="Stage 1 reminder",
            sender_phone=settings.QUO_PHONE_TENANTS_FORMATTED,
            delivery_status="DELIVERED",
            sent_at="2026-11-05T10:00:00",
            stage=1
        )

        day_7_dt = datetime(2026, 11, 7, 10, 0, 0)
        stage = self.rules.get_reminder_stage(day_7_dt)
        self.assertEqual(stage, ReminderStage.STAGE_2_ESCALATION.value)

        summary = self.orch.execute_daily_run(
            is_dry_run=True,
            reference_time=day_7_dt,
            use_report_source_of_truth=False
        )

        self.assertEqual(summary.count_reminded, 1)
        dec = summary.decisions[0]
        self.assertEqual(dec.details.get("stage"), 2)
        self.assertEqual(dec.details.get("reminder_type"), "RENT_REMINDER_STAGE_2_ESCALATION")
        # Escalation template used from 6th of every month onward
        self.assertIn("10-day notice", dec.details.get("sms_text", ""))
        self.assertNotIn("The grace period ends today, November 5", dec.details.get("sms_text", ""))


if __name__ == "__main__":
    unittest.main()
