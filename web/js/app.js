/**
 * Red Bricks Property Management - Main Application Controller
 * Coordinates API fetching, KPI cards, table rendering, tab routing,
 * and multi-page pagination for live RentVine & Quo feeds (zero dummy data).
 */

class AppController {
  constructor() {
    this.tenants = [];
    this.currentFilter = "ALL";
    this.searchQuery = "";
    this.selectedProperty = "ALL";

    // Main portfolio table pagination state
    this.tenantPage = 1;
    this.tenantPageSize = 25;

    // RentVine Live pagination states
    this.rvProps = { page: 1, pageSize: 25, search: "", totalItems: 0, totalPages: 1 };
    this.rvTens = { page: 1, pageSize: 25, search: "", totalItems: 0, totalPages: 1 };
    this.rvLeases = { page: 1, pageSize: 25, search: "", totalItems: 0, totalPages: 1 };

    this.initElements();
    this.initEventListeners();
    this.initRentVinePaginationListeners();

    // Initial load
    this.refreshData(true);
    this.loadRentVineOverview();
    this.loadQuoData();
  }

  initElements() {
    this.tableBody = document.getElementById("tenants-table-body");
    this.searchInput = document.getElementById("tenant-search");
    this.propertySelect = document.getElementById("filter-property");
    this.filterPills = document.querySelectorAll(".filter-pill");
    this.navTabs = document.querySelectorAll(".nav-tab");
    this.tabContents = document.querySelectorAll(".tab-content");

    // Action buttons
    this.btnRun = document.getElementById("btn-run-workflow");
    this.btnDryRun = document.getElementById("btn-dry-run");
    this.btnReport = document.getElementById("btn-view-report");
    this.btnReset = document.getElementById("btn-reset-seed");

    // Modal
    this.reportModal = document.getElementById("report-modal");
    this.reportClose = document.getElementById("report-modal-close");
    this.reportPrint = document.getElementById("btn-print-report");
  }

  initEventListeners() {
    // Tab switching
    this.navTabs.forEach(tab => {
      tab.addEventListener("click", () => {
        this.navTabs.forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        const target = tab.getAttribute("data-tab");
        this.tabContents.forEach(c => c.classList.remove("active"));
        const activeContent = document.getElementById(`tab-${target}`);
        if (activeContent) activeContent.classList.add("active");

        if (target === "rentvine-live") {
          this.loadRentVineOverview();
        }
        if (target === "quo-live") {
          this.loadQuoData();
        }
      });
    });

    const btnRefreshRv = document.getElementById("btn-refresh-rentvine");
    if (btnRefreshRv) {
      btnRefreshRv.addEventListener("click", () => this.loadRentVineOverview());
    }

    const btnRefreshQuo = document.getElementById("btn-refresh-quo");
    if (btnRefreshQuo) {
      btnRefreshQuo.addEventListener("click", () => this.loadQuoData());
    }

    // Main portfolio table filters
    this.filterPills.forEach(pill => {
      pill.addEventListener("click", () => {
        this.filterPills.forEach(p => p.classList.remove("active"));
        pill.classList.add("active");
        this.currentFilter = pill.getAttribute("data-filter");
        this.tenantPage = 1;
        this.renderTable();
      });
    });

    if (this.searchInput) {
      this.searchInput.addEventListener("input", (e) => {
        this.searchQuery = e.target.value.toLowerCase();
        this.tenantPage = 1;
        this.renderTable();
      });
    }

    if (this.propertySelect) {
      this.propertySelect.addEventListener("change", (e) => {
        this.selectedProperty = e.target.value;
        this.tenantPage = 1;
        this.renderTable();
      });
    }

    // Main portfolio table pagination buttons
    const btnTenantsFirst = document.getElementById("btn-tenants-first");
    const btnTenantsPrev = document.getElementById("btn-tenants-prev");
    const btnTenantsNext = document.getElementById("btn-tenants-next");
    const btnTenantsLast = document.getElementById("btn-tenants-last");
    const selTenantsPageSize = document.getElementById("tenants-page-size");

    if (btnTenantsFirst) {
      btnTenantsFirst.addEventListener("click", () => {
        if (this.tenantPage > 1) {
          this.tenantPage = 1;
          this.renderTable();
        }
      });
    }
    if (btnTenantsPrev) {
      btnTenantsPrev.addEventListener("click", () => {
        if (this.tenantPage > 1) {
          this.tenantPage--;
          this.renderTable();
        }
      });
    }
    if (btnTenantsNext) {
      btnTenantsNext.addEventListener("click", () => {
        this.tenantPage++;
        this.renderTable();
      });
    }
    if (btnTenantsLast) {
      btnTenantsLast.addEventListener("click", () => {
        this.tenantPage = 999999;
        this.renderTable();
      });
    }
    if (selTenantsPageSize) {
      selTenantsPageSize.addEventListener("change", (e) => {
        const val = e.target.value;
        this.tenantPageSize = val === "ALL" ? 999999 : parseInt(val, 10);
        this.tenantPage = 1;
        this.renderTable();
      });
    }

    // Workflow actions
    if (this.btnRun) {
      this.btnRun.addEventListener("click", () => this.runWorkflow(false));
    }
    if (this.btnDryRun) {
      this.btnDryRun.addEventListener("click", () => this.runWorkflow(true));
    }
    if (this.btnReset) {
      this.btnReset.addEventListener("click", () => this.resetSeedData());
    }
    if (this.btnReport) {
      this.btnReport.addEventListener("click", () => this.openReportModal());
    }
    if (this.reportClose) {
      this.reportClose.addEventListener("click", () => this.reportModal.classList.remove("open"));
    }
    if (this.reportPrint) {
      this.reportPrint.addEventListener("click", () => window.print());
    }

    // Settings save
    const btnSaveSettings = document.getElementById("btn-save-settings");
    if (btnSaveSettings) {
      btnSaveSettings.addEventListener("click", () => this.saveSettings());
    }
  }

