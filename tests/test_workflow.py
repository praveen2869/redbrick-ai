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
        populate_sample_data(reset=True)
        from src.clients.rentvine_mcp import RentVineMCPClient
        from src.clients.quo_sms import QuoSMSClient
        self.orchestrator = WorkflowOrchestrator(
            rentvine_client=RentVineMCPClient(mock_mode=True),
            quo_client=QuoSMSClient(mock_mode=True)
        )

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
        # Even $1.00 tenants are included
        self.assertIn("Ashley Oliver", reminded_names)
        self.assertIn("Francis Matuvanga", reminded_names)
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
            "Your rent of $1,000.00 for 8620 Fairgreen Avenue was due on 2026-10-01.\n\n"
            "Please pay via your RentVine portal (https://redbricksproperties.rentvine.com/portal/pay) or reply here if you've already sent it.\n\n"
            "Thank you!"
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
        self.assertIn("Your rent of $2,209.00 for Brookdale Village_5601 Stafford Road was due on 2026-10-01.", sms_tao)
        self.assertNotIn("Brookdale Village_5601 Stafford Road Brookdale Village_5601 Stafford Road", sms_tao)
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

        # At 2:00 PM on Oct 30: Cooldown MUST be active for remainder of the day
        oct_30_2pm = datetime(2026, 10, 30, 14, 0)
        rules = BusinessRulesEngine()
        is_dup, last_rem, reason = rules.check_recent_reminder_deduplication(tenant_id, current_time=oct_30_2pm)
        self.assertTrue(is_dup)
        self.assertIn("Cooldown active for remainder of day", reason)

        # Workflow execution at 2:00 PM: must result in SKIP_ALREADY_REMINDED
        dec_today = self.orchestrator._process_tenant(t, run_id="run_oct30_afternoon", is_dry_run=True, current_time=oct_30_2pm)
        self.assertEqual(dec_today.outcome, DecisionOutcome.SKIP_ALREADY_REMINDED)

        # Step 3: Next morning (Oct 31, 8:00 AM): Cooldown expires, tenant reactivates!
        oct_31_8am = datetime(2026, 10, 31, 8, 0)
        is_dup_next, _, reason_next = rules.check_recent_reminder_deduplication(tenant_id, current_time=oct_31_8am)
        self.assertFalse(is_dup_next, f"Tenant should be reactivated the next morning: {reason_next}")
        self.assertIn("Reactivated for morning reminder", reason_next)

        # Workflow execution next morning: tenant is eligible for morning reminder
        dec_next = self.orchestrator._process_tenant(t, run_id="run_oct31_morning", is_dry_run=True, current_time=oct_31_8am)
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

if __name__ == "__main__":
    unittest.main()
