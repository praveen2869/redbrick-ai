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
        self.orchestrator = WorkflowOrchestrator(
            rentvine_client=RentVineMCPClient(mock_mode=True)
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
        rv_client = RentVineMCPClient()
        report = rv_client.fetch_lease_balance_report(report_id=72)
        
        self.assertEqual(report["report_id"], "72")
        self.assertEqual(report["title"], "Lease Balances")
        self.assertGreaterEqual(len(report["records"]), 17)
        self.assertGreater(report["totals"]["total_balance_due"], 0)
        self.assertGreater(report["totals"]["past_due_rent"], 0)

        # Sync to DB and execute workflow enforcing Report #72
        orch = WorkflowOrchestrator(rentvine_client=rv_client)
        summary = orch.execute_daily_run(is_dry_run=True, use_report_source_of_truth=True)

        # All active tenants with overdue balance (even $1.00) are eligible and receive reminders
        self.assertEqual(summary.count_reminded, len(report["records"]))
        reminded_names = [d.tenant_name for d in summary.decisions if d.outcome == DecisionOutcome.SEND_REMINDER]
        self.assertIn("Gary Owens", reminded_names)
        self.assertIn("Christopher Pittman", reminded_names)
        self.assertIn("Ke'Juana Ford", reminded_names)
        # Even $1.00 tenants are included
        self.assertIn("Ashley Oliver", reminded_names)
        self.assertIn("Francis Matuvanga", reminded_names)
        self.assertIn("Yuliya Chernyshov", reminded_names)
        self.assertIn("Kayla McKennon", reminded_names)

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

if __name__ == "__main__":
    unittest.main()