  initRentVinePaginationListeners() {
    // 1. Properties section
    this._bindRvSectionListeners(
      "properties",
      this.rvProps,
      "rv-props-search",
      "rv-props-pagesize",
      "btn-rv-props-first",
      "btn-rv-props-prev",
      "btn-rv-props-next",
      "btn-rv-props-last"
    );

    // 2. Tenants section
    this._bindRvSectionListeners(
      "tenants",
      this.rvTens,
      "rv-tens-search",
      "rv-tens-pagesize",
      "btn-rv-tens-first",
      "btn-rv-tens-prev",
      "btn-rv-tens-next",
      "btn-rv-tens-last"
    );

    // 3. Leases section
    this._bindRvSectionListeners(
      "leases",
      this.rvLeases,
      "rv-leases-search",
      "rv-leases-pagesize",
      "btn-rv-leases-first",
      "btn-rv-leases-prev",
      "btn-rv-leases-next",
      "btn-rv-leases-last"
    );

    // Quick scroll cards
    const cardProps = document.getElementById("card-rv-props");
    if (cardProps) {
      cardProps.addEventListener("click", () => {
        document.getElementById("rv-properties-table")?.scrollIntoView({ behavior: "smooth" });
      });
    }
    const cardTens = document.getElementById("card-rv-tenants");
    if (cardTens) {
      cardTens.addEventListener("click", () => {
        document.getElementById("rv-tenants-table")?.scrollIntoView({ behavior: "smooth" });
      });
    }
    const cardLeases = document.getElementById("card-rv-leases");
    if (cardLeases) {
      cardLeases.addEventListener("click", () => {
        document.getElementById("rv-leases-table")?.scrollIntoView({ behavior: "smooth" });
      });
    }
  }

