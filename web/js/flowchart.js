/**
 * Red Bricks Property Management - Flowchart Interactive Engine
 * Controls visual execution pulses, node telemetry clicks, and animated data flows.
 */

class FlowchartController {
  constructor() {
    this.board = document.getElementById("flowchart-board");
    this.consoleOutput = document.getElementById("workflow-console-output");
    this.terminalTag = document.getElementById("terminal-status-tag");
    this.initNodeClickListeners();
  }

  initNodeClickListeners() {
    const nodes = document.querySelectorAll(".flow-node");
    nodes.forEach(node => {
      node.addEventListener("click", () => {
        const nodeType = node.getAttribute("data-node");
        this.inspectNode(nodeType);
      });
    });
  }

  logTerminal(message, type = "info") {
    if (!this.consoleOutput) return;
    const line = document.createElement("div");
    const ts = new Date().toISOString().substring(11, 19);
    line.className = `log-line text-${type}`;
    line.textContent = `[${ts}] ${message}`;
    this.consoleOutput.appendChild(line);
    this.consoleOutput.scrollTop = this.consoleOutput.scrollHeight;
  }

  inspectNode(nodeType) {
    const details = {
      azure: "☁️ Azure Scheduler: Cron '0 8 * * *' (Every morning at 08:00 UTC). Runs fully unattended on Azure Functions serverless runtime.",
      agent: "⚙️ Python Automation Agent: Stateless automation core. Evaluates RentVine financials, SMS replies, and business rules without ChatGPT dependencies.",
      rentvine: "🏠 RentVine MCP: Protocol: Model Context Protocol (JSON-RPC 2.0). Read-only balance checking & automated note write-backs to tenant profiles.",
      quo: "💬 Quo (formerly OpenPhone) REST API: Accesses tenant SMS conversation threads and dispatches personalized templated payment reminders.",
      gmail: "✉️ Gmail API: Read-only OAuth 2.0 monitoring for resident email replies, dispute statements, and payment confirmations.",
      rules: "⚡ AI + Business Rules: Hybrid engine checking 3-day grace period, 24-hr deduplication cooldown, manual exclusions, and NLP intent parsing.",
      "outcome-paid": "✅ Rent Already Paid: Balance $0.00 or payment cleared. Action: SKIP reminder to prevent tenant irritation.",
      "outcome-promise": "🛡️ Valid Payment Promise: Extracted commitment date on file. Action: HOLD reminders and update RentVine profile note.",
      "outcome-unpaid": "✉️ Unpaid & Eligible: Balance overdue, past grace period, no active promise. Action: Dispatch Quo SMS with portal link.",
      results: "📊 Results Layer: SQLite audit trail, 3x exponential backoff retry queue, and automated daily executive reporting."
    };

    const text = details[nodeType] || "Component operational.";
    this.logTerminal(`[NODE INSPECT] ${text}`, "info");
  }

  async animateExecution(isDryRun = false, summary = null) {
    if (this.terminalTag) {
      this.terminalTag.textContent = isDryRun ? "SIMULATING" : "EXECUTING";
      this.terminalTag.style.color = isDryRun ? "#fbbf24" : "#38bdf8";
    }

    const stepDelay = (ms) => new Promise(r => setTimeout(r, ms));

    // Clear and start terminal stream
    this.logTerminal(`=== TRIGGER: Azure Scheduler initiated daily workflow cycle ===`, "warning");
    const nodeAzure = document.getElementById("node-azure");
    if (nodeAzure) nodeAzure.classList.add("executing-pulse");
    await stepDelay(400);

    // Layer 2: Agent
    this.logTerminal(`[CORE] Python Automation Agent spawned. Ingesting active portfolio...`, "info");
    const nodeAgent = document.getElementById("node-agent");
    if (nodeAgent) nodeAgent.classList.add("executing-pulse");
    await stepDelay(500);

    // Layer 3: Data Sources
    this.logTerminal(`[INGEST] Querying RentVine MCP (balances/notes), Quo REST (SMS history), and Gmail API...`, "info");
    const dataNodes = [document.getElementById("node-rentvine"), document.getElementById("node-quo"), document.getElementById("node-gmail")];
    dataNodes.forEach(n => n && n.classList.add("executing-pulse"));
    await stepDelay(600);

    // Layer 4: AI & Rules Engine
    this.logTerminal(`[DECISION] Evaluating deterministic business rules & running NLP Intent Classifier...`, "warning");
    const nodeRules = document.getElementById("node-rules");
    if (nodeRules) nodeRules.classList.add("executing-pulse");
    await stepDelay(600);

    // Layer 5: Outcome Diamonds
    const diamondPaid = document.getElementById("node-outcome-paid");
    const diamondPromise = document.getElementById("node-outcome-promise");
    const diamondUnpaid = document.getElementById("node-outcome-unpaid");

    if (diamondPaid) diamondPaid.classList.add("executing-pulse");
    if (diamondPromise) diamondPromise.classList.add("executing-pulse");
    if (diamondUnpaid) diamondUnpaid.classList.add("executing-pulse");

    if (summary) {
      document.getElementById("count-diamond-paid").textContent = `${summary.count_paid} units`;
      document.getElementById("count-diamond-promise").textContent = `${summary.count_promised} units`;
      document.getElementById("count-diamond-unpaid").textContent = `${summary.count_reminded} units`;

      this.logTerminal(`[OUTCOMES] Paid: ${summary.count_paid} | Promises: ${summary.count_promised} | Reminded: ${summary.count_reminded} | Flagged: ${summary.count_flagged}`, "success");
      if (isDryRun) {
        this.logTerminal(`[DRY-RUN] Reminders calculated but SMS send suppressed. Audit log updated.`, "warning");
      } else {
        this.logTerminal(`[DEMO MODE] ${summary.count_reminded} reminders needed — Live SMS send skipped (Testing Mode active).`, "warning");
      }
    }
    await stepDelay(500);

    // Layer 6: Results
    const nodeResults = document.getElementById("node-results");
    if (nodeResults) nodeResults.classList.add("executing-pulse");
    this.logTerminal(`[RESULTS LAYER] Audit logs committed to SQLite. Daily Executive Report generated.`, "success");
    await stepDelay(800);

    // Remove pulses
    document.querySelectorAll(".flow-node").forEach(n => n.classList.remove("executing-pulse"));

    if (this.terminalTag) {
      this.terminalTag.textContent = "IDLE";
      this.terminalTag.style.color = "var(--cyan-primary)";
    }
  }
}

window.flowchartCtrl = new FlowchartController();
