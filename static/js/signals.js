// static/js/signals.js
// Signals page logic

(function () {
  "use strict";

  const CFG = (() => {
    const raw = window.SIGNALS_CONFIG || {};
    return {
      dataUrl: raw.dataUrl || "/dashboard/signals/data",
      refreshMs: Number(raw.refreshMs || 30000),
      defaultStatus: String(raw.defaultStatus || raw.default_status || "ALL").toUpperCase(),
      defaultLimit: Number(raw.defaultLimit || raw.default_limit || 500)
    };
  })();

  let signalRawData = [];
  let signalFilteredData = [];
  let signalLastTrigger = null;

  function initTooltips(scope) {
    if (window.UI && typeof UI.initTooltips === "function") {
      UI.initTooltips(scope);
    }
  }

  function num(v, d = 2) {
    const n = Number(v);
    return (v == null || Number.isNaN(n)) ? "-" : n.toFixed(d);
  }

  function safeStr(v, defVal = "—") {
    if (v === null || v === undefined) return defVal;
    const s = String(v);
    return s.length ? s : defVal;
  }

  function jsonPretty(obj) {
    try {
      return JSON.stringify(obj ?? {}, null, 2);
    } catch {
      return "{}";
    }
  }

  function escHtml(v) {
    return String(v ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function getPath(obj, path, defVal = null) {
    try {
      const out = String(path || "")
        .split(".")
        .reduce((acc, key) => acc?.[key], obj);
      return out == null ? defVal : out;
    } catch {
      return defVal;
    }
  }

  function upper(v) {
    return String(v || "").toUpperCase();
  }

  function displaySetup(v) {
    return String(v || "");
  }

  function signalSetupLabel(row) {
    return String(row?.setup || "").trim().toUpperCase();
  }

  function initiatedSetupLabel(row) {
    return signalSetupLabel(row);
  }

  function currentSetupLabel(row) {
    const direct = String(row?.current_setup || "").trim().toUpperCase();
    if (!direct) throw new Error("Signal row is missing current_setup");
    return direct;
  }

  function buildTradePayload(signalRow) {
    return {
      signal_id: signalRow.signal_id || "",
      symbol: signalRow.symbol || "",
      side: signalRow.side || "BUY",
      ltp: signalRow.last_price ?? null,
      ltp_time: signalRow.last_eval_time || "",
      details: {
        signal: signalRow
      }
    };
  }

  function stageBadge(stage) {
    const s = upper(stage);
    let cls = "bg-secondary";
    if (["ACTIVE", "EXPAND"].includes(s)) cls = "bg-success";
    else if (["BUILDING", "TESTING"].includes(s)) cls = "bg-info text-dark";
    else if (["PROTECT", "WEAKENING", "EXIT_BIAS"].includes(s)) cls = "bg-warning text-dark";
    else if (s === "FORCE_EXIT") cls = "bg-danger";
    return `<span class="badge sig-badge ${cls}">${escHtml(s || "—")}</span>`;
  }

  function signalTradeDisabledReason(row) {
    const status = upper(row?.status);
    const stage = upper(row?.stage);
    const terminalStatuses = new Set([
      "INVALIDATED", "EXPIRED", "REPLACED", "CLOSED", "CANCELLED", "BLOCKED"
    ]);
    const posture = upper(row?.management_posture);
    const tradeAction = upper(row?.lifecycle_trade_action);
    if (terminalStatuses.has(status)) return `Signal is ${status.toLowerCase()}.`;
    if (["EXIT_BIAS", "FORCE_EXIT"].includes(stage)) return "Signal is in an exit posture and cannot create a new trade.";
    if (posture === "EXIT" || row?.should_exit_signal === true) return "Signal management posture requires exit; new entry is blocked.";
    if (["EXIT_POSITION", "FORCE_EXIT"].includes(tradeAction)) return "Current trade instruction blocks new entry.";
    return "";
  }

  function signalTradeWarning(row) {
    const stage = upper(row?.stage);
    const posture = upper(row?.management_posture);
    if (["PROTECT", "TRANSITION", "WEAKENING"].includes(stage) || posture === "CAUTION") {
      return "Manual confirmation required for the current defensive signal posture.";
    }
    return "";
  }

  function statusBadge(status) {
    const s = upper(status);
    let cls = "bg-secondary";
    if (s === "OPEN") cls = "bg-success";
    else if (s === "REPLACED") cls = "bg-primary";
    else if (s === "INVALIDATED") cls = "bg-danger";
    else if (s === "EXPIRED") cls = "bg-dark";
    else if (s === "CANCELLED") cls = "bg-secondary";
    else if (s === "CLOSED") cls = "bg-dark";
    else if (s === "BLOCKED") cls = "bg-warning text-dark";
    return `<span class="badge sig-badge ${cls}">${escHtml(s || "—")}</span>`;
  }

  function sideBadge(side) {
    const s = upper(side);
    const cls = s === "BUY" ? "bg-success" : (s === "SELL" ? "bg-danger" : "bg-secondary");
    return `<span class="badge sig-badge ${cls}">${escHtml(s || "—")}</span>`;
  }

  function bbBadge(zone) {
    const z = upper(zone);
    let cls = "bg-secondary";
    if (z.includes("ABOVE")) cls = "bg-danger";
    else if (z.includes("UPPER")) cls = "bg-warning text-dark";
    else if (z.includes("MID")) cls = "bg-info text-dark";
    else if (z.includes("LOWER")) cls = "bg-warning text-dark";
    else if (z.includes("BELOW")) cls = "bg-success";
    return `<span class="badge sig-badge ${cls}">${escHtml(z || "—")}</span>`;
  }

  function setupSignalsModalBehavior() {
    const detailsModalEl = document.getElementById("signalDetailsModal");
    if (!detailsModalEl) return;

    detailsModalEl.addEventListener("hide.bs.modal", () => {
      try {
        if (document.activeElement && detailsModalEl.contains(document.activeElement)) {
          document.activeElement.blur();
        }
      } catch (_) { }
      detailsModalEl.setAttribute("inert", "");
    });

    detailsModalEl.addEventListener("shown.bs.modal", () => {
      detailsModalEl.removeAttribute("inert");
    });

    detailsModalEl.addEventListener("hidden.bs.modal", () => {
      detailsModalEl.removeAttribute("inert");
      if (signalLastTrigger && typeof signalLastTrigger.focus === "function") {
        signalLastTrigger.focus();
      }
      signalLastTrigger = null;
    });
  }

  function buildUrl() {
    const url = new URL(CFG.dataUrl, window.location.origin);
    const length = parseInt($("#sig-length").val(), 10) || 20;
    url.searchParams.set("limit", String(Math.max(length, CFG.defaultLimit || 100)));
    // Always fetch all statuses; filtering is done client-side so signals do not
    // disappear from the page after they are INVALIDATED/CLOSED.
    url.searchParams.set("status", "ALL");
    return url.toString();
  }

  function refreshSetupOptions(rows) {
    const sel = document.getElementById("sig-setup");
    if (!sel) return;

    const current = String(sel.value || "").toUpperCase();

    const setupLabels = Array.from(
      new Set(
        (rows || [])
          .map(r => signalSetupLabel(r))
          .filter(Boolean)
      )
    ).sort((a, b) => a.localeCompare(b));

    sel.innerHTML = "";

    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.textContent = "All Setups";
    sel.appendChild(allOpt);

    setupLabels.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = displaySetup(s);
      sel.appendChild(opt);
    });

    const stillExists = setupLabels.some(s => s.toUpperCase() === current);
    sel.value = stillExists ? current : "";
  }

  async function loadSignals() {
    try {
      const resp = await fetch(buildUrl(), { credentials: "same-origin" });
      const payload = await resp.json().catch(() => ({}));

      if (!resp.ok) {
        console.error("signals fetch failed:", resp.status, payload);
        return;
      }

      ingestSignals(payload);
    } catch (err) {
      console.error("signals fetch failed:", err);
    }
  }


  function buildAuditPayload(row) {
    const id = safeStr(row.signal_id, "");
    const symbol = safeStr(row.symbol, "");
    const userid = safeStr(row.userid || CFG.userid || CFG.currentUserId || "", "");
    const startTime = safeStr(row.first_seen_time || row.created_time || row.last_eval_time, "");
    const endTime = safeStr(row.closed_time || "", "");

    return {
      title: `Signal ${symbol}`,
      subtitle: `${safeStr(row.side, "")} • ${safeStr(row.stage, "")} • ${safeStr(row.status, "")}${userid ? " • " + userid : ""}`,
      entityId: id,
      relatedId: id,
      symbol,
      userid,
      startTime,
      endTime,
      limit: 300
    };
  }

  function actionsCell(row) {
    const id = safeStr(row.signal_id, "");
    const payloadEnc = encodeURIComponent(JSON.stringify(buildTradePayload(row)));
    const auditEnc = encodeURIComponent(JSON.stringify(buildAuditPayload(row)));
    const tradeDisabledReason = signalTradeDisabledReason(row);
    const tradeDisabled = Boolean(tradeDisabledReason);
    const tradeWarning = signalTradeWarning(row);
    const tradeClass = tradeDisabled
      ? "text-muted"
      : (tradeWarning ? "text-warning js-trade-create" : "text-success js-trade-create");
    const tradeIcon = tradeDisabled ? "bi-slash-circle" : "bi-plus-circle";
    const tradeDisabledAttrs = tradeDisabled ? 'disabled aria-disabled="true"' : "";

    return `
      <div class="d-flex align-items-center justify-content-center gap-1 flex-nowrap">
        <button type="button"
          class="btn btn-link btn-sm p-0 px-1 text-primary sig-view"
          data-id="${id}"
          data-bs-toggle="tooltip"
          title="Signal Details">
          <i class="bi bi-info-circle"></i>
        </button>

        <button type="button"
          class="btn btn-link btn-sm p-0 px-1 text-secondary js-audit-timeline"
          data-record="${auditEnc}"
          data-bs-toggle="tooltip"
          title="Audit Timeline">
          <i class="bi bi-clock-history"></i>
        </button>

        <button type="button"
          class="btn btn-link btn-sm p-0 px-1 ${tradeClass}"
          data-source="signals"
          data-record="${payloadEnc}"
          data-bs-toggle="tooltip"
          title="${escHtml(tradeDisabledReason || tradeWarning || "Create Trade")}"
          ${tradeDisabledAttrs}>
          <i class="bi ${tradeIcon}"></i>
        </button>
      </div>
    `;
  }

  function signalPnlCell(row) {
    const pct = Number(row?.last_pnl);
    const perShare = Number(row?.last_pnl_value);

    if (!Number.isFinite(pct) || !Number.isFinite(perShare)) {
      return `<div class="sig-cell-tight text-muted">—</div>`;
    }

    const cls = pct > 0 ? "dash-text-success" : (pct < 0 ? "dash-text-danger" : "text-muted");
    const pctText = `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`;
    const valueText = `${perShare > 0 ? "+" : (perShare < 0 ? "-" : "")}₹${Math.abs(perShare).toFixed(2)}`;

    return `<div class="sig-cell-tight ${cls}">
      ${escHtml(pctText)}
      <div class="sig-sub">${escHtml(valueText)}</div>
    </div>`;
  }

  function addRow(table, row) {
    const closedTime = (row.closed_time && row.closed_time !== "N/A") ? row.closed_time : "Active";
    const closedPrice = (row.closed_price != null && row.closed_price !== "N/A") ? num(row.closed_price, 2) : "-";

    table.row.add([
      `<div class="sig-cell-tight fw-semibold">${escHtml(safeStr(row.symbol, ""))}</div>`,
      `<div class="sig-cell-tight">${escHtml(displaySetup(signalSetupLabel(row)))}</div>`,
      stageBadge(row.stage),
      statusBadge(row.status),

      `<div class="sig-cell-tight">
        ${num(row.created_price, 2)}
        <div class="sig-sub">${escHtml(safeStr(row.first_seen_time, ""))}</div>
      </div>`,

      `<div class="sig-cell-tight">
        ${num(row.last_price, 2)}
        <div class="sig-sub">${escHtml(safeStr(row.last_eval_time, ""))}</div>
      </div>`,

      `<div class="sig-cell-tight">
        ${closedPrice}
        <div class="sig-sub">${escHtml(closedTime)}</div>
      </div>`,

      signalPnlCell(row),
      `<div class="sig-cell-tight">${num(row.vwap, 2)}</div>`,
      `<div class="sig-cell-tight">${num(row.rsi, 2)}</div>`,
      bbBadge(row.bb_zone),

      actionsCell(row)
    ]);
  }

  function getFilteredRows(rows) {
    const selectedSetup = ($("#sig-setup").val() || "").toUpperCase();
    const selectedStatus = ($("#sig-status").val() || "").toUpperCase();

    return (rows || []).filter(row => {
      const rowSetup = signalSetupLabel(row);
      const rowStatus = String(row?.status || "").toUpperCase();

      if (selectedSetup && rowSetup !== selectedSetup) return false;
      if (selectedStatus && rowStatus !== selectedStatus) return false;

      return true;
    });
  }

  function ingestSignals(payload) {
    let rows = [];

    if (payload && payload.status === "success" && Array.isArray(payload.data)) {
      rows = payload.data;
    } else if (payload && Array.isArray(payload.data)) {
      rows = payload.data;
    } else if (Array.isArray(payload)) {
      rows = payload;
    } else {
      rows = [];
    }

    signalRawData = rows;
    refreshSetupOptions(signalRawData);
    renderSignals();
  }

  function renderSignals() {
    const buyTable = $("#signals-buy-table").DataTable();
    const sellTable = $("#signals-sell-table").DataTable();

    const rows = getFilteredRows(signalRawData);
    signalFilteredData = rows;

    const buy = rows.filter(row => upper(row?.side) === "BUY");
    const sell = rows.filter(row => upper(row?.side) === "SELL");

    buyTable.clear();
    sellTable.clear();

    buy.forEach(row => addRow(buyTable, row));
    sell.forEach(row => addRow(sellTable, row));

    buyTable.draw();
    sellTable.draw();

    buyTable.columns.adjust().responsive.recalc();
    sellTable.columns.adjust().responsive.recalc();

    $("#sig-buy-count").text(String(buy.length));
    $("#sig-sell-count").text(String(sell.length));

    const q = $("#sig-search").val() || "";
    buyTable.search(q).draw(false);
    sellTable.search(q).draw(false);

    initTooltips("#signals-controls");
    initTooltips("#signals-buy-table");
    initTooltips("#signals-sell-table");
  }

  function formatPriceMove(row) {
    const created = Number(row?.created_price);
    const last = Number(row?.last_price);
    if (!Number.isFinite(created) || !Number.isFinite(last) || created === 0) return "—";

    const delta = last - created;
    const pctMove = (delta / created) * 100;
    const cls = delta > 0 ? "text-success" : (delta < 0 ? "text-danger" : "text-muted");

    return `<span class="${cls}">${escHtml(delta.toFixed(2))} (${escHtml(pctMove.toFixed(2))}%)</span>`;
  }

  function buildSetupStory(row) {
    const setup = displaySetup(signalSetupLabel(row));
    const side = upper(row?.side);
    const lines = [
      `${setup} ${side} signal.`,
      `Signal lifecycle: ${safeStr(row?.stage)} / ${safeStr(row?.status)}.`,
      `Auction: ${safeStr(row?.auction_action)} / ${safeStr(row?.auction_state)}.`,
      `Management posture: ${safeStr(row?.management_posture)}.`,
      `Directional alignment: ${safeStr(row?.directional_alignment)}.`,
      `Lifecycle reason: ${safeStr(row?.lifecycle_reason || row?.reason)}.`
    ];
    return lines.join("\n");
  }

  function buildStatusDescription(row) {
    const closedTime = (row.closed_time && row.closed_time !== "N/A") ? row.closed_time : "still active";
    return `Originating setup: ${safeStr(signalSetupLabel(row))}. Current setup: ${safeStr(currentSetupLabel(row))}. Current state: ${safeStr(row.stage)} / ${safeStr(row.status)}. Created at ${safeStr(row.first_seen_time)} and last evaluated at ${safeStr(row.last_eval_time)}. Closed state: ${closedTime}.`;
  }

  function renderSignalRows(row) {
    const confidence = row?.confidence == null ? "Not emitted" : num(row.confidence, 2);
    const quality = row?.quality == null || row?.quality === "" ? "Not emitted" : safeStr(row.quality);
    const rows = [
      ["Setup", safeStr(displaySetup(signalSetupLabel(row)))],
      ["Signal Stage", stageBadge(row.stage)],
      ["Signal Status", statusBadge(row.status)],
      ["Management Posture", escHtml(safeStr(row?.management_posture))],
      ["Current Trade Instruction", escHtml(safeStr(row?.lifecycle_trade_action))],
      ["Directional Alignment", escHtml(safeStr(row?.directional_alignment))],
      ["Lifecycle Reason", escHtml(safeStr(row?.lifecycle_reason || row?.reason))],
      ["Confidence", escHtml(confidence)],
      ["Quality", escHtml(quality)]
    ];

    return rows.map(([k, v]) => `
      <tr><th>${escHtml(k)}</th><td>${v}</td></tr>
    `).join("");
  }

  function renderContextRows(row) {
    const snap = row?.snapshot || {};
    const directional = getPath(snap, "auction.directional", {}) || {};
    const balance = getPath(snap, "auction.balance", {}) || {};
    const rows = [
      ["Auction Action", safeStr(row?.auction_action)],
      ["Directional Episode", `${safeStr(row?.auction_state || directional.direction || "—")} / ${safeStr(directional.transition || "NONE")}`],
      ["Balance Episode", `${safeStr(balance.current_state || "NONE")} / ${safeStr(balance.episode_id || "—")}`],
      ["Opportunity", safeStr(row?.opportunity_key)],
      ["Candidate", safeStr(row?.candidate_id)],
      ["Source Event", safeStr(row?.boundary_event_key)],
      ["Setup Reference", `${num(row?.setup_reference_price, 2)} / ${safeStr(row?.setup_reference_source)}`],
      ["Accepted Structure", `${safeStr(getPath(snap, "structure.accepted.state", "UNKNOWN"))} / ${safeStr(getPath(snap, "structure.accepted.side", "NEUTRAL"))}`],
      ["Raw Structure", `${safeStr(getPath(snap, "structure.raw.state", "UNKNOWN"))} / ${safeStr(getPath(snap, "structure.raw.side", "NEUTRAL"))}`],
      ["Candidate State", `${safeStr(getPath(snap, "structure.candidate.active", false))} / ${safeStr(getPath(snap, "structure.candidate.side", "NEUTRAL"))}`],
      ["VWAP", num(getPath(snap, "indicators.vwap.value", row?.vwap), 2)],
      ["RSI", `${num(getPath(snap, "indicators.rsi.value", row?.rsi), 2)} / ${safeStr(getPath(snap, "indicators.rsi.zone", "—"))}`],
      ["BB Zone", safeStr(getPath(snap, "indicators.bollinger.zone", row?.bb_zone || "—"))]
    ];
    return rows.map(([k, v]) => `<tr><th>${escHtml(k)}</th><td>${escHtml(v)}</td></tr>`).join("");
  }

  function renderMarketContextRows(row) {
    const snap = row?.snapshot || {};
    const windows = getPath(snap, "market_windows", {}) || {};
    const priceAction = getPath(snap, "price_action", {}) || {};
    const windowNames = ["15m", "30m", "60m", "sod"];
    const cells = [];
    windowNames.forEach(name => {
      const w = windows?.[name] || windows?.[name.toUpperCase()] || {};
      if (!w || Object.keys(w).length === 0) return;
      cells.push([`${name} Status`, w.status]);
      cells.push([`${name} Move %`, w.move_pct]);
    });
    cells.push(["Price Slope", priceAction.slope]);
    cells.push(["Price Momentum", priceAction.momentum]);
    const useful = cells.filter(([, value]) => value !== null && value !== undefined && value !== "");
    if (!useful.length) return `<tr><td colspan="4" class="text-center text-muted">—</td></tr>`;
    const rows = [];
    for (let i = 0; i < useful.length; i += 2) rows.push(useful.slice(i, i + 2));
    return rows.map(group => `<tr>${group.map(([k, v]) => `<th>${escHtml(k)}</th><td>${escHtml(safeStr(v))}</td>`).join("")}${group.length === 1 ? "<th></th><td></td>" : ""}</tr>`).join("");
  }

  function renderStatusRows(row) {
    const closedTime = (row.closed_time && row.closed_time !== "N/A") ? row.closed_time : "Active";
    const closedPrice = (row.closed_price != null && row.closed_price !== "N/A") ? num(row.closed_price, 2) : "-";

    const times = [
      `First ${safeStr(row.first_seen_time)}`,
      `Eval ${safeStr(row.last_eval_time)}`,
      `Snap ${safeStr(row.last_snapshot_time)}`
    ].join(" | ");

    const rows = [
      ["Created", `${safeStr(row.first_seen_time)} / ${num(row.created_price, 2)}`],
      ["Last", `${safeStr(row.last_eval_time)} / ${num(row.last_price, 2)}`],
      ["Closed", `${closedTime} / ${closedPrice}`],
      ["P&L", signalPnlCell(row)],
      ["Price Move", formatPriceMove(row)],
      ["Times", escHtml(times)]
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th>${escHtml(k)}</th>
        <td>${v}</td>
      </tr>
    `).join("");
  }

  function openSignalModal(signalId) {
    const row = (signalFilteredData || []).find(x => String(x.signal_id || "") === String(signalId || ""))
      || (signalRawData || []).find(x => String(x.signal_id || "") === String(signalId || ""));
    if (!row) return;

    $("#sigm-symbol").text(safeStr(row.symbol));
    $("#sigm-side").html(sideBadge(row.side));
    $("#sigm-setup-inline").text(safeStr(displaySetup(signalSetupLabel(row))));

    $("#sigm-signal-body").html(renderSignalRows(row));
    $("#sigm-setup-view").text(buildSetupStory(row));
    $("#sigm-context-body").html(renderContextRows(row));
    $("#sigm-momentum-context-body").html(renderMarketContextRows(row));
    $("#sigm-status-desc").text(buildStatusDescription(row));
    $("#sigm-status-body").html(renderStatusRows(row));

    $("#sigm-meta").text(jsonPretty(row.meta));
    $("#sigm-criteria").text(jsonPretty(row.criteria));
    $("#sigm-snapshot").text(jsonPretty(row.snapshot));

    $("#sigm-signal-id").val(String(signalId || ""));

    const modalEl = document.getElementById("signalDetailsModal");
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true });
    modal.show();

    initTooltips("#signalDetailsModal");
  }

  function bindDetailsClicks() {
    $(document).on("click", ".sig-view", function (e) {
      e.preventDefault();
      signalLastTrigger = this;
      const id = $(this).attr("data-id") || "";
      openSignalModal(id);
    });
  }

  $(document).ready(() => {
    const defaultStatus = CFG.defaultStatus === "ALL" ? "" : CFG.defaultStatus;
    $("#sig-status").val(defaultStatus);

    const buyTable = $("#signals-buy-table").DataTable({
      responsive: true,
      autoWidth: false,
      pageLength: 20,
      lengthMenu: [[20, 50, 100], [20, 50, 100]],
      dom: "rtip",
      columnDefs: [{ orderable: false, targets: -1 }],
      drawCallback() {
        initTooltips("#signals-buy-table");
      }
    });

    const sellTable = $("#signals-sell-table").DataTable({
      responsive: true,
      autoWidth: false,
      pageLength: 20,
      lengthMenu: [[20, 50, 100], [20, 50, 100]],
      dom: "rtip",
      columnDefs: [{ orderable: false, targets: -1 }],
      drawCallback() {
        initTooltips("#signals-sell-table");
      }
    });

    $("#sig-search").on("input", function () {
      const v = this.value || "";
      buyTable.search(v).draw();
      sellTable.search(v).draw();
    });

    $("#sig-length").on("change", function () {
      const n = parseInt(this.value, 10) || 20;
      buyTable.page.len(n).draw();
      sellTable.page.len(n).draw();
      loadSignals();
    });

    $("#sig-setup, #sig-status").on("change", function () {
      renderSignals();
    });

    setupSignalsModalBehavior();
    bindDetailsClicks();
    loadSignals();

    if (CFG.refreshMs > 0) {
      setInterval(() => {
        loadSignals();
      }, CFG.refreshMs);
    }
  });

  window.populateSignals = ingestSignals;
  window.loadSignals = loadSignals;
})();