  _bindRvSectionListeners(section, stateObj, searchId, sizeId, firstId, prevId, nextId, lastId) {
    let debounceTimer = null;
    const searchInput = document.getElementById(searchId);
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
          stateObj.search = e.target.value.trim();
          stateObj.page = 1;
          this.loadRentVineSection(section);
        }, 300);
      });
    }

    const sizeSelect = document.getElementById(sizeId);
    if (sizeSelect) {
      sizeSelect.addEventListener("change", (e) => {
        stateObj.pageSize = parseInt(e.target.value, 10) || 25;
        stateObj.page = 1;
        this.loadRentVineSection(section);
      });
    }

    const btnFirst = document.getElementById(firstId);
    if (btnFirst) {
      btnFirst.addEventListener("click", () => {
        if (stateObj.page > 1) {
          stateObj.page = 1;
          this.loadRentVineSection(section);
        }
      });
    }

    const btnPrev = document.getElementById(prevId);
    if (btnPrev) {
      btnPrev.addEventListener("click", () => {
        if (stateObj.page > 1) {
          stateObj.page--;
          this.loadRentVineSection(section);
        }
      });
    }

    const btnNext = document.getElementById(nextId);
    if (btnNext) {
      btnNext.addEventListener("click", () => {
        if (stateObj.page < stateObj.totalPages) {
          stateObj.page++;
          this.loadRentVineSection(section);
        }
      });
    }

    const btnLast = document.getElementById(lastId);
    if (btnLast) {
      btnLast.addEventListener("click", () => {
        if (stateObj.page < stateObj.totalPages) {
          stateObj.page = stateObj.totalPages;
          this.loadRentVineSection(section);
        }
      });
    }
  }

  async refreshData(initial = false) {
    try {
      const [tenantsRes, statusRes, runsRes] = await Promise.all([
        fetch("/api/tenants"),
        fetch("/api/status"),
        fetch("/api/runs")
      ]);

      const tenantsData = await tenantsRes.json();
      const statusData = await statusRes.json();
      const runsData = await runsRes.json();

      this.tenants = tenantsData.tenants || [];
      this.populateDynamicProperties();
      this.updateKPIs(statusData.latest_run);
      this.renderTable();
      this.renderRuns(runsData.runs || []);

      if (window.simulatorCtrl) {
        window.simulatorCtrl.populateTenants(this.tenants);
      }

      const totalBadge = document.getElementById("badge-total-tenants");
      const activeCount = this.tenants.filter(t => t.lease_status !== "COMPLETED").length;
      if (totalBadge) totalBadge.textContent = activeCount || this.tenants.length;

      if (initial && window.flowchartCtrl && statusData.latest_run) {
        const lr = statusData.latest_run;
        const dPaid = document.getElementById("count-diamond-paid");
        const dPromise = document.getElementById("count-diamond-promise");
        const dUnpaid = document.getElementById("count-diamond-unpaid");
        if (dPaid) dPaid.textContent = `${lr.count_paid} units`;
        if (dPromise) dPromise.textContent = `${lr.count_promised} units`;
        if (dUnpaid) dUnpaid.textContent = `${lr.count_reminded} units`;
      }
    } catch (err) {
      console.error("Data refresh failed:", err);
    }
  }

  populateDynamicProperties() {
    if (!this.propertySelect) return;
    const currentVal = this.propertySelect.value;
    const properties = new Set();
    this.tenants.forEach(t => {
      if (t.property_name && t.property_name !== "Red Bricks Property") {
        properties.add(t.property_name);
      }
    });

    const sortedProps = Array.from(properties).sort();
    let optionsHtml = `<option value="ALL">All Properties (Live - ${sortedProps.length} complexes)</option>`;
    sortedProps.forEach(p => {
      optionsHtml += `<option value="${p}">${p}</option>`;
    });

    this.propertySelect.innerHTML = optionsHtml;
    if (sortedProps.includes(currentVal)) {
      this.propertySelect.value = currentVal;
    }
  }

  updateKPIs(latestRun) {
    let totalUnits = 0;
    let totalBal = 0;
    let countPaid = 0;
    let countPromises = 0;
    let countSent = 0;
    let countFlagged = 0;
    let countCooldown = 0;

    if (latestRun && typeof latestRun.total_tenants === "number" && latestRun.total_tenants > 0) {
      // Authoritative run summary from workflow execution
      totalUnits = latestRun.total_tenants;
      countPaid = latestRun.count_paid || 0;
      countPromises = latestRun.count_promised || 0;
      countSent = latestRun.count_reminded || 0;
      countFlagged = latestRun.count_flagged || 0;
      countCooldown = latestRun.count_skipped || 0;
      totalBal = latestRun.total_balance_unpaid || 0;
    } else {
      // Filter out completed leases to only show active units
      const activeTenants = this.tenants.filter(t => t.lease_status !== "COMPLETED");
      totalUnits = activeTenants.length;
      activeTenants.forEach(t => {
        if (t.balance > 0) totalBal += t.balance;
        const d = t.latest_decision;
        if (d) {
          if (d.outcome === "SKIP_PAID") countPaid++;
          else if (d.outcome === "HOLD_PROMISE") countPromises++;
          else if (d.outcome === "SEND_REMINDER") countSent++;
          else if (d.outcome === "FLAG_MANUAL_REVIEW") countFlagged++;
          else if (d.outcome === "SKIP_ALREADY_REMINDED" || d.outcome === "SKIP_GRACE_PERIOD") countCooldown++;
        }
      });
    }

    const valUnits = document.getElementById("val-total-units");
    const valOverdue = document.getElementById("val-overdue-balance");
    const valPaid = document.getElementById("val-count-paid");
    const valPromises = document.getElementById("val-count-promises");
    const valSent = document.getElementById("val-count-sent");
    const valSavings = document.getElementById("val-time-saved");

    if (valUnits) valUnits.textContent = totalUnits.toLocaleString();
    if (valOverdue) valOverdue.textContent = `$${Math.round(totalBal).toLocaleString()}`;
    if (valPaid) valPaid.textContent = countPaid.toLocaleString();
    if (valPromises) valPromises.textContent = countPromises.toLocaleString();
    if (valSent) valSent.textContent = countSent.toLocaleString();

    const hoursSaved = (totalUnits * 0.22).toFixed(1);
    if (valSavings) valSavings.textContent = `${hoursSaved} hrs`;

    // Synchronize flowchart outcome diamond counters
    const dPaid = document.getElementById("count-diamond-paid");
    const dPromise = document.getElementById("count-diamond-promise");
    const dUnpaid = document.getElementById("count-diamond-unpaid");
    if (dPaid) dPaid.textContent = `${countPaid} units`;
    if (dPromise) dPromise.textContent = `${countPromises} units`;
    if (dUnpaid) dUnpaid.textContent = `${countSent} units`;

    // Update filter pills with real dynamic counts
    const pillAll = document.getElementById("pill-filter-all");
    const pillReminded = document.getElementById("pill-filter-reminded");
    const pillPromised = document.getElementById("pill-filter-promised");
    const pillPaid = document.getElementById("pill-filter-paid");
    const pillFlagged = document.getElementById("pill-filter-flagged");
    const pillCooldown = document.getElementById("pill-filter-cooldown");

    if (pillAll) pillAll.textContent = `All Active (${totalUnits})`;
    if (pillReminded) pillReminded.textContent = `📩 Reminders Needed (${countSent})`;
    if (pillPromised) pillPromised.textContent = `🛡️ Promises on File (${countPromises})`;
    if (pillPaid) pillPaid.textContent = `✅ Paid / Cleared (${countPaid})`;
    if (pillFlagged) pillFlagged.textContent = `🔴 Flagged / Disputes (${countFlagged})`;
    if (pillCooldown) pillCooldown.textContent = `🚫 In Cooldown / Grace (${countCooldown})`;
  }

  renderTable() {
    if (!this.tableBody) return;
    this.tableBody.innerHTML = "";

    const filtered = this.tenants.filter(t => {
      // 1. Pill Filter
      if (this.currentFilter !== "ALL") {
        const outcome = t.latest_decision ? t.latest_decision.outcome : "PENDING";
        if (outcome !== this.currentFilter) return false;
      }
      // 2. Property Filter
      if (this.selectedProperty !== "ALL" && t.property_name !== this.selectedProperty) {
        return false;
      }
      // 3. Search Query
      if (this.searchQuery) {
        const matchName = t.full_name.toLowerCase().includes(this.searchQuery);
        const matchUnit = `${t.property_name} ${t.unit_number}`.toLowerCase().includes(this.searchQuery);
        const matchPhone = t.phone.toLowerCase().includes(this.searchQuery);
        if (!matchName && !matchUnit && !matchPhone) return false;
      }
      return true;
    });

    const totalFiltered = filtered.length;
    const totalPages = Math.max(1, Math.ceil(totalFiltered / this.tenantPageSize));
    if (this.tenantPage > totalPages) this.tenantPage = totalPages;
    if (this.tenantPage < 1) this.tenantPage = 1;

    // Slice for current page
    const startIndex = (this.tenantPage - 1) * this.tenantPageSize;
    const endIndex = Math.min(startIndex + this.tenantPageSize, totalFiltered);
    const pageItems = filtered.slice(startIndex, endIndex);

    // Update pagination controls
    const infoElem = document.getElementById("tenants-page-info");
    const curElem = document.getElementById("tenants-page-current");
    const btnFirst = document.getElementById("btn-tenants-first");
    const btnPrev = document.getElementById("btn-tenants-prev");
    const btnNext = document.getElementById("btn-tenants-next");
    const btnLast = document.getElementById("btn-tenants-last");

    if (infoElem) {
      if (totalFiltered === 0) {
        infoElem.textContent = "Showing 0 tenants";
      } else {
        infoElem.textContent = `Showing ${startIndex + 1}–${endIndex} of ${totalFiltered} active units (Page ${this.tenantPage} of ${totalPages})`;
      }
    }
    if (curElem) curElem.textContent = this.tenantPage;
    if (btnFirst) btnFirst.disabled = (this.tenantPage <= 1);
    if (btnPrev) btnPrev.disabled = (this.tenantPage <= 1);
    if (btnNext) btnNext.disabled = (this.tenantPage >= totalPages);
    if (btnLast) btnLast.disabled = (this.tenantPage >= totalPages);

    if (totalFiltered === 0) {
      this.tableBody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--text-muted);">No tenants found matching current filter criteria.</td></tr>`;
      return;
    }

    pageItems.forEach(t => {
      const tr = document.createElement("tr");
      const d = t.latest_decision;
      const outcome = d ? d.outcome : "EVALUATE";
      const reason = d ? d.reason : "Standing by for daily Azure Scheduler trigger";

      const badge = this.getOutcomeBadge(outcome);
      const isPaid = t.balance <= 0;
      const balColor = isPaid ? "color:#34d399;" : (t.balance > 2000 ? "color:#f87171;" : "color:#f8fafc;");

      const msgSnippet = t.latest_message ? `
        <div class="msg-snippet" title="${t.latest_message.content}">
          ${t.latest_message.direction === 'INBOUND' ? '📥' : '📤'} ${t.latest_message.content}
        </div>
      ` : `<span class="text-muted" style="font-size:11px;">No messages</span>`;

      tr.innerHTML = `
        <td>
          <div class="tenant-cell-name">
            <span>${t.full_name}</span>
            ${t.is_excluded ? '<span style="font-size:9px;background:#e11d4822;color:#fb7185;padding:2px 5px;border-radius:4px;border:1px solid #e11d4844;">EXCLUDED</span>' : ''}
          </div>
          <div class="tenant-cell-contact">${t.phone} • ${t.email}</div>
        </td>
        <td>
          <div style="font-weight:600;color:#f8fafc;">${t.property_name}</div>
          <div style="font-size:11.5px;color:var(--cyan-primary);">${t.unit_number}</div>
        </td>
        <td>
          <div class="balance-cell" style="${balColor}">$${t.balance.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
          <div style="font-size:11px;color:var(--text-muted);">Rent: $${t.monthly_rent.toLocaleString()}</div>
        </td>
        <td style="color:#94a3b8;font-size:12px;">${t.due_date || '1st of mo.'}</td>
        <td>${msgSnippet}</td>
        <td>${badge}</td>
        <td style="max-width:240px;font-size:12px;color:#cbd5e1;line-height:1.35;">${reason}</td>
        <td>
          <button class="btn btn-secondary btn-sm btn-inspect" data-tenant-id="${t.id}" title="Inspect full profile, RentVine notes & SMS">
            Inspect
          </button>
        </td>
      `;

      this.tableBody.appendChild(tr);
    });

    // Attach inspect click handlers
    document.querySelectorAll(".btn-inspect").forEach(btn => {
      btn.addEventListener("click", () => {
        const tid = btn.getAttribute("data-tenant-id");
        if (window.tenantDrawerCtrl) {
          window.tenantDrawerCtrl.open(tid);
        }
      });
    });
  }

  getOutcomeBadge(outcome) {
    if (outcome === "SKIP_PAID") {
      return `<span class="badge badge-paid">✅ Paid (Skip)</span>`;
    } else if (outcome === "HOLD_PROMISE") {
      return `<span class="badge badge-promise">🛡️ Promise (Hold)</span>`;
    } else if (outcome === "SEND_REMINDER") {
      return `<span class="badge badge-sent">📩 Quo SMS Sent</span>`;
    } else if (outcome === "SKIP_ALREADY_REMINDED") {
      return `<span class="badge badge-dedup">🚫 In Cooldown</span>`;
    } else if (outcome === "SKIP_GRACE_PERIOD") {
      return `<span class="badge badge-grace">🕒 In Grace Period</span>`;
    } else if (outcome === "FLAG_MANUAL_REVIEW") {
      return `<span class="badge badge-flag">🔴 Manual Review</span>`;
    }
    return `<span class="badge" style="background:#1e293b;color:#94a3b8;">Pending</span>`;
  }

  /* ─── LIVE RENTVINE PAGINATED DATA LOADER ─── */

  async loadRentVineOverview() {
    const btn = document.getElementById("btn-refresh-rentvine");
    if (btn) btn.innerHTML = `<span>⏳ Loading...</span>`;

    try {
      // 1. Probe total counts
      const resCounts = await fetch("/api/rentvine-data?section=counts");
      const countsData = await resCounts.json();

      if (countsData && !countsData.error) {
        const cProps = document.getElementById("rv-count-properties");
        const cTens = document.getElementById("rv-count-tenants");
        const cLeases = document.getElementById("rv-count-leases");
        if (cProps) cProps.textContent = (countsData.properties || 0).toLocaleString();
        if (cTens) cTens.textContent = (countsData.tenants || 0).toLocaleString();
        if (cLeases) cLeases.textContent = (countsData.leases || 0).toLocaleString();

        const bProps = document.getElementById("rv-props-total-badge");
        const bTens = document.getElementById("rv-tens-total-badge");
        const bLeases = document.getElementById("rv-leases-total-badge");
        if (bProps) bProps.textContent = `${(countsData.properties || 0).toLocaleString()} total`;
        if (bTens) bTens.textContent = `${(countsData.tenants || 0).toLocaleString()} total`;
        if (bLeases) bLeases.textContent = `${(countsData.leases || 0).toLocaleString()} total`;
      }

      // 2. Fetch page 1 for each of the 3 sections in parallel
      await Promise.all([
        this.loadRentVineSection("properties"),
        this.loadRentVineSection("tenants"),
        this.loadRentVineSection("leases")
      ]);
    } catch (err) {
      console.error("RentVine overview load error:", err);
    } finally {
      if (btn) btn.innerHTML = `<span>🔄 Refresh Live Counts</span>`;
    }
  }

  async loadRentVineSection(section) {
    let stateObj, tbodyId, pageInfoId, pageCurId, btnFirstId, btnPrevId, btnNextId, btnLastId;

    if (section === "properties") {
      stateObj = this.rvProps;
      tbodyId = "rv-properties-tbody";
      pageInfoId = "rv-props-page-info";
      pageCurId = "rv-props-page-current";
      btnFirstId = "btn-rv-props-first";
      btnPrevId = "btn-rv-props-prev";
      btnNextId = "btn-rv-props-next";
      btnLastId = "btn-rv-props-last";
    } else if (section === "tenants") {
      stateObj = this.rvTens;
      tbodyId = "rv-tenants-tbody";
      pageInfoId = "rv-tens-page-info";
      pageCurId = "rv-tens-page-current";
      btnFirstId = "btn-rv-tens-first";
      btnPrevId = "btn-rv-tens-prev";
      btnNextId = "btn-rv-tens-next";
      btnLastId = "btn-rv-tens-last";
    } else if (section === "leases") {
      stateObj = this.rvLeases;
      tbodyId = "rv-leases-tbody";
      pageInfoId = "rv-leases-page-info";
      pageCurId = "rv-leases-page-current";
      btnFirstId = "btn-rv-leases-first";
      btnPrevId = "btn-rv-leases-prev";
      btnNextId = "btn-rv-leases-next";
      btnLastId = "btn-rv-leases-last";
    }

    const tbody = document.getElementById(tbodyId);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:#94a3b8;">⏳ Fetching live ${section} from RentVine (Page ${stateObj.page})...</td></tr>`;
    }

    try {
      const url = `/api/rentvine-data?section=${section}&page=${stateObj.page}&pageSize=${stateObj.pageSize}&search=${encodeURIComponent(stateObj.search)}`;
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to load RentVine section");

      stateObj.totalItems = data.totalItems || 0;
      stateObj.totalPages = Math.max(1, data.totalPages || 1);
      stateObj.page = data.page || 1;

      // Update Pagination UI
      const infoElem = document.getElementById(pageInfoId);
      const curElem = document.getElementById(pageCurId);
      const btnFirst = document.getElementById(btnFirstId);
      const btnPrev = document.getElementById(btnPrevId);
      const btnNext = document.getElementById(btnNextId);
      const btnLast = document.getElementById(btnLastId);

      const startIdx = (stateObj.page - 1) * stateObj.pageSize + 1;
      const endIdx = Math.min(stateObj.page * stateObj.pageSize, stateObj.totalItems);

      if (infoElem) {
        if (stateObj.totalItems === 0) {
          infoElem.textContent = `No ${section} found matching search`;
        } else {
          infoElem.textContent = `Showing ${startIdx}–${endIdx} of ${stateObj.totalItems.toLocaleString()} ${section} (Page ${stateObj.page} of ${stateObj.totalPages})`;
        }
      }
      if (curElem) curElem.textContent = stateObj.page;
      if (btnFirst) btnFirst.disabled = (stateObj.page <= 1);
      if (btnPrev) btnPrev.disabled = (stateObj.page <= 1);
      if (btnNext) btnNext.disabled = (stateObj.page >= stateObj.totalPages);
      if (btnLast) btnLast.disabled = (stateObj.page >= stateObj.totalPages);

      // Render rows
      if (section === "properties") {
        this._renderPropertiesRows(tbody, data.items || []);
      } else if (section === "tenants") {
        this._renderTenantsRows(tbody, data.items || []);
      } else if (section === "leases") {
        this._renderLeasesRows(tbody, data.items || []);
      }
    } catch (err) {
      console.error(`Error loading ${section}:`, err);
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:20px;color:#ef4444;">Failed to fetch live ${section}: ${err.message}</td></tr>`;
      }
    }
  }

  _renderPropertiesRows(tbody, items) {
    if (!tbody) return;
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:24px;color:#94a3b8;">No properties match search criteria.</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(p => `
      <tr>
        <td><code>#${p.property_id || '-'}</code></td>
        <td><strong>${p.name || p.address}</strong>${p.name && p.address && p.name !== p.address ? `<br><small style="color:#64748b;">${p.address}</small>` : ''}</td>
        <td>${p.city || '-'}, ${p.state || '-'}</td>
        <td><code>${p.postal_code || '-'}</code></td>
        <td>
          <span class="badge" style="background:${p.is_active ? '#065f46' : '#334155'};color:${p.is_active ? '#6ee7b7' : '#94a3b8'};padding:3px 8px;border-radius:12px;font-size:11px;">
            ${p.is_active ? 'Active' : 'Inactive'}
          </span>
        </td>
      </tr>
    `).join("");
  }

  _renderTenantsRows(tbody, items) {
    if (!tbody) return;
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;padding:24px;color:#94a3b8;">No contacts match search criteria.</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(t => `
      <tr>
        <td><code>#${t.contact_id || '-'}</code></td>
        <td><strong>${t.name}</strong></td>
        <td>${t.email ? `<a href="mailto:${t.email}" style="color:#60a5fa;text-decoration:none;">${t.email}</a>` : '<span style="color:#64748b;">N/A</span>'}</td>
        <td>${t.phone ? `<a href="tel:${t.phone}" style="color:#60a5fa;text-decoration:none;">${t.phone}</a>` : '<span style="color:#64748b;">N/A</span>'}</td>
      </tr>
    `).join("");
  }

  _renderLeasesRows(tbody, items) {
    if (!tbody) return;
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:24px;color:#94a3b8;">No leases match search criteria.</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(l => {
      const isAct = l.status === "Active";
      return `
        <tr>
          <td><code>#${l.lease_id || '-'}</code></td>
          <td><strong>${(l.tenants || []).join(", ") || 'No Tenant'}</strong></td>
          <td>${l.property_name || '-'}${l.unit_id ? ` <small style="color:#38bdf8;">(Unit ${l.unit_id})</small>` : ''}</td>
          <td><strong style="color:${l.rent > 0 ? '#f87171' : '#94a3b8'};">$${l.rent.toLocaleString('en-US', {minimumFractionDigits: 2})}</strong></td>
          <td style="font-size:12px;color:#94a3b8;">${l.start_date} &rarr; ${l.end_date}</td>
          <td>
            <span class="badge" style="background:${isAct ? '#065f46' : '#334155'};color:${isAct ? '#6ee7b7' : '#94a3b8'};padding:3px 8px;border-radius:12px;font-size:11px;">
              ${l.status}
            </span>
          </td>
        </tr>
      `;
    }).join("");
  }

  /* ─── LIVE QUO OUTPUT (HITL) ─── */

  // Tracks state for Quo Reminders table
  _quoSentSet = new Set();
  _quoSentCount = 0;
  _quoReminders = [];
  _quoFilterText = "";
  _quoCurrentPage = 1;
  _quoPageSize = 8;
  _quoControlsInitialized = false;

  _initQuoControls() {
    if (this._quoControlsInitialized) return;
    this._quoControlsInitialized = true;

    const searchInput = document.getElementById("quo-search-input");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        this._quoFilterText = (e.target.value || "").toLowerCase().trim();
        this._quoCurrentPage = 1;
        this.renderQuoReminders();
      });
    }

    const prevBtn = document.getElementById("quo-prev-btn");
    if (prevBtn) {
      prevBtn.addEventListener("click", () => {
        if (this._quoCurrentPage > 1) {
          this._quoCurrentPage--;
          this.renderQuoReminders();
        }
      });
    }

    const nextBtn = document.getElementById("quo-next-btn");
    if (nextBtn) {
      nextBtn.addEventListener("click", () => {
        const filtered = this._getFilteredReminders();
        const totalPages = Math.max(1, Math.ceil(filtered.length / this._quoPageSize));
        if (this._quoCurrentPage < totalPages) {
          this._quoCurrentPage++;
          this.renderQuoReminders();
        }
      });
    }

    const sendAllBtn = document.getElementById("btn-quo-send-all");
    if (sendAllBtn) {
      sendAllBtn.addEventListener("click", () => {
        this.sendAllHITLReminders();
      });
    }
  }

  _getFilteredReminders() {
    if (!this._quoFilterText) return this._quoReminders;
    const q = this._quoFilterText;
    return this._quoReminders.filter(r => {
      return (
        (r.tenant_name && r.tenant_name.toLowerCase().includes(q)) ||
        (r.tenant_id && r.tenant_id.toLowerCase().includes(q)) ||
        (r.property_unit && r.property_unit.toLowerCase().includes(q)) ||
        (r.phone && r.phone.toLowerCase().includes(q)) ||
        String(r.balance).includes(q)
      );
    });
  }

  renderQuoReminders() {
    const tbodyReminders = document.getElementById("quo-reminders-tbody");
    if (!tbodyReminders) return;

    const filtered = this._getFilteredReminders();
    const totalItems = filtered.length;
    const totalPages = Math.max(1, Math.ceil(totalItems / this._quoPageSize));
    if (this._quoCurrentPage > totalPages) this._quoCurrentPage = totalPages;

    const startIdx = (this._quoCurrentPage - 1) * this._quoPageSize;
    const endIdx = Math.min(startIdx + this._quoPageSize, totalItems);
    const pageItems = filtered.slice(startIdx, endIdx);

    // Update pagination controls
    const info = document.getElementById("quo-pagination-info");
    const pageNum = document.getElementById("quo-page-num");
    const prevBtn = document.getElementById("quo-prev-btn");
    const nextBtn = document.getElementById("quo-next-btn");

    if (info) {
      info.textContent = totalItems === 0 
        ? "No matching tenants" 
        : `Showing ${startIdx + 1}–${endIdx} of ${totalItems} tenants`;
    }
    if (pageNum) pageNum.textContent = `Page ${this._quoCurrentPage} of ${totalPages}`;
    if (prevBtn) {
      prevBtn.disabled = this._quoCurrentPage <= 1;
      prevBtn.style.opacity = this._quoCurrentPage <= 1 ? "0.4" : "1";
      prevBtn.style.cursor = this._quoCurrentPage <= 1 ? "not-allowed" : "pointer";
    }
    if (nextBtn) {
      nextBtn.disabled = this._quoCurrentPage >= totalPages;
      nextBtn.style.opacity = this._quoCurrentPage >= totalPages ? "0.4" : "1";
      nextBtn.style.cursor = this._quoCurrentPage >= totalPages ? "not-allowed" : "pointer";
    }

    if (totalItems === 0) {
      tbodyReminders.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:32px;">
        <div style="color:#10b981;font-size:16px;margin-bottom:8px;">✅ No tenants currently require a reminder</div>
        <div style="color:#64748b;font-size:12px;">All tenants are either up to date, promised, or don't match your search filter.</div>
      </td></tr>`;
      return;
    }

    tbodyReminders.innerHTML = pageItems.map((r) => {
      const alreadySent = this._quoSentSet.has(r.tenant_id);
      const safeId = r.tenant_id.replace(/[^a-zA-Z0-9_-]/g, "");
      const btnId = `hitl-btn-${safeId}`;
      const statusId = `hitl-status-${safeId}`;

      const btnHtml = alreadySent
        ? `<button id="${btnId}" disabled style="background:#065f46;color:#6ee7b7;border:none;border-radius:8px;padding:8px 16px;font-size:12px;font-weight:700;cursor:not-allowed;display:flex;align-items:center;gap:6px;white-space:nowrap;">✅ Sent</button>`
        : `<button id="${btnId}" onclick="window.appCtrl.sendHITLReminder('${r.tenant_id}')"
            style="background:linear-gradient(135deg,#f59e0b,#d97706);color:#0f172a;border:none;border-radius:8px;padding:8px 16px;font-size:12px;font-weight:800;cursor:pointer;display:flex;align-items:center;gap:6px;white-space:nowrap;transition:all 0.2s;box-shadow:0 2px 8px rgba(245,158,11,0.3);"
            onmouseenter="this.style.transform='scale(1.04)'" onmouseleave="this.style.transform='scale(1)'">
            ✉ Send Reminder
          </button>`;

      return `
        <tr id="hitl-row-${safeId}">
          <td><strong>${r.tenant_name}</strong><br><small style="color:#64748b;">${r.tenant_id}</small></td>
          <td>${r.property_unit}</td>
          <td><code>${r.phone}</code></td>
          <td><strong style="color:#f87171;">$${Number(r.balance).toLocaleString('en-US', {minimumFractionDigits: 2})}</strong></td>
          <td>
            <span id="${statusId}" class="badge" style="background:${alreadySent ? 'rgba(16,185,129,0.15)' : '#f59e0b22'};color:${alreadySent ? '#10b981' : '#f59e0b'};border:1px solid ${alreadySent ? 'rgba(16,185,129,0.4)' : 'rgba(245,158,11,0.4)'};padding:4px 8px;border-radius:12px;font-size:11px;white-space:nowrap;">
              ${alreadySent ? '✅ Sent via Quo' : '⏳ Awaiting HITL Approval'}
            </span>
          </td>
          <td>
            <div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:8px 12px;font-size:12px;color:#cbd5e1;line-height:1.4;max-width:440px;">
              💬 <em>${r.sms_preview}</em>
            </div>
          </td>
          <td style="text-align:center;">
            ${btnHtml}
          </td>
        </tr>
      `;
    }).join("");
  }

  async loadQuoData() {
    const btn = document.getElementById("btn-refresh-quo");
    if (btn) btn.innerHTML = `<span>⏳ Loading...</span>`;

    try {
      this._initQuoControls();

      const res = await fetch("/api/quo-live");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to load Quo data");

      this._quoReminders = data.reminders_needed || [];

      // Update KPI counters
      const countNeeded = document.getElementById("quo-count-needed");
      const countInbound = document.getElementById("quo-count-inbound");
      const sentCounter = document.getElementById("quo-sent-counter");
      if (countNeeded && data.counts) countNeeded.textContent = `${data.counts.reminders_needed} units`;
      if (countInbound && data.counts) countInbound.textContent = `${data.counts.inbound_replies} replies`;
      if (sentCounter && data.counts && data.counts.hitl_approved_sent > 0) {
        sentCounter.textContent = data.counts.hitl_approved_sent + this._quoSentCount;
      }

      // Update phone display with live OpenPhone data
      const phoneDisplay = document.getElementById("quo-phone-display");
      if (phoneDisplay && data.channel) {
        const convCount = data.channel.openphone_conversations || 0;
        phoneDisplay.innerHTML = `${data.channel.phone_number || '+1 (667) 281-3879'}
          ${convCount > 0 ? `<span style="font-size:12px;color:#10b981;display:block;margin-top:4px;">📡 ${convCount} live conversations fetched</span>` : ''}`;
      }

      // Render HITL reminders table
      this.renderQuoReminders();

      // ─── Quo SMS Message History ───
      const tbodyMsgs = document.getElementById("quo-messages-tbody");
      if (tbodyMsgs) {
        const msgs = data.sms_messages || data.messages || [];
        if (msgs.length === 0) {
          tbodyMsgs.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:24px;color:#94a3b8;">No SMS messages yet — use the HITL Send buttons above to dispatch approved reminders.</td></tr>`;
        } else {
          tbodyMsgs.innerHTML = msgs.map(m => {
            const isInbound = m.direction === "INBOUND";
            const isSent = m.status && m.status.includes("SENT");
            const dirBadge = isInbound
              ? `<span class="badge" style="background:#065f46;color:#6ee7b7;padding:3px 8px;border-radius:12px;font-size:11px;">📥 INBOUND (Tenant)</span>`
              : isSent
                ? `<span class="badge" style="background:#1e3a5f;color:#38bdf8;padding:3px 8px;border-radius:12px;font-size:11px;">📤 SENT (HITL Approved)</span>`
                : `<span class="badge" style="background:#1e293b;color:#94a3b8;padding:3px 8px;border-radius:12px;font-size:11px;">📋 LOGGED (Suppressed)</span>`;
            const dateStr = m.timestamp ? new Date(m.timestamp).toLocaleString() : '-';
            return `
              <tr>
                <td style="white-space:nowrap;font-size:12px;color:#94a3b8;">${dateStr}</td>
                <td>${dirBadge}</td>
                <td>
                  <strong>${m.tenant_name || m.sender}</strong><br>
                  <small style="color:#64748b;">${m.property_unit || m.tenant_id || ''}</small>
                </td>
                <td>
                  <div style="font-size:13px;color:#f1f5f9;line-height:1.4;background:${isInbound ? 'rgba(56,189,248,0.06)' : (isSent ? 'rgba(16,185,129,0.05)' : 'transparent')};padding:${(isInbound || isSent) ? '8px 12px' : '0'};border-radius:6px;border-left:${isInbound ? '3px solid #38bdf8' : (isSent ? '3px solid #10b981' : 'none')};">
                    ${m.content}
                  </div>
                </td>
                <td>
                  <span class="badge" style="background:#0f172a;color:${isSent ? '#10b981' : '#94a3b8'};font-size:11px;">${m.status || 'LOGGED'}</span>
                </td>
              </tr>
            `;
          }).join("");
        }
      }
    } catch (err) {
      console.error("Quo load error:", err);
      const tbody = document.getElementById("quo-reminders-tbody");
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#ef4444;padding:20px;">Failed to load Quo feed: ${err.message}</td></tr>`;
      }
    } finally {
      if (btn) btn.innerHTML = `<span>🔄 Refresh Quo Feeds</span>`;
    }
  }

  async sendHITLReminder(tenantId) {
    const item = this._quoReminders.find(x => x.tenant_id === tenantId);
    if (!item || this._quoSentSet.has(tenantId)) return;

    const safeId = tenantId.replace(/[^a-zA-Z0-9_-]/g, "");
    const sendBtn = document.getElementById(`hitl-btn-${safeId}`);
    const statusSpan = document.getElementById(`hitl-status-${safeId}`);

    if (sendBtn) {
      sendBtn.disabled = true;
      sendBtn.innerHTML = `⏳ Sending...`;
      sendBtn.style.background = "#334155";
      sendBtn.style.color = "#94a3b8";
    }
    if (statusSpan) {
      statusSpan.textContent = "📡 Dispatching via Quo...";
      statusSpan.style.background = "rgba(56,189,248,0.1)";
      statusSpan.style.color = "#38bdf8";
    }

    try {
      const res = await fetch("/api/send-reminder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          tenant_id: tenantId, 
          phone: item.phone, 
          text: item.sms_preview, 
          hitl_approved: true 
        })
      });
      const data = await res.json();
      if (!res.ok || !data.success) throw new Error(data.error || "Send failed");

      // Mark as sent
      this._quoSentSet.add(tenantId);
      this._quoSentCount++;

      // Update counter
      const counter = document.getElementById("quo-sent-counter");
      if (counter) counter.textContent = this._quoSentCount;

      if (sendBtn) {
        sendBtn.innerHTML = `✅ Sent`;
        sendBtn.style.background = "#065f46";
        sendBtn.style.color = "#6ee7b7";
        sendBtn.style.boxShadow = "none";
      }

      if (statusSpan) {
        statusSpan.textContent = data.mock_mode ? "✅ Sent (Logged — Mock)" : "✅ Sent via Quo API";
        statusSpan.style.background = "rgba(16,185,129,0.1)";
        statusSpan.style.color = "#10b981";
        statusSpan.style.border = "1px solid rgba(16,185,129,0.4)";
      }

      this._showToast(`✅ HITL Approved — Reminder ${data.mock_mode ? 'logged' : 'sent'} to ${item.phone}`, "success");
      setTimeout(() => this.loadQuoData(), 1200);
      return true;
    } catch (err) {
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.innerHTML = `✉ Send Reminder`;
        sendBtn.style.background = "linear-gradient(135deg,#f59e0b,#d97706)";
        sendBtn.style.color = "#0f172a";
      }
      if (statusSpan) {
        statusSpan.textContent = "❌ Send Failed — Retry";
        statusSpan.style.color = "#ef4444";
      }
      this._showToast(`❌ Failed to send reminder to ${item.phone}: ${err.message}`, "error");
      return false;
    }
  }

  async sendAllHITLReminders() {
    const pending = this._quoReminders.filter(r => !this._quoSentSet.has(r.tenant_id));
    if (pending.length === 0) {
      this._showToast("All eligible reminders have already been approved and sent!", "info");
      return;
    }

    const ok = confirm(`Are you sure you want to approve and send reminders to all ${pending.length} tenants?`);
    if (!ok) return;

    const btn = document.getElementById("btn-quo-send-all");
    if (btn) {
      btn.disabled = true;
      btn.style.opacity = "0.6";
      btn.innerHTML = `<span>⏳ Sending 0/${pending.length}...</span>`;
    }

    let successCount = 0;
    for (let i = 0; i < pending.length; i++) {
      const item = pending[i];
      if (btn) btn.innerHTML = `<span>⏳ Sending ${i + 1}/${pending.length}...</span>`;
      const res = await this.sendHITLReminder(item.tenant_id);
      if (res) successCount++;
      // Small pause between dispatches
      await new Promise(r => setTimeout(r, 300));
    }

    if (btn) {
      btn.disabled = false;
      btn.style.opacity = "1";
      btn.innerHTML = `<span>⚡ Send All Reminders (HITL)</span>`;
    }

    this._showToast(`✅ Completed: ${successCount} of ${pending.length} reminders successfully dispatched.`, "success");
    this.renderQuoReminders();
  }

  _showToast(message, type = "info") {
    const existing = document.getElementById("quo-toast");
    if (existing) existing.remove();

    const colors = {
      success: { bg: "rgba(16,185,129,0.15)", border: "rgba(16,185,129,0.5)", color: "#10b981" },
      error:   { bg: "rgba(239,68,68,0.15)",  border: "rgba(239,68,68,0.5)",  color: "#ef4444" },
      info:    { bg: "rgba(56,189,248,0.15)",  border: "rgba(56,189,248,0.5)", color: "#38bdf8" }
    };
    const c = colors[type] || colors.info;

    const toast = document.createElement("div");
    toast.id = "quo-toast";
    toast.style.cssText = `position:fixed;bottom:28px;right:28px;z-index:9999;background:${c.bg};border:1px solid ${c.border};color:${c.color};padding:14px 22px;border-radius:12px;font-size:14px;font-weight:600;backdrop-filter:blur(10px);box-shadow:0 8px 32px rgba(0,0,0,0.4);max-width:420px;line-height:1.4;animation:slideInUp 0.3s ease;`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 4500);
  }

  renderRuns(runs) {
    const container = document.getElementById("runs-history-container");
    if (!container) return;
    container.innerHTML = "";

    if (runs.length === 0) {
      container.innerHTML = `<div class="text-muted" style="padding:20px 0;">No automated workflow runs recorded yet.</div>`;
      return;
    }

    runs.forEach(r => {
      const card = document.createElement("div");
      card.className = "sim-card";
      card.style.marginBottom = "14px";
      const modeStr = r.is_dry_run ? '<span style="color:#fbbf24;">DRY-RUN SIMULATION</span>' : '<span style="color:#34d399;">LIVE DISPATCH</span>';

      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border-subtle);padding-bottom:10px;margin-bottom:12px;">
          <div>
            <span style="font-family:var(--font-mono);font-weight:700;color:#38bdf8;">${r.run_id}</span>
            <span style="font-size:12px;color:var(--text-muted);margin-left:12px;">${r.completed_at ? r.completed_at.substring(0, 19).replace('T', ' ') : ''} UTC</span>
          </div>
          <div style="font-size:12px;font-weight:700;">${modeStr}</div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(6, 1fr);gap:10px;font-size:12px;">
          <div><span style="color:var(--text-muted);display:block;">Evaluated:</span> <strong>${r.total_tenants} units</strong></div>
          <div><span style="color:var(--text-muted);display:block;">Paid:</span> <strong style="color:#34d399;">${r.count_paid}</strong></div>
          <div><span style="color:var(--text-muted);display:block;">Promises:</span> <strong style="color:#fbbf24;">${r.count_promised}</strong></div>
          <div><span style="color:var(--text-muted);display:block;">Reminded:</span> <strong style="color:#f87171;">${r.count_reminded}</strong></div>
          <div><span style="color:var(--text-muted);display:block;">Cooldown/Grace:</span> <strong>${r.count_skipped}</strong></div>
          <div><span style="color:var(--text-muted);display:block;">Flagged:</span> <strong style="color:#fb7185;">${r.count_flagged}</strong></div>
        </div>
      `;
      container.appendChild(card);
    });
  }

  async runWorkflow(isDryRun = false) {
    const btn = isDryRun ? this.btnDryRun : this.btnRun;
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span>⏳ Processing Workflow & Syncing...</span>`;

    try {
      const res = await fetch("/api/run-workflow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_dry_run: isDryRun })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      // 1. Immediately apply the final authoritative results to all KPI cards and flowchart
      if (data.summary) {
        this.updateKPIs(data.summary);
      }

      // 2. Animate execution beam through flowchart with final data
      if (window.flowchartCtrl) {
        await window.flowchartCtrl.animateExecution(isDryRun, data.summary);
      }

      // 3. Refresh tenant table, runs log, and external overviews
      await this.refreshData(false);
      this.loadRentVineOverview();
      this.loadQuoData();

      const syncElem = document.getElementById("last-sync-time");
      if (syncElem) {
        syncElem.textContent = `Last execution: Just now (${isDryRun ? 'Dry-Run' : 'Live'})`;
      }
    } catch (err) {
      alert("Workflow execution error: " + err.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
  }

  async resetSeedData() {
    if (!confirm("Re-sync database with live RentVine portfolio (zero dummy data)?")) return;
    try {
      const res = await fetch("/api/reset-seed", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      await this.refreshData(false);
      this.loadRentVineOverview();
      this.loadQuoData();
      if (window.flowchartCtrl) {
        window.flowchartCtrl.logTerminal("[SYNC] Database synchronized with 100% live RentVine leases. Zero dummy data.", "success");
      }
    } catch (err) {
      alert("Reset failed: " + err.message);
    }
  }

  async openReportModal() {
    this.reportModal.classList.add("open");
    const body = document.getElementById("report-modal-body");
    body.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-muted);">Generating executive report...</div>`;

    try {
      const res = await fetch("/api/latest-report?format=html");
      const html = await res.text();
      body.innerHTML = html;
    } catch (err) {
      body.innerHTML = `<div style="color:var(--rose-primary);padding:20px;">Failed to load report: ${err.message}</div>`;
    }
  }

  async saveSettings() {
    const grace = document.getElementById("set-grace-period")?.value;
    const dedup = document.getElementById("set-dedup-window")?.value;
    const promise = document.getElementById("set-promise-expiry")?.value;
    const model = document.getElementById("set-ai-model")?.value;

    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          grace_period_days: grace,
          deduplication_window_hours: dedup,
          promise_expiry_days: promise,
          ai_model: model
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      alert("Settings saved successfully!");
    } catch (err) {
      alert("Failed to save settings: " + err.message);
    }
  }
}

window.addEventListener("DOMContentLoaded", () => {
  window.appCtrl = new AppController();
});
