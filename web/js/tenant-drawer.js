/**
 * Red Bricks Property Management - Tenant Dossier Flyout Drawer
 * Multi-channel inspection: RentVine financial record, MCP notes, Quo SMS chat, and Gmail.
 */

class TenantDrawerController {
  constructor() {
    this.drawer = document.getElementById("tenant-drawer");
    this.backdrop = document.getElementById("drawer-backdrop");
    this.currentTenantId = null;
    this.initListeners();
  }

  initListeners() {
    const closeBtn = document.getElementById("drawer-close-btn");
    if (closeBtn) closeBtn.addEventListener("click", () => this.close());
    if (this.backdrop) this.backdrop.addEventListener("click", () => this.close());

    // Drawer Tabs switching
    const drawerTabs = document.querySelectorAll(".drawer-tab");
    drawerTabs.forEach(tab => {
      tab.addEventListener("click", () => {
        drawerTabs.forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        const targetView = tab.getAttribute("data-drawer-tab");
        
        // Hide all panes
        document.querySelectorAll(".drawer-tab-pane, .drawer-view").forEach(v => v.classList.remove("active"));
        
        // Show target pane (checking both drawer-pane- and drawer-view- prefixes)
        const activePane = document.getElementById(`drawer-pane-${targetView}`) || document.getElementById(`drawer-view-${targetView}`);
        if (activePane) activePane.classList.add("active");
      });
    });

    // Add manual note button (supporting both IDs)
    const btnAddNote = document.getElementById("drawer-btn-add-note") || document.getElementById("btn-add-manual-note");
    if (btnAddNote) {
      btnAddNote.addEventListener("click", () => this.handleManualNoteSubmit());
    }

    // Send manual SMS button (supporting both IDs)
    const btnSendSms = document.getElementById("drawer-btn-send-sms") || document.getElementById("btn-send-manual-sms");
    if (btnSendSms) {
      btnSendSms.addEventListener("click", () => this.handleManualSmsSubmit());
    }
  }

  async open(tenantId) {
    this.currentTenantId = tenantId;
    if (!this.drawer) this.drawer = document.getElementById("tenant-drawer");
    if (!this.backdrop) this.backdrop = document.getElementById("drawer-backdrop");
    if (this.drawer) this.drawer.classList.add("open");
    if (this.backdrop) this.backdrop.classList.add("open");
    await this.loadTenantDetails(tenantId);
  }

  close() {
    if (this.drawer) this.drawer.classList.remove("open");
    if (this.backdrop) this.backdrop.classList.remove("open");
    this.currentTenantId = null;
  }

