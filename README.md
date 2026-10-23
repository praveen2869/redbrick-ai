# 🏢 Red Bricks Property Management
## AI-Powered Automated Rent Reminder Workflow
### Solution Architecture & Operational Control Center

![Platform Integration](https://img.shields.io/badge/RentVine_MCP-Native_Integration-blue?style=flat-square)
![SMS Channel](https://img.shields.io/badge/Quo_(OpenPhone)-REST_API-purple?style=flat-square)
![Email Channel](https://img.shields.io/badge/Gmail-OAuth_2.0-red?style=flat-square)
![Orchestrator](https://img.shields.io/badge/Python_Agent-Independent_Core-cyan?style=flat-square)
![Trigger](https://img.shields.io/badge/Azure_Scheduler-Daily_08:00_UTC-darkblue?style=flat-square)

---

## 1. Executive Summary

**Red Bricks Property Management** automates rent collections and tenant communications with a **Python Automation Agent** that:
- Runs automatically every day via **Azure Scheduler**.
- Connects directly to **RentVine** via the official **Model Context Protocol (MCP)** (`https://mcp.rentvine.ai/mcp`) for read-only financial balances, payment history, and automated note write-backs.
- Reads SMS communication history and sends reminders through **Quo (formerly OpenPhone)** REST API (`https://api.openphone.com/v1`).
- Monitors resident replies and confirmation receipts through **Gmail API (OAuth 2.0)**.
- Applies a **hybrid AI + Business Rules Engine** that evaluates payments, grace periods, deduplication cooldowns (24 hours), and classifies tenant reply intent (`PROMISE_MADE`, `ALREADY_PAID`, `DISPUTE`, `NO_REPLY`).
- Suppresses harassing reminders when a tenant has already promised payment, updates RentVine notes, and generates a daily executive summary report.

---

## 2. System Architecture Flowchart

```
┌─────────────────────────────────────────────────────────┐
│              AZURE SCHEDULER                            │
│          Runs automatically every day (08:00 UTC)       │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│         PYTHON AUTOMATION AGENT                         │
│       Runs independently of ChatGPT                     │
│                                                         │
│  ┌─────────────┐  ┌───────────┐  ┌──────────────────┐   │
│  │  RENTVINE   │  │    QUO    │  │     GMAIL        │   │
│  │ (MCP/API)   │  │   (API)   │  │    (API)         │   │
│  │             │  │           │  │                  │   │
│  │ • Balance   │  │ • SMS     │  │ • Email replies  │   │
│  │ • Payments  │  │   history │  │ • Confirmations  │   │
│  │ • Notes     │  │ • Replies │  │                  │   │
│  └──────┬──────┘  └─────┬─────┘  └────────┬─────────┘   │
│         └───────────────┴─────────────────┘             │
│                         │                               │
│                         ▼                               │
│         ┌───────────────────────────────┐               │
│         │   AI + BUSINESS RULES ENGINE  │               │
│         │ • Balance check               │               │
│         │ • Due date validation         │               │
│         │ • Payment promise detection   │               │
│         │ • Previous reminder history   │               │
│         └──────────────┬────────────────┘               │
│                        │                                │
│         ┌──────────────┼──────────────┐                 │
│         ▼              ▼              ▼                 │
│  ┌─────────────┐ ┌──────────┐ ┌────────────────┐        │
│  │ RENT PAID?  │ │ VALID    │ │ UNPAID &       │        │
│  │ Skip        │ │ PROMISE? │ │ ELIGIBLE?      │        │
│  │ reminder    │ │ Wait +   │ │ Send Quo SMS   │        │
│  │ [GREEN]     │ │ Update   │ │ reminder       │        │
│  └─────────────┘ │ RentVine │ │ [RED]          │        │
│                  │ note     │ └────────────────┘        │
│                  │ [ORANGE] │                           │
│                  └──────────┘                           │
│                        │                                │
│                        ▼                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │ RESULTS LAYER                                    │   │
│  │ • Save results  • Prevent duplicates             │   │
│  │ • Retry failures  • Daily report                 │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Platform Integrations

### 3.1 RentVine — MCP Integration
- **Endpoint:** `https://mcp.rentvine.ai/mcp`
- **Protocol:** Model Context Protocol (MCP) JSON-RPC 2.0
- **Security:** Governed by existing RentVine role-based access; **financial data is read-only**.
- **Capabilities:**
  - `get_tenant_financial_summary`: Queries balances, due dates, and payment history.
  - `create_tenant_note`: Automatically writes back payment promises and confirmation timestamps to the tenant profile.

### 3.2 Quo (formerly OpenPhone) — SMS Channel
- **Endpoint:** `https://api.openphone.com/v1/messages`
- **Capabilities:**
  - Programmatically dispatches personalized SMS reminders containing rent balance, property unit, and direct RentVine portal payment links.
  - Queries the last 7 days of message history to inspect tenant text replies.
  - Shared team inbox keeps all property management staff synchronized.

### 3.3 Gmail — Email Channel
- **Protocol:** Google Workspace Gmail API (OAuth 2.0 Read-Only)
- **Capabilities:**
  - Monitors resident email replies, dispute notices, and payment receipts.
  - Feeds signals directly into the AI classification pipeline.

---

## 4. Decision Matrix & Workflow Outcomes

| Condition | Action | Audit & System Log |
|---|---|---|
| **Rent already paid** | ✅ **SKIP** — No reminder sent | Logged: `Paid — no action` |
| **Valid payment promise detected** | 🛡️ **HOLD** — Suppress reminder | Extracted commitment date logged in RentVine notes; re-evaluated on promise date |
| **Unpaid & eligible** | 📩 **SEND** — Dispatch Quo SMS reminder | Message delivered via Quo API; note recorded in RentVine |
| **Already reminded today** | 🚫 **SKIP** — Deduplication cooldown | Deduplication window (24h) prevents harassment |
| **Within grace period** | 🕒 **SKIP** — In grace period | Grace period active (days 1–3) |
| **Dispute or manual flag** | 🔴 **FLAG** — Escalate to staff | Account flagged for immediate property manager intervention |

---

## 5. Quick Start & Execution

### Prerequisites
- Python 3.11+
- No third-party packages required for core runtime (uses Python standard library).

### 1. Launch the Operations Control Center
Start the local server and web dashboard:
```bash
python main.py serve
```
Open **`http://127.0.0.1:8000/`** in your browser to view:
- **Interactive Neon Architecture Flowchart** with real-time execution animations and node telemetry.
- **Tenant Portfolio & Decision Matrix** with live filters, search, and multi-channel inspector.
- **Tenant Reply Simulator & Sandbox** to test arbitrary text messages against the AI NLU classifier.
- **Daily Executive Report Viewer** with PDF/Print export.

### 2. CLI Execution Modes
- **Run Live Daily Workflow:**
  ```bash
  python main.py run
  ```
- **Simulate Dry-Run (No SMS sent):**
  ```bash
  python main.py dry-run
  ```
- **Reset Demo Portfolio (12 sample tenants with rich scenarios):**
  ```bash
  python main.py seed
  ```
- **Generate Markdown Executive Report:**
  ```bash
  python main.py report
  ```

### 3. Run Automated Test Suite
```bash
python -m unittest discover -s tests
```

---

## 6. Directory Structure

```
Redbrick/
├── main.py                     # CLI and Web Dashboard entrypoint
├── requirements.txt            # Production dependencies
├── .env.example                # Environment variables template
├── README.md                   # System documentation
├── redbrick_records.db         # Persistent SQLite database
├── src/
│   ├── config.py               # Environment configuration & business defaults
│   ├── models.py               # Domain models (Tenant, RentVineRecord, DecisionOutcome)
│   ├── database.py             # SQLite persistence & audit trail
│   ├── workflow.py             # 5-step daily automation orchestrator
│   ├── reporting.py            # HTML, Markdown, and JSON report generator
│   ├── seed_data.py            # Comprehensive portfolio seed data
│   ├── server.py               # HTTP server & REST API
│   ├── clients/
│   │   ├── rentvine_mcp.py     # RentVine MCP client (JSON-RPC 2.0 / REST)
│   │   ├── quo_sms.py          # Quo (OpenPhone) SMS REST client
│   │   └── gmail_client.py     # Gmail API client
│   └── engine/
│       ├── rules.py            # Deterministic Business Rules Engine
│       ├── ai_classifier.py    # AI NLU Intent Classifier & Date Parser
│       └── retry_manager.py    # 3x exponential backoff retry manager
├── web/
│   ├── index.html              # Single-page executive operations portal
│   ├── css/
│   │   ├── styles.css          # Sleek dark-mode design system & glassmorphism
│   │   └── flowchart.css       # Neon glowing interactive architecture diagram
│   └── js/
│       ├── app.js              # State management & table controller
│       ├── flowchart.js        # Flowchart animation & telemetry engine
│       ├── tenant-drawer.js    # Multi-channel dossier inspector
│       └── simulator.js        # Interactive tenant reply sandbox
└── tests/
    ├── test_rules.py           # Unit tests for business rules
    ├── test_classifier.py      # Unit tests for AI intent classification
    └── test_workflow.py        # End-to-end integration tests
```

---

## 7. Business Impact

- **Staff Time on Reminders:** Reduced from **2–4 hours/day** down to **~15 minutes** (daily report review).
- **Tenant Experience:** Harassment eliminated through structured payment promise tracking.
- **Consistency:** 100% portfolio coverage every day at 08:00 UTC.
- **Audit Governance:** Complete timestamped records across RentVine, Quo SMS, and SQLite.



 python main.py serve