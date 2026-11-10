"""
Red Bricks Property Management - AI Intent & Natural Language Understanding Classifier
Analyzes tenant SMS and email communications to classify reply intent:
- PROMISE_MADE: Detects payment commitments and extracts commitment dates
- ALREADY_PAID: Detects claims of recent payment for verification
- DISPUTE: Detects maintenance withholding, balance disagreements, legal disputes
- NO_REPLY: When no inbound communication exists
"""

import os
import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, List
from src.config import settings, normalize_ai_model, reload_dotenv
from src.models import IntentType, ClassificationResult, Channel, CommunicationMessage

logger = logging.getLogger("AIClassifier")

class AIIntentClassifier:
    _llm_disabled: bool = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None
    ):
        reload_dotenv()

        # Keys
        if api_key and (provider in ("claude", "anthropic") or "sk-ant" in str(api_key)):
            self.claude_key = str(api_key).strip()
        else:
            self.claude_key = str(os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or settings.ANTHROPIC_API_KEY or "").strip()

        self.groq_key = str(os.getenv("GROQ_API_KEY") or settings.GROQ_API_KEY or "").strip()

        if api_key and (provider == "openai" or ("sk-" in str(api_key) and "sk-ant" not in str(api_key))):
            self.openai_key = str(api_key).strip()
        else:
            self.openai_key = str(os.getenv("OPENAI_API_KEY") or settings.OPENAI_API_KEY or "").strip()

        # Provider resolution
        pref_provider = (provider or os.getenv("AI_PROVIDER") or settings.AI_PROVIDER or "").lower().strip()
        if pref_provider in ("claude", "anthropic") or self.claude_key:
            self.provider = "claude"
            self.api_key = self.claude_key
        elif pref_provider == "groq" or (self.groq_key and not self.openai_key):
            self.provider = "groq"
            self.api_key = self.groq_key
        elif pref_provider == "openai" or self.openai_key:
            self.provider = "openai"
            self.api_key = self.openai_key
        else:
            self.provider = "claude" if pref_provider in ("claude", "anthropic") else "heuristics"
            self.api_key = ""

        # Normalize model for provider (handles aliases like "sonnet 5.5", "claude-3-5-sonnet", etc.)
        default_model = (
            "claude-sonnet-4-6" if self.provider == "claude"
            else ("llama-3.3-70b-versatile" if self.provider == "groq" else "gpt-4o-mini")
        )
        raw_model = model or os.getenv("AI_MODEL") or settings.AI_MODEL or default_model
        self.model = normalize_ai_model(self.provider, raw_model)

        # Endpoints
        if self.provider == "claude":
            self.endpoint = "https://api.anthropic.com/v1/messages"
        elif self.provider == "groq":
            self.endpoint = "https://api.groq.com/openai/v1/chat/completions"
        else:
            self.endpoint = "https://api.openai.com/v1/chat/completions"

        self.is_groq = (self.provider == "groq")

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

        # Resolve relative dates using the message timestamp and applicable local date
        msg_date = now
        if latest_msg.timestamp:
            try:
                parsed_ts = datetime.fromisoformat(latest_msg.timestamp.replace("Z", "+00:00"))
                if parsed_ts.tzinfo is not None:
                    parsed_ts = parsed_ts.replace(tzinfo=None)
                msg_date = parsed_ts
            except Exception:
                msg_date = now

        # Dynamic reload if key was not available during initialization
        if not self.api_key:
            reload_dotenv()
            if self.provider == "claude":
                self.api_key = (os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or "").strip()
            elif self.provider == "groq":
                self.api_key = (os.getenv("GROQ_API_KEY") or "").strip()
            elif self.provider == "openai":
                self.api_key = (os.getenv("OPENAI_API_KEY") or "").strip()

        # Try LLM if API key is present
        if self.api_key and not AIIntentClassifier._llm_disabled:
            try:
                if self.provider == "claude":
                    return self._classify_with_claude(text, latest_msg.channel, msg_date)
                else:
                    return self._classify_with_openai_compatible(text, latest_msg.channel, msg_date)
            except Exception as e:
                provider_display = (
                    f"Claude Sonnet ({self.model})" if self.provider == "claude"
                    else ("Groq" if self.provider == "groq" else "OpenAI")
                )
                err_str = str(e)
                if any(x in err_str for x in ["401", "403", "404", "Forbidden", "Unauthorized", "invalid x-api-key", "Not Found"]):
                    AIIntentClassifier._llm_disabled = True
                    logger.warning(
                        f"{provider_display} API auth error ({e}). Please verify API key in .env. "
                        "Falling back to built-in semantic NLP engine."
                    )
                else:
                    logger.warning(f"{provider_display} API call failed: {e}. Falling back to internal NLP engine.")

        # Fallback to internal heuristic semantic classifier
        return self._classify_with_heuristics(text, latest_msg.channel, msg_date)

    def _classify_with_claude(
        self, text: str, channel: Channel, reference_date: datetime
    ) -> ClassificationResult:
        """Calls Anthropic Messages API (Claude 3.5 Sonnet) using standard library urllib."""
        ref_date_str = reference_date.strftime("%Y-%m-%d (%A)")
        system_prompt = (
            f"You are an AI assistant for Red Bricks Property Management. "
            f"Current reference date is {ref_date_str}. "
            "Analyze the tenant's reply to an overdue rent notification and classify their intent.\n"
            "You MUST respond with ONLY a valid raw JSON object (no markdown fences, no preamble, no commentary) with the following structure:\n"
            "{\n"
            '  "intent": "PROMISE_MADE" | "PAYMENT_UNSPECIFIED" | "UNABLE_TO_PAY" | "DISPUTE" | "ALREADY_PAID" | "UNRELATED" | "NO_REPLY",\n'
            '  "confidence": 0.95,\n'
            '  "extracted_date": "YYYY-MM-DD" or null,\n'
            '  "extracted_amount": 1500.0 or null,\n'
            '  "reasoning": "Brief explanation of decision"\n'
            "}\n"
            "Strict classification rules:\n"
            "1. PROMISE_MADE: ONLY when tenant provides an EXPLICIT, SPECIFIC payment date (e.g. tomorrow, this Friday, Nov 15th, on the 20th). "
            "You MUST populate 'extracted_date' (YYYY-MM-DD). If NO specific date is given, NEVER use PROMISE_MADE!\n"
            "2. PAYMENT_UNSPECIFIED: Payment-related reply WITHOUT a specific date (e.g. 'Yes, I will do', 'I will pay soon', 'I need more time', 'I will try to arrange payment', 'Working on it', 'I will take care of it').\n"
            "3. UNABLE_TO_PAY: Tenant states they cannot or will not pay (e.g. 'I can't pay right now', 'I don't have the money', 'I lost my job', 'Cannot afford this', 'I refuse to pay').\n"
            "4. DISPUTE: Tenant disputes the balance amount, maintenance issues, withholding rent, or mentions legal action / attorney.\n"
            "5. ALREADY_PAID: Tenant claims they already made the payment or transferred funds.\n"
            "6. UNRELATED: Unrelated conversations, greetings, wrong numbers, or general questions not related to payment commitment.\n"
            "7. NO_REPLY: Empty message."
        )

        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": 0.1,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": f'Tenant message: "{text}"'}
            ]
        }

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "User-Agent": "RedBricks-Automation/2.0"
            }
        )

        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content_blocks = data.get("content", [])
            raw_text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

            clean_json = raw_text.strip()
            if "```json" in clean_json:
                clean_json = clean_json.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in clean_json:
                clean_json = clean_json.split("```", 1)[1].split("```", 1)[0].strip()
            else:
                m = re.search(r'(\{[\s\S]*\})', clean_json)
                if m:
                    clean_json = m.group(1)

            parsed = json.loads(clean_json)
            intent_str = parsed.get("intent", "GENERAL_INQUIRY")
            try:
                intent = IntentType(intent_str)
            except Exception:
                intent = IntentType.GENERAL_INQUIRY

            extracted_amt = parsed.get("extracted_amount")
            if extracted_amt is not None:
                try:
                    extracted_amt = float(extracted_amt)
                except (ValueError, TypeError):
                    extracted_amt = None

            return ClassificationResult(
                intent=intent,
                confidence=float(parsed.get("confidence", 0.95)),
                raw_text=text,
                source_channel=channel,
                extracted_date=parsed.get("extracted_date"),
                extracted_amount=extracted_amt,
                reasoning=(
                    f"{parsed.get('reasoning')} [Claude Sonnet ({self.model})]"
                    if parsed.get("reasoning")
                    else f"Classified via Claude Sonnet ({self.model})"
                )
            )

    def _classify_with_openai_compatible(
        self, text: str, channel: Channel, reference_date: datetime
    ) -> ClassificationResult:
        """Calls Groq or OpenAI endpoint using lightweight JSON-RPC / REST."""
        ref_date_str = reference_date.strftime("%Y-%m-%d (%A)")
        system_prompt = (
            f"You are an AI assistant for Red Bricks Property Management. "
            f"Current date is {ref_date_str}. "
            "Analyze the tenant's reply to rent notification. "
            "Respond ONLY with a JSON object with fields: "
            "intent (PROMISE_MADE, PAYMENT_UNSPECIFIED, UNABLE_TO_PAY, DISPUTE, ALREADY_PAID, UNRELATED, NO_REPLY), "
            "confidence (float 0.0-1.0), "
            "extracted_date (YYYY-MM-DD or null), "
            "extracted_amount (float or null), "
            "reasoning (short explanation). "
            "Rules: PROMISE_MADE strictly requires an explicit identifiable date. If no date is given, use PAYMENT_UNSPECIFIED."
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
        intent recognition, dispute detection, and date extraction according to strict business rules:
          1. Specific payment date provided -> PROMISE_MADE (with extracted_date)
          2. Payment-related reply without specific date -> PAYMENT_UNSPECIFIED
          3. Unable or unwilling to pay -> UNABLE_TO_PAY
          4. Dispute or balance contestation -> DISPUTE
          5. Already paid claims -> ALREADY_PAID
          6. Unrelated or general inquiry -> UNRELATED
        """
        lower = text.lower().strip()

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

        # 2. Unable or Unwilling to Pay indicators
        unable_patterns = [
            r"can['’]?t pay", r"cannot pay", r"unable to pay", r"don['’]?t have (?:the )?money",
            r"no money", r"don['’]?t have enough", r"out of money", r"broke (?:right now|at the moment)?",
            r"lost my job", r"lost job", r"unemployed", r"laid off", r"refuse to pay",
            r"won['’]?t pay", r"not going to pay", r"not paying", r"hardship",
            r"cannot afford", r"can['’]?t afford", r"impossible to pay"
        ]
        for pat in unable_patterns:
            if re.search(pat, lower):
                return ClassificationResult(
                    intent=IntentType.UNABLE_TO_PAY,
                    confidence=0.95,
                    raw_text=text,
                    source_channel=channel,
                    reasoning=f"Tenant states unable or unwilling to pay: '{pat}'"
                )

        # 3. Already Paid indicators
        already_paid_patterns = [
            r"already paid", r"just paid", r"paid it this morning", r"paid yesterday",
            r"transferred", r"sent the money", r"check dropped off", r"receipt attached",
            r"submitted payment", r"went through", r"check was delivered", r"paid on the portal",
            r"paid online", r"zelle sent", r"venmo sent", r"payment was submitted"
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

        # 4. Payment Promise or Payment-Related Reply indicators
        payment_phrases = [
            r"will pay", r"i['’]?ll pay", r"ill pay", r"going to pay", r"gonna pay",
            r"promise to pay", r"can pay", r"sending payment", r"sending.*tomorrow",
            r"sending.*friday", r"sending.*tonight", r"pay.*this friday", r"paycheck",
            r"pay.*on the", r"transfer.*on", r"will send", r"will settle", r"clearing on",
            r"will do", r"yes,? i will do", r"need more time", r"arrange payment",
            r"trying to arrange", r"try to arrange", r"pay soon", r"work on payment",
            r"working on it", r"take care of it", r"handle it", r"pay later"
        ]
        is_payment_related = any(re.search(pat, lower) for pat in payment_phrases)

        # Attempt to extract an explicitly identifiable future payment date
        extracted_date = self._extract_date_from_text(lower, reference_date)
        extracted_amount = self._extract_amount_from_text(text)

        if is_payment_related:
            if extracted_date is not None:
                # Specific payment date confirmed -> Eligible for Payment Promised (On Hold)
                return ClassificationResult(
                    intent=IntentType.PROMISE_MADE,
                    confidence=0.94,
                    raw_text=text,
                    source_channel=channel,
                    extracted_date=extracted_date,
                    extracted_amount=extracted_amount,
                    reasoning=f"Tenant made confirmed payment commitment with specific date: {extracted_date}"
                )
            else:
                # Payment-related reply WITHOUT a specific date -> Manual Review / Disputes
                return ClassificationResult(
                    intent=IntentType.PAYMENT_UNSPECIFIED,
                    confidence=0.90,
                    raw_text=text,
                    source_channel=channel,
                    extracted_date=None,
                    extracted_amount=extracted_amount,
                    reasoning="Payment-related reply without a specific payment date"
                )

        # 5. Unrelated conversation or general inquiry
        return ClassificationResult(
            intent=IntentType.UNRELATED,
            confidence=0.75,
            raw_text=text,
            source_channel=channel,
            reasoning="Unrelated conversation or general message; does not affect reminder eligibility"
        )

    def _extract_date_from_text(self, text: str, ref_date: datetime) -> Optional[str]:
        """
        Extracts relative or absolute dates from conversational text.
        Returns None if NO specific, identifiable date is found. Never falls back to arbitrary dates.
        """
        # Tomorrow
        if "tomorrow" in text:
            return (ref_date + timedelta(days=1)).strftime("%Y-%m-%d")

        # Tonight / Today
        if "tonight" in text or "today" in text or "later today" in text:
            return ref_date.strftime("%Y-%m-%d")

        # "in X days" e.g. "in 2 days", "in 3 days"
        in_days_m = re.search(r"\bin\s+(\d+)\s+days?\b", text)
        if in_days_m:
            try:
                days_cnt = int(in_days_m.group(1))
                if 1 <= days_cnt <= 60:
                    return (ref_date + timedelta(days=days_cnt)).strftime("%Y-%m-%d")
            except Exception:
                pass

        # Days of week: e.g. Friday, Monday, etc.
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6
        }
        for day_name, target_idx in weekdays.items():
            if re.search(rf"\b{day_name}\b", text):
                current_idx = ref_date.weekday()
                days_ahead = (target_idx - current_idx) % 7
                if days_ahead == 0:
                    days_ahead = 7  # Next week's instance
                return (ref_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        # "on the 5th", "on the 12th", "by the 15th", etc.
        day_match = re.search(r"(?:on|by)\s+the\s+(\d{1,2})(?:st|nd|rd|th)?", text)
        if day_match:
            day_num = int(day_match.group(1))
            try:
                month = ref_date.month
                year = ref_date.year
                if day_num < ref_date.day:
                    month = month + 1 if month < 12 else 1
                    year = year if month != 1 else year + 1
                return datetime(year, month, day_num).strftime("%Y-%m-%d")
            except Exception:
                pass

        # Explicit dates: Month Day e.g. "Nov 25", "October 25th", "December 1"
        months = ["january", "february", "march", "april", "may", "june",
                  "july", "august", "september", "october", "november", "december",
                  "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        for m_idx, m_name in enumerate(months):
            m_num = (m_idx % 12) + 1
            match = re.search(rf"\b{m_name}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text)
            if match:
                day_num = int(match.group(1))
                try:
                    return datetime(ref_date.year, m_num, day_num).strftime("%Y-%m-%d")
                except Exception:
                    pass

        # MM/DD or MM/DD/YYYY format e.g. "11/15" or "11/15/2026"
        slash_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
        if slash_match:
            try:
                m_val = int(slash_match.group(1))
                d_val = int(slash_match.group(2))
                y_val = int(slash_match.group(3)) if slash_match.group(3) else ref_date.year
                if y_val < 100:
                    y_val += 2000
                if 1 <= m_val <= 12 and 1 <= d_val <= 31:
                    return datetime(y_val, m_val, d_val).strftime("%Y-%m-%d")
            except Exception:
                pass

        # ISO format: YYYY-MM-DD
        iso_m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
        if iso_m:
            try:
                y = int(iso_m.group(1))
                m = int(iso_m.group(2))
                d = int(iso_m.group(3))
                return datetime(y, m, d).strftime("%Y-%m-%d")
            except Exception:
                pass

        # Strict requirement: If no specific date was identifiable, return None!
        return None

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
