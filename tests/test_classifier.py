"""
Unit tests for AI Intent Classifier and Natural Language Understanding
"""

import unittest
from datetime import datetime
from src.models import IntentType, Channel, CommunicationMessage, MessageDirection
from src.engine.ai_classifier import AIIntentClassifier

class TestAIIntentClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = AIIntentClassifier()
        self.ref_date = datetime(2026, 10, 15) # Thursday

    def test_promise_made_classification(self):
        msg = CommunicationMessage(
            id="m1", tenant_id="t1", channel=Channel.SMS, direction=MessageDirection.INBOUND,
            sender="+1", recipient="+2", content="Sorry for delay! I will pay $1500 tomorrow morning.",
            timestamp="2026-10-15T09:00:00"
        )
        res = self.classifier.classify_tenant_communications([msg], reference_date=self.ref_date)
        self.assertEqual(res.intent, IntentType.PROMISE_MADE)
        self.assertEqual(res.extracted_date, "2026-10-16")
        self.assertEqual(res.extracted_amount, 1500.0)

    def test_already_paid_classification(self):
        msg = CommunicationMessage(
            id="m2", tenant_id="t1", channel=Channel.SMS, direction=MessageDirection.INBOUND,
            sender="+1", recipient="+2", content="I already paid this morning via rentvine portal",
            timestamp="2026-10-15T09:00:00"
        )
        res = self.classifier.classify_tenant_communications([msg], reference_date=self.ref_date)
        self.assertEqual(res.intent, IntentType.ALREADY_PAID)

    def test_dispute_classification(self):
        msg = CommunicationMessage(
            id="m3", tenant_id="t1", channel=Channel.SMS, direction=MessageDirection.INBOUND,
            sender="+1", recipient="+2", content="You charged me twice and the AC is still broken! I am withholding rent until fixed.",
            timestamp="2026-10-15T09:00:00"
        )
        res = self.classifier.classify_tenant_communications([msg], reference_date=self.ref_date)
        self.assertEqual(res.intent, IntentType.DISPUTE)

    def test_no_reply(self):
        res = self.classifier.classify_tenant_communications([], reference_date=self.ref_date)
        self.assertEqual(res.intent, IntentType.NO_REPLY)

if __name__ == "__main__":
    unittest.main()
