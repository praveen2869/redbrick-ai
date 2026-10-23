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
        self.orchestrator = WorkflowOrchestrator()

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

if __name__ == "__main__":
    unittest.main()
