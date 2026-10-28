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

if __name__ == "__main__":
    unittest.main()