  async loadTenantDetails(tenantId) {
    try {
      const res = await fetch(`/api/tenant/${tenantId}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to load tenant");

      this.renderProfile(data);
    } catch (err) {
      console.error("Error loading tenant details:", err);
      alert("Could not load tenant details: " + err.message);
    }
  }

  renderProfile(data) {
    const t = data.tenant || {};
    const rv = data.rentvine || {};
    const comms = data.communications || [];
    const decisions = data.decisions || [];

    // Header
    const nameEl = document.getElementById("drawer-name");
    if (nameEl) nameEl.textContent = t.full_name || "Unknown Tenant";
    
    const propEl = document.getElementById("drawer-property");
    if (propEl) {
      propEl.textContent = `${t.property_name || "Property"} • ${t.unit_number || "Unit"}${t.phone ? ' • ' + t.phone : ''}`;
    }

    // 1. RentVine Financials (Checking both potential DOM IDs)
    const balEl = document.getElementById("drawer-rv-balance") || document.getElementById("drawer-balance");
    if (balEl) {
      const bal = rv.balance || 0;
      balEl.textContent = `$${bal.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
      balEl.style.color = bal <= 0 ? "#34d399" : (bal > 1500 ? "#f87171" : "#fbbf24");
    }

    const monEl = document.getElementById("drawer-rv-rent") || document.getElementById("drawer-monthly");
    if (monEl) {
      monEl.textContent = `$${(rv.monthly_rent || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    }

    const dueEl = document.getElementById("drawer-rv-due") || document.getElementById("drawer-due-date");
    if (dueEl) {
      dueEl.textContent = rv.due_date || "1st of month";
    }

    const statusEl = document.getElementById("drawer-rv-status");
    if (statusEl) {
      statusEl.textContent = rv.lease_status || (rv.balance <= 0 ? "PAID" : "ACTIVE");
      statusEl.style.color = rv.balance <= 0 ? "#34d399" : "#38bdf8";
    }

    const lastPayEl = document.getElementById("drawer-last-payment");
    if (lastPayEl) {
      lastPayEl.textContent = rv.last_payment_date ? `$${(rv.last_payment_amount || 0).toLocaleString()} on ${rv.last_payment_date}` : "None this cycle";
    }

    // RentVine Notes Timeline
    const notesContainer = document.getElementById("drawer-rv-notes") || document.getElementById("drawer-notes-list");
    if (notesContainer) {
      notesContainer.innerHTML = "";
      const notes = rv.notes || [];
      if (notes.length === 0) {
        notesContainer.innerHTML = `<div class="text-muted" style="padding:10px 0;font-size:13px;">No notes on file in RentVine.</div>`;
      } else {
        notes.forEach(n => {
          const item = document.createElement("div");
          item.className = "timeline-note";
          item.style.cssText = "margin-bottom:10px;padding:10px;background:#1e293b;border-radius:8px;border-left:3px solid #38bdf8;";
          const dateStr = n.created_at ? n.created_at.replace("T", " ").substring(0, 16) : "";
          item.innerHTML = `
            <div class="note-meta" style="display:flex;justify-content:space-between;font-size:11px;color:#94a3b8;margin-bottom:4px;">
              <span class="note-author" style="font-weight:700;color:#38bdf8;">${n.author || "Agent"}</span>
              <span>${dateStr}</span>
            </div>
            <div class="note-text" style="font-size:13px;color:#e2e8f0;line-height:1.4;">${n.text || ""}</div>
          `;
          notesContainer.appendChild(item);
        });
      }
    }

    // 2. Quo SMS Chat bubbles
    const smsContainer = document.getElementById("drawer-quo-thread") || document.getElementById("drawer-chat-bubbles");
    if (smsContainer) {
      smsContainer.innerHTML = "";
      const smsMessages = comms.filter(m => m.channel === "SMS");
      if (smsMessages.length === 0) {
        smsContainer.innerHTML = `<div class="text-muted" style="text-align:center;padding:30px 0;font-size:13px;">No SMS message history with ${t.phone || "this tenant"}.</div>`;
      } else {
        smsMessages.forEach(m => {
          const bubble = document.createElement("div");
          const isOut = m.direction === "OUTBOUND";
          bubble.className = `chat-bubble ${isOut ? 'outbound' : 'inbound'}`;
          bubble.style.cssText = isOut 
            ? "margin-left:auto;background:#0284c7;color:#fff;padding:10px 14px;border-radius:12px 12px 2px 12px;max-width:85%;margin-bottom:10px;"
            : "margin-right:auto;background:#334155;color:#f1f5f9;padding:10px 14px;border-radius:12px 12px 12px 2px;max-width:85%;margin-bottom:10px;";
          const timeStr = m.timestamp ? m.timestamp.replace("T", " ").substring(0, 16) : "";
          bubble.innerHTML = `
            <div style="font-size:13px;line-height:1.55;white-space:pre-wrap;word-break:break-word;">${m.content}</div>
            <div class="bubble-meta" style="font-size:10px;opacity:0.8;margin-top:4px;text-align:right;">${timeStr} • ${m.status || "SENT"}</div>
          `;
          smsContainer.appendChild(bubble);
        });
        smsContainer.scrollTop = smsContainer.scrollHeight;
      }
    }

    // 3. Gmail Threads
    const emailContainer = document.getElementById("drawer-email-thread");
    if (emailContainer) {
      emailContainer.innerHTML = "";
      const emailMessages = comms.filter(m => m.channel === "EMAIL");
      if (emailMessages.length === 0) {
        emailContainer.innerHTML = `<div class="text-muted" style="padding:20px 0;font-size:13px;">No email replies recorded from ${t.email || "this tenant"}.</div>`;
      } else {
        emailMessages.forEach(m => {
          const card = document.createElement("div");
          card.className = "timeline-note";
          card.style.cssText = "margin-bottom:10px;padding:10px;background:#1e293b;border-radius:8px;border-left:3px solid #a855f7;";
          const timeStr = m.timestamp ? m.timestamp.replace("T", " ").substring(0, 16) : "";
          card.innerHTML = `
            <div class="note-meta" style="display:flex;justify-content:space-between;font-size:11px;color:#c084fc;margin-bottom:4px;font-weight:700;">
              <span>From: ${m.sender || t.email}</span>
              <span style="color:#94a3b8;font-weight:normal;">${timeStr}</span>
            </div>
            <div class="note-text" style="white-space:pre-wrap;font-size:13px;color:#e2e8f0;line-height:1.4;">${m.content}</div>
          `;
          emailContainer.appendChild(card);
        });
      }
    }

    // 4. Decision History (Audit Trail)
    const auditContainer = document.getElementById("drawer-audit-log") || document.getElementById("drawer-audit-list");
    if (auditContainer) {
      auditContainer.innerHTML = "";
      if (decisions.length === 0) {
        auditContainer.innerHTML = `<div class="text-muted" style="padding:20px 0;font-size:13px;">No prior automated workflow decisions recorded for this tenant.</div>`;
      } else {
        decisions.forEach(d => {
          const item = document.createElement("div");
          item.className = "timeline-note";
          const borderColor = d.outcome === "SEND_REMINDER" ? "#f87171" : (d.outcome === "HOLD_PROMISE" ? "#fbbf24" : (d.outcome === "SKIP_PAID" ? "#34d399" : "#94a3b8"));
          item.style.cssText = `margin-bottom:10px;padding:10px;background:#1e293b;border-radius:8px;border-left:3px solid ${borderColor};`;
          item.innerHTML = `
            <div class="note-meta" style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
              <span style="font-weight:700;color:${borderColor};">${d.outcome}</span>
              <span style="color:#94a3b8;font-size:11px;">${d.timestamp ? d.timestamp.substring(0, 19).replace('T', ' ') : ''}</span>
            </div>
            <div class="note-text" style="font-size:13px;color:#e2e8f0;line-height:1.4;">${d.reason || "Evaluated by AI Rules Engine"}</div>
            ${d.sms_sent ? '<div style="font-size:11px;color:#38bdf8;margin-top:4px;font-weight:600;">✓ SMS Reminder Dispatched</div>' : ''}
            ${d.note_written ? '<div style="font-size:11px;color:#a78bfa;margin-top:2px;">✓ RentVine Note Recorded</div>' : ''}
          `;
          auditContainer.appendChild(item);
        });
      }
    }
  }

  async handleManualNoteSubmit() {
    const input = document.getElementById("drawer-input-new-note") || document.getElementById("manual-note-input");
    const text = input ? input.value.trim() : "";
    if (!text || !this.currentTenantId) return;

    try {
      const res = await fetch("/api/add-note", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenant_id: this.currentTenantId,
          text: text,
          author: "Property Manager"
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      input.value = "";
      await this.loadTenantDetails(this.currentTenantId);
      if (window.flowchartCtrl) {
        window.flowchartCtrl.logTerminal(`[RENTVINE NOTE ADDED] Manual note recorded for tenant ${this.currentTenantId}`, "success");
      }
    } catch (err) {
      alert("Failed to write note: " + err.message);
    }
  }

  async handleManualSmsSubmit() {
    const input = document.getElementById("drawer-manual-sms-input") || document.getElementById("manual-sms-input");
    const text = input ? input.value.trim() : "";
    if (!text || !this.currentTenantId) return;

    try {
      const res = await fetch("/api/send-sms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenant_id: this.currentTenantId,
          text: text
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      input.value = "";
      await this.loadTenantDetails(this.currentTenantId);
      if (window.flowchartCtrl) {
        window.flowchartCtrl.logTerminal(`[QUO SMS SENT] Manual text delivered to tenant ${this.currentTenantId}`, "success");
      }
    } catch (err) {
      alert("Failed to send SMS: " + err.message);
    }
  }
}

window.tenantDrawerCtrl = new TenantDrawerController();
