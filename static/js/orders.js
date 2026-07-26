// static/js/orders.js
// Dashboard Orders
// - draft/executed buckets
// - multi-user flat table with collapsible user groups
// - shared edit flow:
//     draft rows    -> shared trade_edit.js in draft mode
//     executed rows -> shared trade_edit.js in live mode
// - quick submit    -> submit draft directly after current row values

(function () {
  "use strict";

  let RAW = [];
  let ALL_NORM = [];
  let NORM = [];
  let DT = null;
  let CURRENT = null;
  let CURRENT_BUCKET = "draft";
  let orderCollapsedGroups = { draft: Object.create(null), executed: Object.create(null) };
  let orderLoadSequence = 0;
  let orderLoadController = null;

  function cfg() {
    return window.ORDERS_CONFIG || {};
  }

  function showUsers() {
    return !!cfg().showUsers;
  }

  function selectedUserFilter() {
    const value = String($("#or-user-filter").val() || "ALL").trim().toUpperCase();
    return value || "ALL";
  }

  function selectedModeFilter() {
    const value = String($("#or-mode-filter").val() || "ALL").trim().toUpperCase();
    return value || "ALL";
  }

  function updateOrdersDownloadLink() {
    const link = document.getElementById("or-download");
    if (!link) return;

    const baseUrl = String(link.dataset.baseUrl || link.getAttribute("href") || "").trim();
    if (!baseUrl) return;

    const url = new URL(baseUrl, window.location.origin);
    const userid = selectedUserFilter();

    url.searchParams.delete("userid");
    if (showUsers() && userid !== "ALL") {
      url.searchParams.set("userid", userid);
    }

    link.href = `${url.pathname}${url.search}`;
  }

  function orderGroupState(bucket = CURRENT_BUCKET) {
    const key = bucket === "executed" ? "executed" : "draft";
    if (!orderCollapsedGroups[key]) {
      orderCollapsedGroups[key] = Object.create(null);
    }
    return orderCollapsedGroups[key];
  }

  function resetOrderGroupState(bucket) {
    const key = bucket === "executed" ? "executed" : "draft";
    orderCollapsedGroups[key] = Object.create(null);
  }

  function isOrderGroupCollapsed(group) {
    const key = String(group || "").trim();
    const state = orderGroupState();
    if (!Object.prototype.hasOwnProperty.call(state, key)) {
      state[key] = true;
    }
    return !!state[key];
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function intOr(v, d = 0) {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : d;
  }

  function fmtNum(v, d = 2) {
    const n = num(v);
    return n == null ? "–" : n.toFixed(d);
  }

  function fmtMoney(v) {
    const n = num(v);
    return "₹" + (n == null ? "0.00" : n.toFixed(2));
  }

  function upper(x, def = "") {
    const s = String(x == null ? "" : x).trim();
    return s ? s.toUpperCase() : def;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function pnlClass(v) {
    return Number(v || 0) >= 0 ? "dash-text-success" : "dash-text-danger";
  }

  function renderPnl(v, available = true) {
    if (!available) {
      return `<span class="text-muted">—</span>`;
    }
    const n = Number(v || 0);
    return `<span class="${pnlClass(n)}">${n.toFixed(2)}</span>`;
  }

  function renderHitBadge(v) {
    return v
      ? `<span class="badge bg-success">Yes</span>`
      : `<span class="badge bg-secondary">No</span>`;
  }

  function renderModeBadge(mode) {
    const value = upper(mode, "VIRTUAL");
    const cls = value === "REAL" ? "bg-primary" : "bg-secondary";
    return `<span class="badge ${cls} ms-1">${esc(value)}</span>`;
  }

  function renderStatusBadge(entryStatus, exitStatus) {
    const es = upper(entryStatus || "");
    const xs = upper(exitStatus || "NONE");

    if (xs === "READY") return `<span class="badge bg-warning text-dark">EXIT READY</span>`;
    if (xs === "SUBMITTED") return `<span class="badge bg-warning text-dark">EXIT SUBMITTED</span>`;
    if (xs === "FILLED") return `<span class="badge bg-dark">CLOSED</span>`;

    if (es === "CREATED") return `<span class="badge bg-secondary">DRAFT</span>`;
    if (es === "READY") return `<span class="badge bg-warning text-dark">CONFIRMED</span>`;
    if (es === "SUBMITTED") return `<span class="badge bg-primary">SUBMITTED</span>`;
    if (es === "FILLED") return `<span class="badge bg-success">FILLED</span>`;
    if (es === "CANCELLED") return `<span class="badge bg-danger">CANCELLED</span>`;
    if (es === "REJECTED") return `<span class="badge bg-danger">REJECTED</span>`;
    if (es === "INVALID") return `<span class="badge bg-danger">INVALID</span>`;

    return `<span class="badge bg-light text-dark border">${esc(es || "—")}</span>`;
  }

  function isDraftRow(r) {
    const es = upper(r.entry_status || r.exec_status);
    const xs = upper(r.exit_status, "NONE");
    return (es === "CREATED" || es === "READY") && xs === "NONE";
  }

  function isEditable(r) {
    return isDraftRow(r) && upper(r.entry_status || r.exec_status) === "CREATED";
  }

  function originLabel(value) {
    const origin = upper(value, "UNKNOWN");
    return ({
      SIGNAL_AUTO: "Signal Auto",
      SIGNAL_MANUAL: "Signal Manual",
      WATCHLIST: "Watchlist",
      POSITION_ADD: "Position Add",
      UNKNOWN: "Unknown"
    })[origin] || origin.replaceAll("_", " ");
  }

  function managementLabel(value) {
    const mode = upper(value, "MANUAL_PRICE");
    return mode === "SIGNAL_LIFECYCLE" ? "Signal Lifecycle" : "Manual Price-Based";
  }

  function normalizeRow(r) {
    const entryPrice = num(r.entry_price ?? 0) ?? 0;
    const qty = num(r.qty ?? r.quantity ?? 0) ?? 0;
    const last = num(r.last_price ?? entryPrice) ?? entryPrice;

    const execStatusCode = r.exec_status_code ?? "";
    const execStatusMessage = r.exec_status_message ?? "";

    const reconcileStatusCode = r.reconcile_status_code ?? "";
    const reconcileStatusMessage = r.reconcile_status_message ?? "";

    return {
      id: r.id ?? null,
      userid: r.userid ?? "",

      entry_plan_time: r.entry_plan_time ?? "",
      execution_time: r.execution_time ?? "",
      date: r.date ?? "",
      origin: upper(r.origin, "UNKNOWN"),
      management_mode: upper(r.management_mode, "MANUAL_PRICE"),
      signal_reference: r.signal_reference ?? null,

      symbol: r.symbol ?? "–",
      equity_ref: r.equity_ref ?? "",
      instrument_type: r.instrument_type ?? "–",
      trade_type: r.trade_type ?? "–",

      product: r.product ?? "MIS",
      entry_price: entryPrice,
      quantity: qty,
      qty: qty,
      last_price: last,

      required_amt: num(r.required_amt ?? (entryPrice * qty)) ?? 0,

      current_stop_price: num(r.current_stop_price),
      current_stop_amt: num(r.current_stop_amt),
      current_target_price: num(r.current_target_price),
      current_target_amt: num(r.current_target_amt),
      trade_management: r.trade_management || {},

      entry_status: upper(r.entry_status, ""),
      exit_status: upper(r.exit_status, "NONE"),
      exec_status: upper(r.exec_status, ""),
      execution_mode: upper(r.execution_mode, ""),

      exit_time: r.exit_time ?? "Active",

      entry_order_id: r.entry_order_id ?? "",
      exit_order_id: r.exit_order_id ?? "",

      exec_status_code: execStatusCode,
      exec_status_message: execStatusMessage,
      reconcile_status_code: reconcileStatusCode,
      reconcile_status_message: reconcileStatusMessage,

      last_pnl: num(r.last_pnl ?? 0) ?? 0,
      last_pnl_value: num(r.last_pnl_value ?? 0) ?? 0,
      exit_pnl: num(r.exit_pnl ?? 0) ?? 0,
      pnl_available: r.pnl_available === true,
      pnl: num(r.pnl ?? r.pnl_value ?? r.exit_pnl ?? r.last_pnl_value ?? 0) ?? 0,
      pnl_value: num(r.pnl_value ?? r.pnl ?? r.exit_pnl ?? r.last_pnl_value ?? 0) ?? 0,

      sl_hit: !!r.sl_hit,
      sl_hit_time: r.sl_hit_time ?? "",
      t1_hit: !!r.t1_hit,
      t1_hit_time: r.t1_hit_time ?? "",
      t2_hit: !!r.t2_hit,
      t2_hit_time: r.t2_hit_time ?? "",
      t3_hit: !!r.t3_hit,
      t3_hit_time: r.t3_hit_time ?? "",

      _raw: r
    };
  }

  function initTooltips() {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
      const inst = bootstrap.Tooltip.getInstance(el);
      if (inst) inst.dispose();
      new bootstrap.Tooltip(el);
    });
  }

  function actionIcons(row) {
    const record = esc(JSON.stringify(row));

    if (CURRENT_BUCKET === "draft") {
      return `
        <div class="d-flex align-items-center justify-content-center gap-1 flex-nowrap">
          <button type="button"
            class="btn btn-link btn-sm p-0 px-1 text-primary js-trade-edit"
            data-mode="draft"
            data-record="${record}"
            title="Edit Draft Order" aria-label="Edit Draft Order"
            ${isEditable(row) ? "" : "disabled"}>
            <i class="bi bi-pencil-square"></i>
          </button>
          <button type="button"
            class="btn btn-link btn-sm p-0 px-1 text-success js-order-queue"
            data-id="${esc(String(row.id))}"
            title="Quick Submit" aria-label="Quick Submit"
            ${isEditable(row) ? "" : "disabled"}>
            <i class="bi bi-check-circle"></i>
          </button>
        </div>
      `;
    }

    return `
      <div class="d-flex align-items-center justify-content-center gap-1 flex-nowrap">
        <button type="button"
          class="btn btn-link btn-sm p-0 px-1 text-primary js-order-executed"
          data-id="${esc(String(row.id))}"
          title="View Order Details" aria-label="View Order Details">
          <i class="bi bi-info-circle"></i>
        </button>
        <button type="button"
          class="btn btn-link btn-sm p-0 px-1 text-warning js-trade-edit"
          data-mode="live"
          data-record="${record}"
          title="Edit Live Risk" aria-label="Edit Live Risk">
          <i class="bi bi-pencil-square"></i>
        </button>
      </div>
    `;
  }

  function renderTable() {
    if (!DT) return;

    DT.clear();

    let totalPnl = 0;

    NORM.forEach(function (r) {
      const group = String(r?.userid || "").trim();
      const groupState = orderGroupState();
      if (showUsers() && group && !Object.prototype.hasOwnProperty.call(groupState, group)) {
        groupState[group] = true;
      }

      const pnl = Number(r.pnl_value ?? r.pnl ?? 0);
      totalPnl += pnl;

      DT.row.add([
        esc(r.userid || "—"),
        esc(r.entry_plan_time || r.date || "–"),
        esc(originLabel(r.origin)),
        esc(r.symbol),
        `${esc(r.instrument_type)} / ${esc(r.product || "MIS")} ${renderModeBadge(r.execution_mode)}`,
        esc(r.trade_type),
        fmtNum(r.entry_price),
        esc(String(r.quantity)),
        fmtNum(r.last_price),
        renderPnl(pnl, r.pnl_available),
        renderStatusBadge(r.entry_status, r.exit_status),
        actionIcons(r)
      ]);
    });

    $("#orders-total-lastpnl").html(
      `<b class="${pnlClass(totalPnl)}">${Number(totalPnl).toFixed(2)}</b>`
    );

    DT.column(0).visible(showUsers(), false);
    DT.draw(false);
    initTooltips();
  }

  function openExecutedModal(row) {
    CURRENT = row;

    $("#oe-head-user").text(row.userid || "—");
    $("#oe-head-symbol").text(row.symbol || "—");
    $("#oe-head-type").text(`${row.instrument_type || "EQ"} / ${row.product || "MIS"}`);
    $("#oe-head-side").text(row.trade_type || "BUY");
    $("#oe-head-price").text(fmtMoney(row.entry_price));

    $("#oe-entry-time").text(row.entry_plan_time || row.date || "—");
    $("#oe-origin").text(originLabel(row.origin));
    $("#oe-management-mode").text(managementLabel(row.management_mode));
    $("#oe-signal-reference").text(row.signal_reference || "—");
    $("#oe-exit-time").text(row.exit_time || "Active");
    $("#oe-last-price").text(fmtNum(row.last_price));
    $("#oe-pnl").html(
      renderPnl(row.pnl_value ?? row.pnl ?? 0, row.pnl_available)
    );
    $("#oe-status").html(renderStatusBadge(row.entry_status, row.exit_status));

    $("#oe-stop-price").text(row.current_stop_price != null ? fmtNum(row.current_stop_price) : "—");
    $("#oe-stop-amt").text(row.current_stop_amt != null ? fmtNum(row.current_stop_amt) : "—");

    $("#oe-target-price").text(row.current_target_price != null ? fmtNum(row.current_target_price) : "—");
    $("#oe-target-amt").text(row.current_target_amt != null ? fmtNum(row.current_target_amt) : "—");

    $("#oe-entry-price").text(fmtNum(row.entry_price));
    $("#oe-qty").text(row.quantity != null ? row.quantity : "—");
    $("#oe-entry-order-id").text(row.entry_order_id || "—");
    $("#oe-exit-order-id").text(row.exit_order_id || "—");
    $("#oe-execution-mode").text(row.execution_mode || "—");

    const hasExec = !!(row.exec_status_code || row.exec_status_message);
    const hasReconcile = !!(row.reconcile_status_code || row.reconcile_status_message);
    const hasStatus = hasExec || hasReconcile;

    $("#oe-error-wrap").toggleClass("d-none", !hasStatus);

    const execCodeText = row.exec_status_code || "—";
    const execMessageText = row.exec_status_message || "—";
    const reconcileCodeText = row.reconcile_status_code || "—";
    const reconcileMessageText = row.reconcile_status_message || "—";

    $("#oe-error-code").html(
      `EXEC: ${esc(execCodeText)}<br>RECON: ${esc(reconcileCodeText)}`
    );
    $("#oe-error-message").html(
      `EXEC: ${esc(execMessageText)}<br>RECON: ${esc(reconcileMessageText)}`
    );

    const mdl = document.getElementById("executedOrderModal");
    if (mdl) bootstrap.Modal.getOrCreateInstance(mdl, { focus: true }).show();
  }

  async function queueOrderById(orderId) {
    try {
      const resp = await fetch(cfg().submitTradeUrl || "/dashboard/trading/submit_trade", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: orderId })
      });

      const payload = await resp.json().catch(() => ({}));
      return { ok: resp.ok && !!payload.ok, payload };
    } catch (e) {
      console.error("queueOrderById failed", e);
      return { ok: false, payload: { error: "network_error" } };
    }
  }

  async function refreshOrdersAndPositions() {
    await loadOrders(CURRENT_BUCKET);
    if (window.DashboardAPI && typeof DashboardAPI.fetch === "function") {
      try {
        DashboardAPI.fetch("positions");
      } catch (e) {
        console.warn("DashboardAPI.fetch('positions') failed", e);
      }
    }
  }

  $(document).on("click", ".js-order-executed", function () {
    const id = $(this).data("id");
    const row = NORM.find(x => String(x.id) === String(id));
    if (row) openExecutedModal(row);
  });

  $(document).on("click", ".js-order-queue", async function () {
    const id = $(this).data("id");
    const row = NORM.find(x => String(x.id) === String(id));
    if (!row || !isEditable(row)) return;

    const res = await queueOrderById(row.id);
    if (res.ok) {
      await refreshOrdersAndPositions();
    } else {
      alert(
        res.payload?.message ||
        res.payload?.skipped?.[0]?.details ||
        res.payload?.error ||
        res.payload?.reason ||
        "Unable to queue order."
      );
    }
  });

  function applyFilters() {
    const userFilter = selectedUserFilter();
    const modeFilter = selectedModeFilter();

    updateOrdersDownloadLink();

    NORM = ALL_NORM.filter(function (row) {
      const userOk = userFilter === "ALL" || upper(row.userid) === userFilter;
      const modeOk = modeFilter === "ALL" || upper(row.execution_mode, "VIRTUAL") === modeFilter;
      return userOk && modeOk;
    });

    renderTable();

    const q = $("#or-search").val() || "";
    if (DT) DT.search(q).draw(false);
  }

  function populateOrders(payload) {
    try {
      RAW = Array.isArray(payload) ? payload : (payload?.data || []);
    } catch (e) {
      console.error("populateOrders: bad payload", e);
      RAW = [];
    }

    ALL_NORM = RAW.map(normalizeRow);
    applyFilters();
  }

  async function loadManagedUsers() {
    const usersUrl = cfg().managedUsersUrl || cfg().usersUrl;
    if (!showUsers() || !usersUrl || !$("#or-user-filter").length) return;

    const $select = $("#or-user-filter");
    const previous = String($select.val() || "").trim().toUpperCase();

    try {
      const resp = await fetch(usersUrl, { credentials: "same-origin", cache: "no-store" });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok || payload.status !== "success") return;

      const users = Array.isArray(payload.data) ? payload.data : [];
      $select.empty().append(new Option("All users", "ALL"));
      users.forEach(function (row) {
        const userid = upper(row?.userid);
        if (userid) $select.append(new Option(userid, userid));
      });

      const preferred = previous || "ALL";
      $select.val($select.find(`option[value="${preferred}"]`).length ? preferred : "ALL");
    } catch (e) {
      console.warn("loadManagedUsers failed", e);
    }
  }

  window.populateOrders = populateOrders;

  async function loadOrders(bucket, options = {}) {
    const requestedBucket = bucket === "executed" ? "executed" : "draft";
    const bucketChanged = requestedBucket !== CURRENT_BUCKET;
    CURRENT_BUCKET = requestedBucket;

    if (options.resetGroups) {
      resetOrderGroupState(requestedBucket);
    }

    const requestSequence = ++orderLoadSequence;

    if (orderLoadController) {
      orderLoadController.abort();
    }
    orderLoadController = new AbortController();

    // Never leave rows from the other bucket visible while the new request is pending.
    if (bucketChanged || options.clearImmediately) {
      RAW = [];
      ALL_NORM = [];
      NORM = [];
      renderTable();
    }

    try {
      const url = `/dashboard/orders/data?bucket=${encodeURIComponent(requestedBucket)}&limit=5000&_=${Date.now()}`;
      const resp = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        signal: orderLoadController.signal
      });
      const payload = await resp.json().catch(() => ({}));

      // Ignore a late response from a previous bucket/refresh request.
      if (requestSequence !== orderLoadSequence || requestedBucket !== CURRENT_BUCKET) return;
      if (payload.status !== "success") return;
      if (payload.bucket && String(payload.bucket).toLowerCase() !== requestedBucket) {
        console.warn("loadOrders ignored mismatched bucket response", payload.bucket, requestedBucket);
        return;
      }

      populateOrders(payload.data || []);
    } catch (e) {
      if (e && e.name === "AbortError") return;
      console.error("loadOrders failed", e);
    }
  }

  $(document).on("click", "#orders-table tbody tr.order-group-row", function () {
    if (!showUsers()) return;

    const group = String($(this).attr("data-group") || "").trim();
    if (!group) return;

    const state = orderGroupState();
    state[group] = !isOrderGroupCollapsed(group);
    DT.draw(false);
  });

  $(document).on("change", 'input[name="orders-bucket"]', function () {
    const bucket = $('input[name="orders-bucket"]:checked').val() || "draft";
    loadOrders(bucket, { resetGroups: true, clearImmediately: true });
  });

  $(document).on("change", "#or-length", function () {
    if (DT) DT.page.len(parseInt(this.value, 10) || -1).draw(false);
  });

  $(document).on("input", "#or-search", function () {
    if (DT) DT.search(this.value).draw();
  });

  $(document).on("change", "#or-user-filter, #or-mode-filter", function () {
    applyFilters();
  });

  $(document).on("hidden.bs.modal", "#executedOrderModal", function () {
    CURRENT = null;
  });

  $(function () {
    updateOrdersDownloadLink();

    DT = $("#orders-table").DataTable({
      responsive: true,
      autoWidth: false,
      dom: "rtip",
      pageLength: -1,
      lengthMenu: [[100, 150, 200, -1], [100, 150, 200, "All"]],
      order: showUsers() ? [[0, "asc"], [1, "desc"]] : [[1, "desc"]],
      rowGroup: showUsers() ? {
        dataSrc: 0,
        startRender: function (rows, group) {
          const collapsed = isOrderGroupCollapsed(group);

          rows.nodes().each(function (r) {
            r.style.display = collapsed ? "none" : "";
          });

          let totalPnl = 0;
          let draftCount = 0;
          let executedCount = 0;

          rows.data().each(function (row) {
            const raw = String(row[9] || "")
              .replace(/<[^>]*>/g, "")
              .replace(/,/g, "")
              .trim();
            const n = Number(raw);
            if (Number.isFinite(n)) totalPnl += n;

            const statusText = String(row[10] || "").toUpperCase();
            if (statusText.includes("DRAFT") || statusText.includes("CONFIRMED")) draftCount += 1;
            else executedCount += 1;
          });

          const pnlCls = totalPnl >= 0 ? "dash-text-success" : "dash-text-danger";
          const icon = collapsed ? "bi-chevron-right" : "bi-chevron-down";
          const metaText = CURRENT_BUCKET === "draft"
            ? `Draft: ${draftCount}`
            : `Executed: ${executedCount}`;

          return $(`
            <tr class="table-light order-group-row" data-group="${group}" style="cursor:pointer;">
              <td colspan="12">
                <div class="d-flex align-items-center justify-content-between">
                  <div class="fw-semibold d-flex align-items-center gap-2">
                    <i class="bi ${icon}"></i>
                    <span>User: ${group}</span>
                  </div>
                  <div class="small text-muted">
                    Rows: ${rows.count()} &nbsp;|&nbsp;
                    ${metaText} &nbsp;|&nbsp;
                    P&amp;L:
                    <span class="${pnlCls}">${totalPnl.toFixed(2)}</span>
                  </div>
                </div>
              </td>
            </tr>
          `);
        }
      } : undefined,
      columnDefs: [
        { targets: 0, visible: showUsers() },
        { orderable: false, targets: -1 }
      ],
      drawCallback() {
        initTooltips();
      }
    });

    if (window.DashboardAPI && typeof window.DashboardAPI.register === "function") {
      try {
        DashboardAPI.register("orders", async function () {
          await loadOrders(CURRENT_BUCKET);
        });
      } catch (e) {
        console.warn("DashboardAPI.register('orders') failed", e);
      }
    }

    document.addEventListener("trade:edit:refresh", async function () {
      await refreshOrdersAndPositions();
    });

    loadManagedUsers().finally(function () {
      loadOrders("draft");
    });
  });

})();