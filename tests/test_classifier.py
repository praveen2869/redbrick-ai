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

    def test_sonnet_model_normalization(self):
        from src.config import normalize_ai_model
        self.assertEqual(normalize_ai_model("claude", "sonnet 5.5"), "claude-3-5-sonnet-20241022")
        self.assertEqual(normalize_ai_model("claude", "claude-3-5-sonnet"), "claude-3-5-sonnet-20241022")
        self.assertEqual(normalize_ai_model("claude", "claude-sonnet-5.5"), "claude-3-5-sonnet-20241022")
        self.assertEqual(normalize_ai_model("claude", "claude-3-7-sonnet"), "claude-3-7-sonnet-20250219")
        self.assertEqual(normalize_ai_model("claude", None), "claude-3-5-sonnet-20241022")

    def test_claude_response_parsing(self):
        import unittest.mock as mock
        import io
        import json

        classifier = AIIntentClassifier(api_key="sk-ant-testkey", model="claude-3-5-sonnet-20241022", provider="claude")
        self.assertEqual(classifier.provider, "claude")
        self.assertEqual(classifier.endpoint, "https://api.anthropic.com/v1/messages")

        mock_claude_resp = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "intent": "PROMISE_MADE",
                        "confidence": 0.98,
                        "extracted_date": "2026-10-16",
                        "extracted_amount": 1500.0,
                        "reasoning": "Tenant explicitly promised $1500 payment tomorrow."
                    })
                }
            ],
            "model": "claude-3-5-sonnet-20241022"
        }

        fake_resp = io.BytesIO(json.dumps(mock_claude_resp).encode("utf-8"))
        fake_resp.read = fake_resp.getvalue

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_cm = mock.MagicMock()
            mock_cm.__enter__.return_value = fake_resp
            mock_urlopen.return_value = mock_cm

            msg = CommunicationMessage(
                id="m10", tenant_id="t10", channel=Channel.SMS, direction=MessageDirection.INBOUND,
                sender="+19803007559", recipient="+1", content="I will pay $1500 tomorrow morning.",
                timestamp="2026-10-15T09:00:00"
            )
            result = classifier.classify_tenant_communications([msg], reference_date=self.ref_date)
            self.assertEqual(result.intent, IntentType.PROMISE_MADE)
            self.assertEqual(result.extracted_date, "2026-10-16")
            self.assertEqual(result.extracted_amount, 1500.0)
            self.assertIn("Claude Sonnet", result.reasoning)

if __name__ == "__main__":
    unittest.main()
