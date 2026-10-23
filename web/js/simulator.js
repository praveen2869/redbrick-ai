/**
 * Red Bricks Property Management - Tenant Reply Simulator Controller
 * Interactive sandbox for testing arbitrary text messages against AI NLP and business rules.
 */

class SimulatorController {
  constructor() {
    this.tenantSelect = document.getElementById("sim-tenant-select");
    this.messageInput = document.getElementById("sim-message-text");
    this.btnRun = document.getElementById("btn-run-simulation");
    this.resultsEmpty = document.getElementById("sim-results-empty");
    this.resultsContent = document.getElementById("sim-results-content");

    this.initListeners();
  }

  initListeners() {
    if (this.btnRun) {
      this.btnRun.addEventListener("click", () => this.executeSimulation());
    }

    // Quick presets
    const presetButtons = document.querySelectorAll(".btn-preset");
    presetButtons.forEach(btn => {
      btn.addEventListener("click", () => {
        const text = btn.getAttribute("data-text");
        if (this.messageInput) {
          this.messageInput.value = text;
        }
      });
    });
  }

  populateTenants(tenants) {
    if (!this.tenantSelect) return;
    this.tenantSelect.innerHTML = "";
    tenants.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = `${t.full_name} (${t.property_name} ${t.unit_number}) - Bal: $${t.balance.toLocaleString()}`;
      this.tenantSelect.appendChild(opt);
    });
  }

  async executeSimulation() {
    const tenantId = this.tenantSelect ? this.tenantSelect.value : null;
    const text = this.messageInput ? this.messageInput.value.trim() : "";
    const channelInput = document.querySelector('input[name="sim-channel"]:checked');
    const channel = channelInput ? channelInput.value : "SMS";

    if (!tenantId || !text) {
      alert("Please select a tenant and provide message text to simulate.");
      return;
    }

    this.btnRun.disabled = true;
    this.btnRun.textContent = "Analyzing Intent with AI Engine...";

    try {
      const res = await fetch("/api/simulate-reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenant_id: tenantId,
          text: text,
          channel: channel,
          apply_to_db: true
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Simulation failed");

      this.renderResults(data);

      // Refresh main dashboard and table data so changes reflect live
      if (window.appCtrl) {
        window.appCtrl.refreshData(false);
      }
      if (window.flowchartCtrl) {
        window.flowchartCtrl.logTerminal(
          `[AI SIMULATION] Tenant ${tenantId} reply parsed: ${data.classification.intent} (${data.decision_preview.outcome})`,
          "warning"
        );
      }
    } catch (err) {
      alert("Simulation error: " + err.message);
    } finally {
      this.btnRun.disabled = false;
      this.btnRun.innerHTML = `<span>⚡ Run AI Classifier & Decision Engine</span>`;
    }
  }

  renderResults(data) {
    const c = data.classification;
    const d = data.decision_preview;

    if (this.resultsEmpty) this.resultsEmpty.classList.add("hidden");
    if (this.resultsContent) this.resultsContent.classList.remove("hidden");

    // Metrics
    const intentElem = document.getElementById("sim-result-intent");
    intentElem.textContent = c.intent;
    intentElem.className = `sim-badge ${this.getIntentClass(c.intent)}`;

    document.getElementById("sim-result-confidence").textContent = `${Math.round(c.confidence * 100)}%`;
    document.getElementById("sim-result-date").textContent = c.extracted_date || "None Detected";

    // Outcome Box
    const outcomeBox = document.getElementById("sim-result-outcome-box");
    const outcomeIcon = document.getElementById("sim-result-outcome-icon");
    const outcomeTitle = document.getElementById("sim-result-outcome-title");
    const outcomeDesc = document.getElementById("sim-result-outcome-desc");

    outcomeTitle.textContent = `${d.outcome} — ${d.action}`;
    outcomeDesc.textContent = d.reason;

    if (d.outcome === "HOLD_PROMISE") {
      outcomeIcon.textContent = "🛡️";
      outcomeBox.style.background = "rgba(245, 158, 11, 0.12)";
      outcomeBox.style.borderColor = "rgba(245, 158, 11, 0.4)";
      outcomeTitle.style.color = "#fbbf24";
    } else if (d.outcome === "FLAG_MANUAL_REVIEW") {
      outcomeIcon.textContent = "🔴";
      outcomeBox.style.background = "rgba(225, 29, 72, 0.15)";
      outcomeBox.style.borderColor = "rgba(225, 29, 72, 0.4)";
      outcomeTitle.style.color = "#fb7185";
    } else if (d.outcome === "SKIP_PAID") {
      outcomeIcon.textContent = "✅";
      outcomeBox.style.background = "rgba(16, 185, 129, 0.12)";
      outcomeBox.style.borderColor = "rgba(16, 185, 129, 0.4)";
      outcomeTitle.style.color = "#34d399";
    } else {
      outcomeIcon.textContent = "ℹ️";
      outcomeBox.style.background = "rgba(6, 182, 212, 0.12)";
      outcomeBox.style.borderColor = "rgba(6, 182, 212, 0.4)";
      outcomeTitle.style.color = "#38bdf8";
    }

    // RentVine Note
    const noteElem = document.getElementById("sim-result-note-text");
    if (data.note_text) {
      noteElem.textContent = data.note_text;
    } else {
      noteElem.textContent = "No note written for this intent category.";
    }

    // Reasoning
    document.getElementById("sim-result-reasoning").textContent = c.reasoning;
  }

  getIntentClass(intent) {
    if (intent === "PROMISE_MADE") return "highlight-amber";
    if (intent === "DISPUTE") return "highlight-rose";
    if (intent === "ALREADY_PAID") return "highlight-emerald";
    return "highlight-cyan";
  }
}

window.simulatorCtrl = new SimulatorController();
