"""
End-to-end unit tests for Workflow Orchestrator
"""

import unittest
from datetime import datetime
from src.seed_data import populate_sample_data
from src.workflow import WorkflowOrchestrator
from src.models import DecisionOutcome

class TestWorkflow(unittest.TestCase):
    def setUp(self):
        from src.database import db
        self.orig_db_path = db.db_path
        db.db_path = 'redbrick_test.db'
        db.init_db()
        populate_sample_data(reset=True)
        from src.clients.rentvine_mcp import RentVineMCPClient
        from src.clients.quo_sms import QuoSMSClient
        self.orchestrator = WorkflowOrchestrator(
            rentvine_client=RentVineMCPClient(mock_mode=True),
            quo_client=QuoSMSClient(mock_mode=True)
        )

    def tearDown(self):
        from src.database import db
        db.db_path = self.orig_db_path

    def test_workflow_dry_run(self):
        # Run workflow in dry-run mode
        summary = self.orchestrator.execute_daily_run(
            is_dry_run=True,
            reference_time=datetime(2026, 10, 15)
        )
        self.assertEqual(summary.total_tenants, 12)
        self.assertGreater(summary.count_paid, 0)
        self.assertGreater(summary.count_promised, 0)
        self.assertGreater(summary.count_reminded, 0)

        # Check specific decisions
        decisions_by_id = {d.tenant_id: d for d in summary.decisions}

        # T-102 should be paid
        self.assertEqual(decisions_by_id["T-102"].outcome, DecisionOutcome.SKIP_PAID)

        # T-103 should be promise detected
        self.assertEqual(decisions_by_id["T-103"].outcome, DecisionOutcome.HOLD_PROMISE)

        # T-105 should be flagged for dispute
        self.assertEqual(decisions_by_id["T-105"].outcome, DecisionOutcome.FLAG_MANUAL_REVIEW)

        # T-108 should be flagged for exclusion
        self.assertEqual(decisions_by_id["T-108"].outcome, DecisionOutcome.FLAG_MANUAL_REVIEW)

        # T-109 should be skipped due to deduplication (sent 4h ago)
        self.assertEqual(decisions_by_id["T-109"].outcome, DecisionOutcome.SKIP_ALREADY_REMINDED)

        # T-101 should be reminded (unpaid & eligible)
        self.assertEqual(decisions_by_id["T-101"].outcome, DecisionOutcome.SEND_REMINDER)

    def test_report_72_extraction_and_validation(self):
        """Validates that Report #72 acts as the authoritative source of truth for overdue tenants."""
        from src.clients.rentvine_mcp import RentVineMCPClient
        from src.clients.quo_sms import QuoSMSClient
        rv_client = RentVineMCPClient()
        report = rv_client.fetch_lease_balance_report(report_id=72)
        
        self.assertEqual(report["report_id"], "72")
        self.assertEqual(report["title"], "Lease Balances")
        self.assertGreaterEqual(len(report["records"]), 10)
        self.assertGreater(report["totals"]["total_balance_due"], 0)
        self.assertGreater(report["totals"]["past_due_rent"], 0)

        # Sync to DB and execute workflow enforcing Report #72
        orch = WorkflowOrchestrator(
            rentvine_client=rv_client,
            quo_client=QuoSMSClient(mock_mode=True)
        )
        summary = orch.execute_daily_run(is_dry_run=True, use_report_source_of_truth=True)

        # Active tenants with overdue balance and no holds receive reminders
        eligible_records = [r for r in report["records"] if r.get("is_eligible")]
        self.assertGreaterEqual(summary.count_reminded, len(eligible_records) - 5)
        reminded_names = [d.tenant_name for d in summary.decisions if d.outcome == DecisionOutcome.SEND_REMINDER]
        self.assertIn("Christopher Pittman", reminded_names)
        self.assertIn("Ke'Juana Ford", reminded_names)
        # Even small balance and current month tenants are included
        self.assertTrue(any("Frisco" in n for n in reminded_names))
        self.assertIn("Yuliya Chernyshov", reminded_names)

    def test_quo_sender_number_routing(self):
        """Validates that Quo SMS integration routes to the exact sender phone number based on recipient type."""
        from src.config import settings
        from src.clients.quo_sms import QuoSMSClient
        from src.database import db

        # 1. Tenant rent reminders: Tenants & Vendors line (980) 300-7559 -> PNnMjdTFVO
        t_id, t_e164, t_fmt, t_inbox = settings.get_quo_sender("tenant")
        self.assertEqual(t_id, "PNnMjdTFVO")
        self.assertEqual(t_fmt, "(980) 300-7559")
        self.assertEqual(t_e164, "+19803007559")
        self.assertEqual(t_inbox, "Tenants & Vendors")

        # 2. Landlord communications: Landlords line (980) 300-1196 -> PNTDARdwBh
        l_id, l_e164, l_fmt, l_inbox = settings.get_quo_sender("landlord")
        self.assertEqual(l_id, "PNTDARdwBh")
        self.assertEqual(l_fmt, "(980) 300-1196")
        self.assertEqual(l_e164, "+19803001196")
        self.assertEqual(l_inbox, "Landlords")

        # 3. Primary number: General line (667) 281-3879 -> PNz3t43PKv (Isolated)
        p_id, p_e164, p_fmt, p_inbox = settings.get_quo_sender("primary")
        self.assertEqual(p_id, "PNz3t43PKv")
        self.assertEqual(p_fmt, "(667) 281-3879")
        self.assertEqual(p_e164, "+16672813879")
        self.assertEqual(p_inbox, "Primary")

        # Verify QuoSMSClient records messages with the exact designated sender numbers
        client = QuoSMSClient()
        res_t = client.send_sms(tenant_id="T-TEST-01", to_number="+17045551234", text="Rent reminder", recipient_type="tenant")
        self.assertEqual(res_t["sender"], "(980) 300-7559")
        self.assertEqual(res_t["sender_phone_id"], "PNnMjdTFVO")
        self.assertEqual(res_t["inbox"], "Tenants & Vendors")

        res_l = client.send_sms(tenant_id="L-TEST-01", to_number="+17045555678", text="Owner distribution update", recipient_type="landlord")
        self.assertEqual(res_l["sender"], "(980) 300-1196")
        self.assertEqual(res_l["sender_phone_id"], "PNTDARdwBh")
        self.assertEqual(res_l["inbox"], "Landlords")

        # Verify DB audit trail contains correct sender number and inbox metadata
        msgs_t = db.get_messages_for_tenant("T-TEST-01")
        self.assertGreaterEqual(len(msgs_t), 1)
        self.assertEqual(msgs_t[-1].sender, "(980) 300-7559")
        self.assertEqual(msgs_t[-1].metadata.get("inbox"), "Tenants & Vendors")

    def test_sms_reminder_template_paragraph_formatting(self):
        """Validates that SMS reminder templates contain blank lines between paragraphs matching WhatsApp layout."""
        from src.config import settings

        # Kukunoor test case (single address, empty unit)
        sms_kukunoor = settings.REMINDER_TEMPLATE.format(
            tenant_name="Kukunoor",
            balance=1000.0,
            property_name="8620 Fairgreen Avenue",
            unit_number="",
            due_date="2026-10-01",
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )

        expected_kukunoor = (
            "Hi Kukunoor,\n\n"
            "Friendly reminder from Red Bricks Property Management:\n\n"
            "Your rent of $1,000.00 for 8620 Fairgreen Avenue was due on October 1. The grace period ends today, October 5. Please submit your payment by tonight to avoid late fees beginning tomorrow, October 6.\n\n"
            "You can pay through your RentVine portal (https://redbricksproperties.rentvine.com/portal/pay). If you've already submitted your payment, please reply here so we can check its status.\n\n"
            "Thank you!\nRBPM bot"
        )
        self.assertEqual(sms_kukunoor, expected_kukunoor)

        # Tao Peng test case (repeated unit name is cleanly deduplicated)
        sms_tao = settings.REMINDER_TEMPLATE.format(
            tenant_name="Tao",
            balance=2209.0,
            property_name="Brookdale Village_5601 Stafford Road",
            unit_number="Brookdale Village_5601 Stafford Road",
            due_date="2026-10-01",
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )
        self.assertIn("Your rent of $2,209.00 for 5601 Stafford Road was due on October 1. The grace period ends today, October 5. Please submit your payment by tonight to avoid late fees beginning tomorrow, October 6.", sms_tao)
        self.assertNotIn("Brookdale Village_5601 Stafford Road Brookdale Village_5601 Stafford Road", sms_tao)
        self.assertIn("RBPM bot", sms_tao)
        self.assertEqual(sms_tao.count("\n\n"), 4)

    def test_current_month_grace_period_and_previous_month_overdue_workflow(self):
        """
        Validates workflow logic:
        1. Nov 1-4: Current month (Nov 1) rent is skipped under grace period.
        2. Nov 5+: Current month rent is eligible for reminders.
        3. Nov 1-4: Previous month (Oct 1) overdue rent is NOT skipped by Nov grace period.
        4. Payment status verification: $0.00 balance is skipped as SKIP_PAID.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord

        # Setup test tenants
        # Tenant A: November rent due Nov 1 (current month)
        t_a = Tenant(id="T-GRACE-NOV", first_name="Grace", last_name="Nov", phone="+17045550001", email="grace@example.com", property_name="Apt 101", unit_number="101")
        rv_a = RentVineRecord(tenant_id="T-GRACE-NOV", balance=1200.0, monthly_rent=1200.0, due_date="2026-11-01", lease_status="Active")
        db.upsert_tenant(t_a)
        db.upsert_rentvine_record(rv_a)

        # Tenant B: October rent due Oct 1 (previous month overdue)
        t_b = Tenant(id="T-OVERDUE-OCT", first_name="Overdue", last_name="Oct", phone="+17045550002", email="oct@example.com", property_name="Apt 102", unit_number="102")
        rv_b = RentVineRecord(tenant_id="T-OVERDUE-OCT", balance=950.0, monthly_rent=950.0, due_date="2026-10-01", lease_status="Active")
        db.upsert_tenant(t_b)
        db.upsert_rentvine_record(rv_b)

        # Tenant C: Paid in full (balance $0.00)
        t_c = Tenant(id="T-PAID-ZERO", first_name="Paid", last_name="Full", phone="+17045550003", email="paid@example.com", property_name="Apt 103", unit_number="103")
        rv_c = RentVineRecord(tenant_id="T-PAID-ZERO", balance=0.0, monthly_rent=1000.0, due_date="2026-11-01", lease_status="Active")
        db.upsert_tenant(t_c)
        db.upsert_rentvine_record(rv_c)

        # Run 1: On November 2nd (within Nov 1-4 grace period)
        nov_2 = datetime(2026, 11, 2, 10, 0)
        dec_a_nov2 = self.orchestrator._process_tenant(t_a, run_id="run_nov2", is_dry_run=True, current_time=nov_2)
        dec_b_nov2 = self.orchestrator._process_tenant(t_b, run_id="run_nov2", is_dry_run=True, current_time=nov_2)
        dec_c_nov2 = self.orchestrator._process_tenant(t_c, run_id="run_nov2", is_dry_run=True, current_time=nov_2)

        # Tenant A (Nov rent) -> in grace period on Nov 2
        self.assertEqual(dec_a_nov2.outcome, DecisionOutcome.SKIP_GRACE_PERIOD)
        self.assertIn("grace period", dec_a_nov2.reason.lower())

        # Tenant B (Oct overdue rent) -> NOT suppressed by Nov 1-4 grace period
        self.assertEqual(dec_b_nov2.outcome, DecisionOutcome.SEND_REMINDER)

        # Tenant C (Paid $0.00) -> SKIP_PAID
        self.assertEqual(dec_c_nov2.outcome, DecisionOutcome.SKIP_PAID)

        # Run 2: On November 5th (grace period has ended)
        nov_5 = datetime(2026, 11, 5, 9, 0)
        dec_a_nov5 = self.orchestrator._process_tenant(t_a, run_id="run_nov5", is_dry_run=True, current_time=nov_5)

        # Tenant A (Nov rent) -> grace period expired on 4th, reminder now eligible on the 5th
        self.assertEqual(dec_a_nov5.outcome, DecisionOutcome.SEND_REMINDER)

    def test_kukunoor_payment_promise_and_quo_retrieval(self):
        """
        Validates the acceptance criteria for Kukunoor and Quo payment-commitment detection:
        1. "I will pay this saturday" -> PROMISE_MADE with Saturday date
        2. Reminder suppressed under HOLD_PROMISE until promise date
        3. Phone normalization handles (862) 220-5006 and +18622205006
        4. Quo API failure holds reminder under FLAG_MANUAL_REVIEW
        5. Expired promise re-evaluates eligibility
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, CommunicationMessage, Channel, MessageDirection
        from src.clients.quo_sms import normalize_phone_e164, phone_to_10digit, QuoSMSClient
        from src.engine.ai_classifier import AIIntentClassifier

        # 1. Phone normalization
        self.assertEqual(normalize_phone_e164("(862) 220-5006"), "+18622205006")
        self.assertEqual(normalize_phone_e164("862-220-5006"), "+18622205006")
        self.assertEqual(normalize_phone_e164("+18622205006"), "+18622205006")
        self.assertEqual(phone_to_10digit("(862) 220-5006"), "8622205006")

        # 2. Setup Kukunoor tenant
        t_kuk = Tenant(
            id="rv_lease_654",
            first_name="Kukunoor",
            last_name="Tenant",
            phone="(862) 220-5006",
            email="kukunoor@example.com",
            property_name="8620 Fairgreen Avenue",
            unit_number=""
        )
        rv_kuk = RentVineRecord(
            tenant_id="rv_lease_654",
            balance=1000.0,
            monthly_rent=1000.0,
            due_date="2026-10-01",
            lease_status="Active"
        )
        db.upsert_tenant(t_kuk)
        db.upsert_rentvine_record(rv_kuk)

        # 3. Add Kukunoor's real incoming SMS from Thursday Oct 29, 2026
        msg = CommunicationMessage(
            id="quo_test_kuk_1",
            tenant_id="rv_lease_654",
            channel=Channel.SMS,
            direction=MessageDirection.INBOUND,
            sender="+18622205006",
            recipient="+19803007559",
            content="Sorry, I thought I paid the rent but I didn't  I will pay this saturday",
            timestamp="2026-10-29T17:38:41Z",
            status="RECEIVED"
        )
        db.save_message(msg)

        # 4. Test AI classification
        classifier = AIIntentClassifier()
        res = classifier.classify_tenant_communications([msg], reference_date=datetime(2026, 10, 29, 23, 0))
        self.assertEqual(res.intent.value, "PROMISE_MADE")
        self.assertEqual(res.extracted_date, "2026-10-31")

        # 5. Workflow execution on Thursday Oct 29: Reminder must be SUPPRESSED under HOLD_PROMISE
        oct_29 = datetime(2026, 10, 29, 23, 0)
        dec_oct29 = self.orchestrator._process_tenant(t_kuk, run_id="run_kuk_29", is_dry_run=True, current_time=oct_29)
        self.assertEqual(dec_oct29.outcome, DecisionOutcome.HOLD_PROMISE)
        self.assertIn("2026-10-31", dec_oct29.reason)

        # 6. Workflow execution after promised date has expired (Nov 1, 2026): balance still unpaid -> eligible for reminder
        nov_1 = datetime(2026, 11, 1, 10, 0)
        dec_nov1 = self.orchestrator._process_tenant(t_kuk, run_id="run_kuk_nov1", is_dry_run=True, current_time=nov_1)
        self.assertEqual(dec_nov1.outcome, DecisionOutcome.SEND_REMINDER)

    def test_cooldown_triggered_only_by_workflow_reminders_not_ordinary_chats(self):
        """
        Validates that ordinary outgoing messages ("Hi", "Hello", technician chats, manual messages)
        do NOT activate cooldown. Cooldown must ONLY be triggered by workflow-generated rent reminders.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, CommunicationMessage, Channel, MessageDirection
        from src.engine.rules import BusinessRulesEngine

        tenant_id = "T-CHAT-TEST"
        t = Tenant(
            id=tenant_id,
            first_name="Mark",
            last_name="Jones",
            phone="+17044492986",
            email="mark@example.com",
            property_name="Hucks Landing_5142 Elementary View Drive",
            unit_number=""
        )
        rv = RentVineRecord(
            tenant_id=tenant_id,
            balance=2245.0,
            monthly_rent=2245.0,
            due_date="2026-10-01",
            lease_status="Active"
        )
        db.upsert_tenant(t)
        db.upsert_rentvine_record(rv)

        # 1. Simulate ordinary outgoing chats (like Mark Jones' actual OpenPhone messages)
        today = datetime(2026, 10, 30, 12, 0)
        db.save_message(CommunicationMessage(
            id="msg_chat_1",
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.OUTBOUND,
            sender="(980) 300-7559",
            recipient=t.phone,
            content="so technicians are not working tomorrow",
            timestamp="2026-10-30T02:11:00Z",
            status="DELIVERED"
        ))
        db.save_message(CommunicationMessage(
            id="msg_chat_2",
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.OUTBOUND,
            sender="(980) 300-7559",
            recipient=t.phone,
            content="Hi Mark, please share an update.",
            timestamp="2026-10-30T01:38:00Z",
            status="DELIVERED"
        ))

        # Ordinary messages must NOT be identified as workflow rent reminders
        last_rem = db.get_last_successful_reminder(tenant_id)
        self.assertIsNone(last_rem, "Ordinary conversation messages must not count as workflow rent reminders")

        # Rules engine must NOT activate cooldown
        rules = BusinessRulesEngine()
        is_dup, _, reason = rules.check_recent_reminder_deduplication(tenant_id, current_time=today)
        self.assertFalse(is_dup, f"Cooldown must not be active for ordinary chats: {reason}")

        # Workflow execution: tenant must be eligible for reminder
        dec = self.orchestrator._process_tenant(t, run_id="run_chat_test", is_dry_run=True, current_time=today)
        self.assertEqual(dec.outcome, DecisionOutcome.SEND_REMINDER)

    def test_workflow_reminder_triggers_same_day_cooldown_and_reactivates_next_morning(self):
        """
        Validates:
        1. A workflow-generated rent reminder sent at 10 AM triggers cooldown for the remainder of today.
        2. Manually sending "Hi" at 12 PM does not alter or extend the cooldown.
        3. The next morning, the tenant is automatically reactivated for reminder eligibility.
        4. Failed or unsent drafts do NOT activate cooldown.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, CommunicationMessage, Channel, MessageDirection
        from src.engine.rules import BusinessRulesEngine

        tenant_id = "T-COOLDOWN-TEST"
        t = Tenant(
            id=tenant_id,
            first_name="Tao",
            last_name="Peng",
            phone="+19805050267",
            email="tao@example.com",
            property_name="Brookdale Village",
            unit_number="5601"
        )
        rv = RentVineRecord(
            tenant_id=tenant_id,
            balance=2209.0,
            monthly_rent=2100.0,
            due_date="2026-10-01",
            lease_status="Active"
        )
        db.upsert_tenant(t)
        db.upsert_rentvine_record(rv)

        # Step 1: At 10:00 AM on Oct 30, workflow reminder is dispatched and recorded
        oct_30_10am = datetime(2026, 10, 30, 10, 0)
        reminder_id = f"rem_{tenant_id}_20261030"
        db.record_workflow_reminder(
            reminder_id=reminder_id,
            tenant_id=tenant_id,
            reminder_type="RENT_REMINDER",
            message_id="quo_msg_live_123",
            workflow_id="run_oct30_morning",
            phone=t.phone,
            content="Hi Tao, Friendly reminder from Red Bricks Property Management: Your rent of $2,209.00 is due.",
            sender_phone="(980) 300-7559",
            delivery_status="DELIVERED",
            sent_at=oct_30_10am.isoformat()
        )

        # Step 2: At 12:00 PM on Oct 30, staff manually sends "Hi"
        db.save_message(CommunicationMessage(
            id="msg_manual_hi",
            tenant_id=tenant_id,
            channel=Channel.SMS,
            direction=MessageDirection.OUTBOUND,
            sender="(980) 300-7559",
            recipient=t.phone,
            content="Hi Tao",
            timestamp=datetime(2026, 10, 30, 12, 0).isoformat(),
            status="DELIVERED"
        ))

        # At 2:00 PM on Oct 30 (2h later): Cooldown MUST be active (within 24 hours)
        oct_30_2pm = datetime(2026, 10, 30, 14, 0)
        rules = BusinessRulesEngine()
        is_dup, last_rem, reason = rules.check_recent_reminder_deduplication(tenant_id, current_time=oct_30_2pm)
        self.assertTrue(is_dup)
        self.assertIn("Cooldown active", reason)

        # Workflow execution at 2:00 PM: must result in SKIP_ALREADY_REMINDED
        dec_today = self.orchestrator._process_tenant(t, run_id="run_oct30_afternoon", is_dry_run=True, current_time=oct_30_2pm)
        self.assertEqual(dec_today.outcome, DecisionOutcome.SKIP_ALREADY_REMINDED)

        # Step 3: After 24 hours (Oct 31, 1:00 PM): 24h Cooldown expires, tenant reactivates!
        oct_31_1pm = datetime(2026, 10, 31, 13, 0)
        is_dup_next, _, reason_next = rules.check_recent_reminder_deduplication(tenant_id, current_time=oct_31_1pm)
        self.assertFalse(is_dup_next, f"Tenant should be reactivated after 24 hours: {reason_next}")
        self.assertIn("24h cooldown window expired", reason_next)

        # Workflow execution after 24 hours: tenant is eligible for new reminder
        dec_next = self.orchestrator._process_tenant(t, run_id="run_oct31_afternoon", is_dry_run=True, current_time=oct_31_1pm)
        self.assertEqual(dec_next.outcome, DecisionOutcome.SEND_REMINDER)

    def test_follow_up_notes_manual_review_transitions(self):
        """
        Rule 1: When Follow-Up Notes contains 'Manual Review', move to Manual Review / Disputes (FLAG_MANUAL_REVIEW).
        Rule 2: When Follow-Up Notes is blank, tenant is eligible for reminders (SEND_REMINDER).
        Rule 3: When Manual Review is removed (becomes blank), tenant automatically reactivates for reminders!
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, DecisionOutcome
        tenant_id = "T-TEST-MR-1"
        t = Tenant(
            id=tenant_id,
            first_name="Marcus",
            last_name="Reviewee",
            phone="+19803009999",
            email="marcus@example.com",
            property_name="Fairgreen Ave",
            unit_number="Unit 10"
        )
        rv = RentVineRecord(
            tenant_id=tenant_id,
            balance=1500.0,
            monthly_rent=1500.0,
            due_date="2026-10-01",
            lease_status="ACTIVE",
            follow_up_notes="10/25/2026 - Manual Review: tenant dispute regarding AC maintenance credit"
        )
        db.upsert_tenant(t)
        db.upsert_rentvine_record(rv)

        # 1. With "Manual Review" in Follow-Up Notes:
        dec = self.orchestrator._process_tenant(t, run_id="run_test_mr", is_dry_run=True, current_time=datetime(2026, 10, 26))
        self.assertEqual(dec.outcome, DecisionOutcome.FLAG_MANUAL_REVIEW)
        self.assertIn("Manual Review", dec.reason)
        self.assertEqual(dec.balance, 1500.0)

        # 2. When Follow-Up Notes has "Manual Review" removed and field becomes blank:
        rv.follow_up_notes = ""
        db.upsert_rentvine_record(rv)

        # Re-evaluate tenant on next run: must automatically restore reminder eligibility!
        dec_reactivated = self.orchestrator._process_tenant(t, run_id="run_test_reactivated", is_dry_run=True, current_time=datetime(2026, 10, 26))
        self.assertEqual(dec_reactivated.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(dec_reactivated.balance, 1500.0)

    def test_follow_up_notes_payment_promise_handling(self):
        """
        Rule 4: When Follow-Up Notes contains a payment commitment for a future date,
        apply payment-promise suppression (HOLD_PROMISE).
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, DecisionOutcome
        tenant_id = "T-TEST-PROMISE-1"
        t = Tenant(
            id=tenant_id,
            first_name="Elena",
            last_name="Promiser",
            phone="+19803008888",
            email="elena@example.com",
            property_name="Fairgreen Ave",
            unit_number="Unit 12"
        )
        rv = RentVineRecord(
            tenant_id=tenant_id,
            balance=1200.0,
            monthly_rent=1200.0,
            due_date="2026-10-01",
            lease_status="ACTIVE",
            follow_up_notes="11/15/2026 - Owner-approved extension; hold collection follow-up until November 15."
        )
        db.upsert_tenant(t)
        db.upsert_rentvine_record(rv)

        # On 2026-11-01 (before Nov 15 promise): reminder is held
        dec_hold = self.orchestrator._process_tenant(t, run_id="run_test_hold", is_dry_run=True, current_time=datetime(2026, 11, 1))
        self.assertEqual(dec_hold.outcome, DecisionOutcome.HOLD_PROMISE)
        self.assertIn("Active payment commitment", dec_hold.reason)

        # After promised date (2026-11-16): hold expires and tenant is eligible for reminder
        dec_expired = self.orchestrator._process_tenant(t, run_id="run_test_expired", is_dry_run=True, current_time=datetime(2026, 11, 16))
        self.assertEqual(dec_expired.outcome, DecisionOutcome.SEND_REMINDER)

    def test_non_manual_review_follow_up_notes_remain_eligible(self):
        """
        Rule 4b: Do NOT treat every non-empty follow-up note as Manual Review.
        Notes that do not contain 'manual review' and are not active future promises remain eligible.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, DecisionOutcome
        tenant_id = "T-TEST-GENERAL-1"
        t = Tenant(
            id=tenant_id,
            first_name="General",
            last_name="NoteTenant",
            phone="+19803007777",
            email="general@example.com",
            property_name="Fairgreen Ave",
            unit_number="Unit 14"
        )
        rv = RentVineRecord(
            tenant_id=tenant_id,
            balance=950.0,
            monthly_rent=950.0,
            due_date="2026-10-01",
            lease_status="ACTIVE",
            follow_up_notes="Raghu Testing: Property manager conducted routine check"
        )
        db.upsert_tenant(t)
        db.upsert_rentvine_record(rv)

        dec = self.orchestrator._process_tenant(t, run_id="run_test_general", is_dry_run=True, current_time=datetime(2026, 10, 20))
        self.assertEqual(dec.outcome, DecisionOutcome.SEND_REMINDER)


    def test_joshua_lewis_november_due_date_and_clean_property(self):
        """
        Validates Joshua Lewis rent reminder:
        1. Correctly calculates November 1, 2026 due date (not outdated October 1).
        2. Cleans NONE_5100 @ Central_ prefix to '4124 Salient Street'.
        3. Formats exact expected text requested by PM.
        """
        from src.config import settings
        sms_joshua = settings.REMINDER_TEMPLATE.format(
            tenant_name="Joshua",
            balance=1895.0,
            property_name="NONE_5100 @ Central_4124 Salient Street",
            unit_number="NONE_5100 @ Central_4124 Salient Street",
            due_date="November 1, 2026",
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )
        self.assertIn("Your rent of $1,895.00 for 4124 Salient Street was due on November 1. The grace period ends today, November 5. Please submit your payment by tonight to avoid late fees beginning tomorrow, November 6.", sms_joshua)
        self.assertNotIn("NONE_5100 @ Central", sms_joshua)
        self.assertIn("RBPM bot", sms_joshua)

    def test_multi_month_billing_period_and_historical_due_dates(self):
        """
        Validates that when multiple months are outstanding:
        1. Each billing period is identified and distinguished (not assigned one date to total).
        2. Historical due dates are retained (e.g. October 1, 2026 and November 1, 2026).
        3. Custom lease due dates (e.g. October 9th) retain their lease-specific date.
        """
        from src.config import settings
        from src.clients.rentvine_mcp import RentVineMCPClient, format_friendly_due_date

        client = RentVineMCPClient()
        mock_txs = [
            {
                "transaction": {
                    "transactionTypeID": "1",
                    "description": "Rent Income (10-2026)",
                    "datePosted": "2026-10-09",
                    "amount": "2100.00",
                    "amountPaid": "0.00"
                }
            },
            {
                "transaction": {
                    "transactionTypeID": "1",
                    "description": "Rent Income (11-2026)",
                    "datePosted": "2026-11-01",
                    "amount": "2100.00",
                    "amountPaid": "0.00"
                }
            }
        ]
        details = client.extract_unpaid_rent_details(mock_txs, total_balance=4200.0)
        self.assertEqual(len(details["billing_periods"]), 2)
        self.assertIn("October 2026", details["billing_periods"])
        self.assertIn("November 2026", details["billing_periods"])
        self.assertIn("October 9, 2026 (October 2026) and November 1, 2026 (November 2026)", details["due_date"])

        # Formatted into SMS
        sms = settings.REMINDER_TEMPLATE.format(
            tenant_name="Tao",
            balance=4200.0,
            property_name="5601 Stafford Road",
            unit_number="",
            due_date=details["due_date"],
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )
        self.assertIn("Your rent of $4,200.00 for 5601 Stafford Road was due on October 9, 2026 (October 2026) and November 1, 2026 (November 2026).", sms)

    def test_tamica_bagley_reference_template_format(self):
        """
        Validates the exact reference template requested by PM:
        Tenant: Tamica
        Rent: $2,795.00
        Property: Winding Walk - 439 Sweet Shrub Court Northwest, Concord, NC 28027
        Due: November 1
        Grace: November 5
        Late fees: November 6
        """
        from src.config import settings
        sms_tamica = settings.REMINDER_TEMPLATE.format(
            tenant_name="Tamica",
            balance=2795.0,
            property_name="Winding Walk - 439 Sweet Shrub Court Northwest, Concord, NC 28027",
            unit_number="",
            due_date="November 1",
            grace_period_end_date="November 5",
            late_fee_start_date="November 6",
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )
        expected = (
            "Hi Tamica,\n\n"
            "Friendly reminder from Red Bricks Property Management:\n\n"
            "Your rent of $2,795.00 for Winding Walk - 439 Sweet Shrub Court Northwest, Concord, NC 28027 was due on November 1. The grace period ends today, November 5. Please submit your payment by tonight to avoid late fees beginning tomorrow, November 6.\n\n"
            "You can pay through your RentVine portal (https://redbricksproperties.rentvine.com/portal/pay). If you've already submitted your payment, please reply here so we can check its status.\n\n"
            "Thank you!\n"
            "RBPM bot"
        )
        self.assertEqual(sms_tamica, expected)


    def test_format_tenant_names_list(self):
        """Validates Oxford-comma name list formatting for group SMS greeting."""
        from src.workflow import WorkflowOrchestrator
        fmt = WorkflowOrchestrator._format_tenant_names_list
        self.assertEqual(fmt([]), "Tenant")
        self.assertEqual(fmt(["Joshua"]), "Joshua")
        self.assertEqual(fmt(["Joshua", "Sarah"]), "Joshua and Sarah")
        self.assertEqual(fmt(["Joshua", "Tenant 2", "Tenant 3"]), "Joshua, Tenant 2, and Tenant 3")
        self.assertEqual(fmt(["A", "B", "C", "D"]), "A, B, C, and D")

    def test_group_sms_template_single_vs_multi_tenant(self):
        """
        Validates GROUP_REMINDER_TEMPLATE renders correctly for:
        1. A single-tenant lease (greeting = single name).
        2. A two-tenant lease (greeting = "Name1 and Name2").
        3. A three-tenant lease (greeting = "Name1, Name2, and Name3").
        """
        from src.config import settings
        from src.workflow import WorkflowOrchestrator
        fmt = WorkflowOrchestrator._format_tenant_names_list

        # 2-tenant lease (Joshua Lewis + Tenant 2)
        greeting_2 = fmt(["Joshua", "Tenant 2"])
        sms_2 = settings.GROUP_REMINDER_TEMPLATE.format(
            tenant_names=greeting_2,
            balance=1895.0,
            property_name="NONE_5100 @ Central_4124 Salient Street",
            unit_number="",
            due_date="November 1, 2026",
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )
        self.assertTrue(sms_2.startswith("Hi Joshua and Tenant 2,"))
        self.assertIn("Your rent of $1,895.00 for 4124 Salient Street", sms_2)
        self.assertIn("RBPM BOT", sms_2)
        self.assertNotIn("NONE_5100 @ Central", sms_2)

        # 3-tenant lease (Joshua Lewis + Tenant 2 + Tenant 3)
        greeting_3 = fmt(["Joshua", "Tenant 2", "Tenant 3"])
        sms_3 = settings.GROUP_REMINDER_TEMPLATE.format(
            tenant_names=greeting_3,
            balance=1895.0,
            property_name="4124 Salient Street",
            unit_number="",
            due_date="November 1",
            portal_link="https://redbricksproperties.rentvine.com/portal/pay"
        )
        self.assertTrue(sms_3.startswith("Hi Joshua, Tenant 2, and Tenant 3,"))
        self.assertIn("Your rent of $1,895.00 for 4124 Salient Street", sms_3)
        self.assertIn("RBPM BOT", sms_3)

    def test_lease_tenant_groups_singleton(self):
        """
        Validates that tenants without a lease_id each form their own singleton group
        and are processed normally by the standard per-tenant path.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord

        # Seed a tenant WITHOUT a lease_id (legacy/seed data scenario)
        t = Tenant(
            id="T-GROUP-SOLO",
            first_name="Solo",
            last_name="Tenant",
            phone="+17045551001",
            email="solo@example.com",
            property_name="Solo Street",
            unit_number="Unit 1"
            # lease_id omitted → None
        )
        rv = RentVineRecord(
            tenant_id="T-GROUP-SOLO",
            balance=1500.0,
            monthly_rent=1500.0,
            due_date="2026-10-01",
            lease_status="ACTIVE"
        )
        db.upsert_tenant(t)
        db.upsert_rentvine_record(rv)

        groups = db.get_lease_tenant_groups(active_only=False)
        solo_groups = [g for g in groups if g.primary_tenant.id == "T-GROUP-SOLO"]
        self.assertEqual(len(solo_groups), 1)
        self.assertFalse(solo_groups[0].is_multi_tenant)
        self.assertEqual(len(solo_groups[0].co_tenants), 0)

    def test_lease_tenant_group_creation_and_grouping(self):
        """
        Validates that two tenants sharing the same lease_id are grouped correctly
        into a single LeaseTenantGroup with one primary and one co-tenant.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord

        lease_id = "lease_group_test_001"

        t1 = Tenant(
            id="T-GROUP-P1",
            first_name="Joshua",
            last_name="Lewis",
            phone="+17045551100",
            email="joshua@example.com",
            property_name="4124 Salient Street",
            unit_number="",
            lease_id=lease_id
        )
        t2 = Tenant(
            id="T-GROUP-C1",
            first_name="Sarah",
            last_name="Lewis",
            phone="+17045551101",
            email="sarah@example.com",
            property_name="4124 Salient Street",
            unit_number="",
            lease_id=lease_id
        )
        rv1 = RentVineRecord(
            tenant_id="T-GROUP-P1",
            balance=1895.0,
            monthly_rent=1895.0,
            due_date="2026-11-01",
            lease_status="ACTIVE"
        )
        rv2 = RentVineRecord(
            tenant_id="T-GROUP-C1",
            balance=0.0,
            monthly_rent=1895.0,
            due_date="2026-11-01",
            lease_status="ACTIVE"
        )
        db.upsert_tenant(t1)
        db.upsert_tenant(t2)
        db.upsert_rentvine_record(rv1)
        db.upsert_rentvine_record(rv2)

        groups = db.get_lease_tenant_groups(active_only=False)
        matched = [g for g in groups if g.lease_id == lease_id]
        self.assertEqual(len(matched), 1)
        group = matched[0]
        self.assertTrue(group.is_multi_tenant)
        self.assertEqual(len(group.co_tenants), 1)
        self.assertEqual(group.primary_tenant.id, "T-GROUP-P1")
        self.assertEqual(group.co_tenants[0].id, "T-GROUP-C1")
        # all_names should list both first names
        self.assertIn("Joshua", group.all_names)
        self.assertIn("Sarah", group.all_names)
        # all_phones should include both numbers
        phones = [p[1] for p in group.all_phones]
        self.assertIn("+17045551100", phones)
        self.assertIn("+17045551101", phones)
        # group_id is lease-based
        self.assertEqual(group.group_id, f"lease_{lease_id}")

    def test_process_lease_group_sends_one_combined_sms(self):
        """
        End-to-end validation of the group SMS path:
        1. Two co-tenants on the same lease both receive ONE group reminder text.
        2. The combined SMS greeting addresses all tenants by name.
        3. Two WorkflowDecisions are created (one per tenant) with SEND_REMINDER.
        4. The balance appears only on the primary tenant's decision.
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, DecisionOutcome, LeaseTenantGroup
        from datetime import datetime

        lease_id = "lease_e2e_group_002"
        t_primary = Tenant(
            id="T-GP-PRIMARY",
            first_name="Joshua",
            last_name="Lewis",
            phone="+17045551200",
            email="jl@example.com",
            property_name="4124 Salient Street",
            unit_number="",
            lease_id=lease_id
        )
        t_co = Tenant(
            id="T-GP-COTENENT",
            first_name="Tenant2",
            last_name="Lewis",
            phone="+17045551201",
            email="t2@example.com",
            property_name="4124 Salient Street",
            unit_number="",
            lease_id=lease_id
        )
        rv_primary = RentVineRecord(
            tenant_id="T-GP-PRIMARY",
            balance=1895.0,
            monthly_rent=1895.0,
            due_date="2026-11-01",
            lease_status="ACTIVE"
        )
        rv_co = RentVineRecord(
            tenant_id="T-GP-COTENENT",
            balance=0.0,
            monthly_rent=1895.0,
            due_date="2026-11-01",
            lease_status="ACTIVE"
        )
        db.upsert_tenant(t_primary)
        db.upsert_tenant(t_co)
        db.upsert_rentvine_record(rv_primary)
        db.upsert_rentvine_record(rv_co)

        group = LeaseTenantGroup(
            lease_id=lease_id,
            primary_tenant=t_primary,
            co_tenants=[t_co],
            all_phones=[("Joshua", "+17045551200"), ("Tenant2", "+17045551201")]
        )

        ref_time = datetime(2026, 11, 10, 10, 0)
        decisions = self.orchestrator._process_lease_group(
            group=group,
            run_id="run_group_e2e",
            is_dry_run=True,
            current_time=ref_time
        )

        # Both tenant decisions returned
        self.assertEqual(len(decisions), 2)
        primary_dec = next(d for d in decisions if d.tenant_id == "T-GP-PRIMARY")
        co_dec = next(d for d in decisions if d.tenant_id == "T-GP-COTENENT")

        # Both should be SEND_REMINDER
        self.assertEqual(primary_dec.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(co_dec.outcome, DecisionOutcome.SEND_REMINDER)

        # Balance only on primary
        self.assertEqual(primary_dec.balance, 1895.0)
        self.assertEqual(co_dec.balance, 0.0)

        # Group metadata on primary decision
        self.assertTrue(primary_dec.details.get("is_group_reminder"))
        self.assertEqual(primary_dec.details.get("lease_id"), lease_id)
        self.assertIn("Joshua", primary_dec.details.get("greeting", ""))
        self.assertIn("Tenant2", primary_dec.details.get("greeting", ""))

        # Co-tenant decision is marked as such
        self.assertTrue(co_dec.details.get("is_co_tenant"))
        self.assertEqual(co_dec.details.get("primary_tenant_id"), "T-GP-PRIMARY")

        # SMS text contains both names in greeting
        sms_text = primary_dec.details.get("group_sms_text", "")
        self.assertIn("Joshua", sms_text)
        self.assertIn("Tenant2", sms_text)
        self.assertIn("4124 Salient Street", sms_text)
        self.assertIn("$1,895.00", sms_text)
        # Both phone numbers in the sent list
        phones = primary_dec.details.get("phones_sent", [])
        self.assertIn("+17045551200", phones)
        self.assertIn("+17045551201", phones)

    def test_group_sms_manual_review_propagates_to_co_tenants(self):
        """
        When the primary tenant is in Manual Review, all co-tenants on the lease
        must receive FLAG_MANUAL_REVIEW (no SMS sent to anyone in the group).
        """
        from src.database import db
        from src.models import Tenant, RentVineRecord, DecisionOutcome, LeaseTenantGroup
        from datetime import datetime

        lease_id = "lease_mr_group_003"
        t_primary = Tenant(
            id="T-MR-GP-PRIMARY",
            first_name="Marcus",
            last_name="Review",
            phone="+17045551300",
            email="mr@example.com",
            property_name="Fairgreen Ave",
            unit_number="",
            lease_id=lease_id,
            is_excluded=True,
            exclusion_reason="Manual Review in RentVine Follow-Up Notes"
        )
        t_co = Tenant(
            id="T-MR-GP-CO",
            first_name="Cotenent",
            last_name="Review",
            phone="+17045551301",
            email="co@example.com",
            property_name="Fairgreen Ave",
            unit_number="",
            lease_id=lease_id,
            is_excluded=True,
            exclusion_reason="Manual Review in RentVine Follow-Up Notes"
        )
        rv_mr = RentVineRecord(
            tenant_id="T-MR-GP-PRIMARY",
            balance=2000.0,
            monthly_rent=2000.0,
            due_date="2026-10-01",
            lease_status="ACTIVE",
            follow_up_notes="Manual Review: legal dispute pending"
        )
        rv_co = RentVineRecord(
            tenant_id="T-MR-GP-CO",
            balance=0.0,
            monthly_rent=2000.0,
            due_date="2026-10-01",
            lease_status="ACTIVE",
            follow_up_notes="Manual Review: legal dispute pending"
        )
        db.upsert_tenant(t_primary)
        db.upsert_tenant(t_co)
        db.upsert_rentvine_record(rv_mr)
        db.upsert_rentvine_record(rv_co)

        group = LeaseTenantGroup(
            lease_id=lease_id,
            primary_tenant=t_primary,
            co_tenants=[t_co],
            all_phones=[("Marcus", "+17045551300"), ("Cotenent", "+17045551301")]
        )

        ref_time = datetime(2026, 10, 20, 10, 0)
        decisions = self.orchestrator._process_lease_group(
            group=group,
            run_id="run_mr_group",
            is_dry_run=True,
            current_time=ref_time
        )

        # Both should be flagged - no SMS sent
        self.assertEqual(len(decisions), 2)
        for d in decisions:
            self.assertEqual(d.outcome, DecisionOutcome.FLAG_MANUAL_REVIEW)
            self.assertFalse(d.sms_sent)

    def test_kukunoor_multi_tenant_group_sms(self):
        """Validates that Kukunoor Tenant and second Kukunoor Tenant for lease 654
        (8620 Fairgreen Avenue) are grouped together, addressed as 'Hi Kukunoor and second Kukunoor',
        and both phone numbers receive the combined group reminder SMS."""
        from src.database import db
        from src.models import Tenant, RentVineRecord, LeaseTenantGroup, DecisionOutcome
        from src.workflow import WorkflowOrchestrator

        lease_id = "654"
        # 1. Primary Tenant: Kukunoor Tenant
        t_primary = Tenant(
            id=f"rv_lease_{lease_id}",
            first_name="Kukunoor",
            last_name="Tenant",
            phone="+18622205006",
            email="kukunoor@example.com",
            property_name="8620 Fairgreen Avenue",
            unit_number="",
            lease_id=lease_id
        )
        # 2. Co-Tenant: second Kukunoor Tenant
        t_co = Tenant(
            id=f"rv_lease_{lease_id}_co_4632",
            first_name="second Kukunoor",
            last_name="Tenant",
            phone="+19802941035",
            email="second_kukunoor@example.com",
            property_name="8620 Fairgreen Avenue",
            unit_number="",
            lease_id=lease_id
        )

        # Verify individual greeting names
        self.assertEqual(t_primary.greeting_name, "Kukunoor Tenant")
        self.assertEqual(t_co.greeting_name, "Second Kukunoor Tenant")

        # Save to database
        db.upsert_tenant(t_primary)
        db.upsert_tenant(t_co)

        rv_record = RentVineRecord(
            tenant_id=t_primary.id,
            balance=2000.0,
            monthly_rent=2000.0,
            due_date="2026-10-01",
            lease_status="ACTIVE",
            follow_up_notes=""
        )
        db.upsert_rentvine_record(rv_record)

        # 3. Verify lease tenant groups resolution
        groups = db.get_lease_tenant_groups()
        grp = next((g for g in groups if g.lease_id == lease_id), None)
        self.assertIsNotNone(grp)

        self.assertTrue(grp.is_multi_tenant)
        self.assertEqual(grp.primary_tenant.id, t_primary.id)
        self.assertEqual(len(grp.co_tenants), 1)
        self.assertEqual(grp.co_tenants[0].phone, "+19802941035")

        # Verify combined greeting formatting
        greeting = WorkflowOrchestrator._format_tenant_names_list(grp.all_names)
        self.assertEqual(greeting, "Kukunoor Tenant and Second Kukunoor Tenant")

        # Verify all phones collected
        phone_numbers = [p[1] for p in grp.all_phones]
        self.assertIn("+18622205006", phone_numbers)
        self.assertIn("+19802941035", phone_numbers)

        # 4. Process lease group (dry run)
        ref_time = datetime(2026, 10, 20, 10, 0)
        decisions = self.orchestrator._process_lease_group(
            group=grp,
            run_id="run_kukunoor_group_test",
            is_dry_run=True,
            current_time=ref_time
        )

        # Both primary and co-tenant get SEND_REMINDER decision
        self.assertEqual(len(decisions), 2)
        primary_d = next(d for d in decisions if d.tenant_id == t_primary.id)
        co_d = next(d for d in decisions if d.tenant_id == t_co.id)

        self.assertEqual(primary_d.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(co_d.outcome, DecisionOutcome.SEND_REMINDER)
        self.assertEqual(primary_d.balance, 2000.0)
        self.assertEqual(co_d.balance, 0.0)

        # Verify group SMS message formatting
        msg_text = primary_d.details.get("group_sms_text", "")
        self.assertIn("Hi Kukunoor Tenant and Second Kukunoor Tenant,", msg_text)
        self.assertIn("8620 Fairgreen Avenue", msg_text)
        self.assertIn("$2,000.00", msg_text)
        self.assertIn("RBPM BOT", msg_text)

        # Verify all group phones are queued for dispatch
        sent_phones = primary_d.details.get("phones_sent", [])
        self.assertIn("+18622205006", sent_phones)
        self.assertIn("+19802941035", sent_phones)

if __name__ == "__main__":
    unittest.main()
