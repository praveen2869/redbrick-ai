"""
Red Bricks Property Management - AI Intent & Natural Language Understanding Classifier
Analyzes tenant SMS and email communications to classify reply intent:
- PROMISE_MADE: Detects payment commitments and extracts commitment dates
- ALREADY_PAID: Detects claims of recent payment for verification
- DISPUTE: Detects maintenance withholding, balance disagreements, legal disputes
- NO_REPLY: When no inbound communication exists
"""

import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, List
from src.config import settings
from src.models import IntentType, ClassificationResult, Channel, CommunicationMessage

logger = logging.getLogger("AIClassifier")

class AIIntentClassifier:
    _llm_disabled: bool = False

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.groq_key = settings.GROQ_API_KEY
        self.openai_key = api_key or settings.OPENAI_API_KEY
        self.api_key = self.groq_key or self.openai_key
        self.is_groq = bool(self.groq_key)
        self.model = model or settings.AI_MODEL or ("llama-3.3-70b-versatile" if self.is_groq else "gpt-4o-mini")
        self.endpoint = (
            "https://api.groq.com/openai/v1/chat/completions"
            if self.is_groq
            else "https://api.openai.com/v1/chat/completions"
        )

    def classify_tenant_communications(
        self,
        messages: List[CommunicationMessage],
        reference_date: Optional[datetime] = None
    ) -> ClassificationResult:
        """
        Classifies the most recent inbound communication from the tenant.
        Prioritizes SMS from Quo, then Email from Gmail.
        """
        now = reference_date or datetime.utcnow()

        # Find latest inbound message
        inbound_messages = [
            m for m in messages
            if m.direction.value == "INBOUND"
        ]

        if not inbound_messages:
            return ClassificationResult(
                intent=IntentType.NO_REPLY,
                confidence=1.0,
                raw_text="",
                source_channel=Channel.SMS,
                reasoning="No inbound tenant replies recorded in Quo or Gmail"
            )

        # Sort by timestamp descending
        latest_msg = sorted(inbound_messages, key=lambda x: x.timestamp, reverse=True)[0]
        text = latest_msg.content.strip()

        # Try LLM (Groq or OpenAI) if API key is present and not marked disabled
        if self.api_key and not AIIntentClassifier._llm_disabled:
            try:
                return self._classify_with_llm(text, latest_msg.channel, now)
            except Exception as e:
                provider_name = "Groq" if self.is_groq else "OpenAI"
                if "403" in str(e) or "401" in str(e) or "Forbidden" in str(e) or "Unauthorized" in str(e):
                    AIIntentClassifier._llm_disabled = True
                    logger.warning(f"{provider_name} API returned auth error ({e}). Disabling LLM calls and using internal NLP engine.")
                else:
                    logger.warning(f"{provider_name} API call failed: {e}. Falling back to internal NLP engine.")

        # Fallback to internal heuristic semantic classifier
        return self._classify_with_heuristics(text, latest_msg.channel, now)

    def _classify_with_llm(
        self, text: str, channel: Channel, reference_date: datetime
    ) -> ClassificationResult:
        """Calls Groq or OpenAI endpoint using lightweight JSON-RPC / REST."""
        ref_date_str = reference_date.strftime("%Y-%m-%d (%A)")
        system_prompt = (
            f"You are an AI assistant for Red Bricks Property Management. "
            f"Current date is {ref_date_str}. "
            "Analyze the tenant's reply to rent notification. "
            "Respond ONLY with a JSON object with fields: "
            "intent (PROMISE_MADE, ALREADY_PAID, DISPUTE, GENERAL_INQUIRY, NO_REPLY), "
            "confidence (float 0.0-1.0), "
            "extracted_date (YYYY-MM-DD or null), "
            "extracted_amount (float or null), "
            "reasoning (short explanation)."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Tenant message: \"{text}\""}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
        )

        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)

            intent_str = parsed.get("intent", "GENERAL_INQUIRY")
            try:
                intent = IntentType(intent_str)
            except Exception:
                intent = IntentType.GENERAL_INQUIRY

            provider_name = f"Groq ({self.model})" if self.is_groq else f"OpenAI ({self.model})"
            return ClassificationResult(
                intent=intent,
                confidence=float(parsed.get("confidence", 0.95)),
                raw_text=text,
                source_channel=channel,
                extracted_date=parsed.get("extracted_date"),
                extracted_amount=parsed.get("extracted_amount"),
                reasoning=parsed.get("reasoning", f"Classified via {provider_name}")
            )

    def _classify_with_heuristics(
        self, text: str, channel: Channel, reference_date: datetime
    ) -> ClassificationResult:
        """
        High-precision semantic classifier that handles common tenant phrasing patterns,
        intent recognition, dispute detection, and date extraction.
        """
        lower = text.lower()

        # 1. Dispute or manual review indicators
        dispute_patterns = [
            r"wrong amount", r"incorrect amount", r"overcharged", r"charged me twice",
            r"withhold", r"withholding", r"broken", r"repair", r"leak", r"mold",
            r"lawyer", r"attorney", r"dispute", r"not paying until", r"unfair",
            r"fraud", r"eviction notice", r"harassment", r"contact my lawyer"
        ]
        for pat in dispute_patterns:
            if re.search(pat, lower):
                return ClassificationResult(
                    intent=IntentType.DISPUTE,
                    confidence=0.95,
                    raw_text=text,
                    source_channel=channel,
                    reasoning=f"Detected dispute/maintenance condition matching '{pat}'"
                )

        # 2. Already Paid indicators
        already_paid_patterns = [
            r"already paid", r"just paid", r"paid it this morning", r"paid yesterday",
            r"transferred", r"sent the money", r"check dropped off", r"receipt attached",
            r"submitted payment", r"went through", r"check was delivered", r"paid on the portal",
            r"zelle sent", r"venmo sent"
        ]
        for pat in already_paid_patterns:
            if re.search(pat, lower):
                return ClassificationResult(
                    intent=IntentType.ALREADY_PAID,
                    confidence=0.92,
                    raw_text=text,
                    source_channel=channel,
                    reasoning=f"Tenant claims payment has already occurred ('{pat}')"
                )

        # 3. Payment Promise indicators
        promise_patterns = [
            r"will pay", r"i'll pay", r"ill pay", r"going to pay", r"gonna pay",
            r"promise to pay", r"sending.*tomorrow", r"sending.*friday", r"sending.*tonight",
            r"pay.*this friday", r"paycheck", r"pay.*on the", r"transfer.*on",
            r"can pay by", r"will send on", r"will settle", r"pay next week",
            r"pay when i get paid", r"clearing on"
        ]
        is_promise = any(re.search(pat, lower) for pat in promise_patterns)

        if is_promise:
            extracted_date = self._extract_date_from_text(lower, reference_date)
            extracted_amount = self._extract_amount_from_text(text)
            return ClassificationResult(
                intent=IntentType.PROMISE_MADE,
                confidence=0.94,
                raw_text=text,
                source_channel=channel,
                extracted_date=extracted_date,
                extracted_amount=extracted_amount,
                reasoning=f"Tenant made explicit payment commitment for {extracted_date or 'unspecified date'}"
            )

        # 4. General inquiry / other
        return ClassificationResult(
            intent=IntentType.GENERAL_INQUIRY,
            confidence=0.70,
            raw_text=text,
            source_channel=channel,
            reasoning="Message does not contain specific payment promise or dispute"
        )

    def _extract_date_from_text(self, text: str, ref_date: datetime) -> Optional[str]:
        """Extracts relative or absolute dates from conversational text."""
        # Tomorrow
        if "tomorrow" in text:
            return (ref_date + timedelta(days=1)).strftime("%Y-%m-%d")

        # Tonight / Today
        if "tonight" in text or "today" in text or "later today" in text:
            return ref_date.strftime("%Y-%m-%d")

        # Days of week: e.g. Friday, Monday, etc.
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6
        }
        for day_name, target_idx in weekdays.items():
            if day_name in text:
                current_idx = ref_date.weekday()
                days_ahead = (target_idx - current_idx) % 7
                if days_ahead == 0:
                    days_ahead = 7  # Next week's instance
                return (ref_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        # "on the 5th", "on the 12th", etc.
        day_match = re.search(r"(?:on|by)\s+the\s+(\d{1,2})(?:st|nd|rd|th)?", text)
        if day_match:
            day_num = int(day_match.group(1))
            try:
                # If target day is earlier in the month than today, assume next month
                month = ref_date.month
                year = ref_date.year
                if day_num < ref_date.day:
                    month = month + 1 if month < 12 else 1
                    year = year if month != 1 else year + 1
                return datetime(year, month, day_num).strftime("%Y-%m-%d")
            except Exception:
                pass

        # Explicit dates: YYYY-MM-DD or Oct 25 / October 25
        months = ["january", "february", "march", "april", "may", "june",
                  "july", "august", "september", "october", "november", "december",
                  "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        for m_idx, m_name in enumerate(months):
            m_num = (m_idx % 12) + 1
            match = re.search(rf"{m_name}\s+(\d{{1,2}})", text)
            if match:
                day_num = int(match.group(1))
                try:
                    return datetime(ref_date.year, m_num, day_num).strftime("%Y-%m-%d")
                except Exception:
                    pass

        # Fallback default promise: 3 days from reference
        return (ref_date + timedelta(days=3)).strftime("%Y-%m-%d")

    def _extract_amount_from_text(self, text: str) -> Optional[float]:
        """Extracts dollar amounts like $1,400 or $500."""
        match = re.search(r"\$\s*([0-9,]+(?:\.[0-9]{2})?)", text)
        if match:
            clean = match.group(1).replace(",", "")
            try:
                return float(clean)
            except Exception:
                pass
        return None
