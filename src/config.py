"""
Red Bricks Property Management - Configuration Module
Loads settings from .env file (or OS environment) with smart mock_mode auto-detection.
"""

import os
from dataclasses import dataclass
from typing import Optional

# ─────────────────────────────────────────────
# Lightweight .env loader (no external deps)
# ─────────────────────────────────────────────
def _load_dotenv(path: str = ".env") -> None:
    """Parse KEY=VALUE lines from the .env file and inject into os.environ."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:  # don't override real env vars
                os.environ[key] = value

_load_dotenv()

# ─────────────────────────────────────────────
# Helper: auto-disable mock_mode when a real
# API key/token is present in the environment
# ─────────────────────────────────────────────
def _mock_mode(env_var: str, credential_var: str, default: str = "") -> bool:
    """
    Returns False (live mode) if:
      - The env_var is explicitly set to 'false'
      - OR a real credential value exists for credential_var (non-empty, non-placeholder)
    Otherwise returns True (simulation mode).
    """
    explicit = os.getenv(env_var, "").lower()
    if explicit == "false":
        return False
    if explicit == "true":
        return True
    # Auto-detect: if credential looks real (not a placeholder), go live
    cred = os.getenv(credential_var, default).strip()
    placeholder_hints = [
        "demo_key", "mock", "placeholder", "example", "redbrick_2026",
        "redbrick_9942", "GOCSPX-redbrick", ""
    ]
    for hint in placeholder_hints:
        if hint and hint.lower() in cred.lower():
            return True
    if cred:
        return False  # non-empty, non-placeholder → live
    return True  # nothing set → simulation

@dataclass
class Settings:
    # Environment
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "redbrick_records.db")

    # ── RentVine ──────────────────────────────────────────────────────────
    # MCP endpoint (for AI tool OAuth integrations like Claude/ChatGPT)
    RENTVINE_MCP_ENDPOINT: str = os.getenv("RENTVINE_MCP_ENDPOINT", "https://mcp.rentvine.ai/mcp")
    # REST API base URL — your Rentvine subdomain (e.g. https://example.rentvine.com)
    RENTVINE_API_BASE_URL: str = os.getenv("RENTVINE_API_BASE_URL", "")
    # Basic auth: Access Key + Secret (from Settings > Users, Roles and API)
    RENTVINE_API_KEY: str = os.getenv("RENTVINE_API_KEY", "rv_live_demo_key_redbrick_2026")
    RENTVINE_API_SECRET: str = os.getenv("RENTVINE_API_SECRET", "")
    # Username/Password (for reference only — MCP OAuth requires a browser flow)
    RENTVINE_USERNAME: str = os.getenv("RENTVINE_USERNAME", "")
    RENTVINE_PASSWORD: str = os.getenv("RENTVINE_PASSWORD", "")
    # Optional pre-captured Bearer token from DevTools
    RENTVINE_BEARER_TOKEN: str = os.getenv("RENTVINE_BEARER_TOKEN", "")
    RENTVINE_AUTH_URL: str = os.getenv("RENTVINE_AUTH_URL", "https://rentvine.com/api/v2/auth/token")
    # Auto mock-mode: False when either API secret OR bearer token is present
    RENTVINE_MOCK_MODE: bool = (
        False if os.getenv("RENTVINE_MOCK_MODE", "").lower() == "false"
        else (True if os.getenv("RENTVINE_MOCK_MODE", "").lower() == "true"
              else not bool(
                  os.getenv("RENTVINE_BEARER_TOKEN", "").strip() or
                  os.getenv("RENTVINE_API_SECRET", "").strip()
              ))
    )


    # ── Quo (OpenPhone) ───────────────────────────────────────────────────
    QUO_API_URL: str = os.getenv("QUO_API_URL", "https://api.openphone.com/v1")
    QUO_API_KEY: str = os.getenv("QUO_API_KEY", "quo_live_secret_redbrick_9942")
    QUO_DEFAULT_PHONE_NUMBER_ID: str = os.getenv("QUO_DEFAULT_PHONE_NUMBER_ID", "PNnMjdTFVO")
    
    # 1. Tenants & Vendors inbox: (980) 300-7559 (Used for rent reminders & follow-ups)
    QUO_PHONE_TENANTS_ID: str = os.getenv("QUO_PHONE_NUMBER_TENANTS", "PNnMjdTFVO")
    QUO_PHONE_TENANTS_FORMATTED: str = "(980) 300-7559"
    QUO_PHONE_TENANTS_E164: str = "+19803007559"

    # 2. Landlords inbox: (980) 300-1196 (Used for landlord communications & updates)
    QUO_PHONE_LANDLORDS_ID: str = os.getenv("QUO_PHONE_NUMBER_LANDLORDS", "PNTDARdwBh")
    QUO_PHONE_LANDLORDS_FORMATTED: str = "(980) 300-1196"
    QUO_PHONE_LANDLORDS_E164: str = "+19803001196"

    # 3. Primary inbox: (667) 281-3879 (General inbox - isolated from tenant/landlord communications)
    QUO_PHONE_PRIMARY_ID: str = os.getenv("QUO_PHONE_NUMBER_PRIMARY", "PNz3t43PKv")
    QUO_PHONE_PRIMARY_FORMATTED: str = "(667) 281-3879"
    QUO_PHONE_PRIMARY_E164: str = "+16672813879"

    # Auto mock-mode: False when a real API key is present
    QUO_MOCK_MODE: bool = _mock_mode(
        "QUO_MOCK_MODE",
        "QUO_API_KEY",
        default="quo_live_secret_redbrick_9942"
    )

    def get_quo_sender(self, recipient_type: str = "tenant") -> tuple:
        """
        Returns (phone_id, e164_number, formatted_number, inbox_name) based on recipient type.
          - 'tenant'   -> ('PNnMjdTFVO', '+19803007559', '(980) 300-7559', 'Tenants & Vendors')
          - 'landlord' -> ('PNTDARdwBh', '+19803001196', '(980) 300-1196', 'Landlords')
          - 'primary'  -> ('PNz3t43PKv', '+16672813879', '(667) 281-3879', 'Primary')
        Primary number is isolated and never used for tenant rent reminders or landlord communications.
        """
        rtype = (recipient_type or "tenant").lower().strip()
        if rtype in ("tenant", "tenants", "vendor", "vendors", "rent_reminder"):
            return (self.QUO_PHONE_TENANTS_ID, self.QUO_PHONE_TENANTS_E164, self.QUO_PHONE_TENANTS_FORMATTED, "Tenants & Vendors")
        elif rtype in ("landlord", "landlords", "owner", "owners"):
            return (self.QUO_PHONE_LANDLORDS_ID, self.QUO_PHONE_LANDLORDS_E164, self.QUO_PHONE_LANDLORDS_FORMATTED, "Landlords")
        elif rtype == "primary":
            return (self.QUO_PHONE_PRIMARY_ID, self.QUO_PHONE_PRIMARY_E164, self.QUO_PHONE_PRIMARY_FORMATTED, "Primary")
        # Strict default: Tenants & Vendors line
        return (self.QUO_PHONE_TENANTS_ID, self.QUO_PHONE_TENANTS_E164, self.QUO_PHONE_TENANTS_FORMATTED, "Tenants & Vendors")

    # ── Gmail ─────────────────────────────────────────────────────────────
    GMAIL_CLIENT_ID: str = os.getenv("GMAIL_CLIENT_ID", "redbrick-gmail-client-id.apps.googleusercontent.com")
    GMAIL_CLIENT_SECRET: str = os.getenv("GMAIL_CLIENT_SECRET", "GOCSPX-redbrick-mock-secret")
    GMAIL_INBOX_LABEL: str = os.getenv("GMAIL_INBOX_LABEL", "INBOX")
    GMAIL_MOCK_MODE: bool = _mock_mode(
        "GMAIL_MOCK_MODE",
        "GMAIL_CLIENT_ID",
        default="redbrick-gmail-client-id.apps.googleusercontent.com"
    )

    # ── AI Engine ─────────────────────────────────────────────────────────
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY") or None
    GROQ_API_KEY: Optional[str] = os.getenv("GROQ_API_KEY") or None
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "groq" if os.getenv("GROQ_API_KEY") else ("openai" if os.getenv("OPENAI_API_KEY") else "heuristics"))
    AI_MODEL: str = os.getenv("AI_MODEL", "llama-3.3-70b-versatile" if os.getenv("GROQ_API_KEY") else "gpt-4o-mini")
    AI_CONFIDENCE_THRESHOLD: float = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.75"))

    # ── Business Rules ────────────────────────────────────────────────────
    GRACE_PERIOD_DAYS: int = int(os.getenv("GRACE_PERIOD_DAYS", "3"))
    DEDUPLICATION_WINDOW_HOURS: int = int(os.getenv("DEDUPLICATION_WINDOW_HOURS", "24"))
    PROMISE_EXPIRY_DAYS: int = int(os.getenv("PROMISE_EXPIRY_DAYS", "5"))
    MAX_RETRY_ATTEMPTS: int = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
    RETRY_BACKOFF_SECONDS: int = int(os.getenv("RETRY_BACKOFF_SECONDS", "300"))

    # ── SMS Reminder Template ─────────────────────────────────────────────
    REMINDER_TEMPLATE: str = (
        "Hi {tenant_name}, friendly reminder from Red Bricks Property Management: "
        "Your rent of ${balance:,.2f} for {property_name} {unit_number} was due on {due_date}. "
        "Please pay via your RentVine portal ({portal_link}) or reply here if you've already sent it. Thank you!"
    )

settings = Settings()
