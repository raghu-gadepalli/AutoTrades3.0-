(function () {
  "use strict";

  let currentSnapshotData = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, val) {
    const el = byId(id);
    if (el) el.textContent = val;
  }

  function setHtml(id, val) {
    const el = byId(id);
    if (el) el.innerHTML = val;
  }

  function esc(v) {
    return String(v ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(v, d = 2) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(d) : "—";
  }

  function intNum(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString("en-IN") : "—";
  }

  function pct(v, d = 2) {
    const n = Number(v);
    return Number.isFinite(n) ? `${n.toFixed(d)}%` : "—";
  }

  function signed(v, d = 2) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return `${n > 0 ? "+" : ""}${n.toFixed(d)}`;
  }

  function signedPct(v, d = 2) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return `${n > 0 ? "+" : ""}${n.toFixed(d)}%`;
  }

  function text(v, fallback = "—") {
    if (v === null || v === undefined) return fallback;
    const s = String(v).trim();
    return s || fallback;
  }

  function fmtDateTime(v) {
    if (!v) return "—";
    try {
      const d = new Date(v);
      if (isNaN(d.getTime())) return text(v);
      return d.toLocaleString("en-IN", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return text(v);
    }
  }

  function get(obj, path, fallback = null) {
    try {
      return path.split(".").reduce((a, k) => a?.[k], obj) ?? fallback;
    } catch {
      return fallback;
    }
  }


  function first(data, paths, fallback = null) {
    for (const path of paths) {
      const v = get(data, path, null);
      if (v !== null && v !== undefined && String(v).trim() !== "") return v;
    }
    return fallback;
  }

  function structureAccepted(data) {
    return get(data, "structure.accepted", {}) || {};
  }

  function structureRaw(data) {
    return get(data, "structure.raw", {}) || {};
  }


  function structureState(data) {
    return first(data, ["structure.accepted.state", "structure.state", "indicators.hma.state"], "UNKNOWN");
  }

  function structureSide(data) {
    return first(data, ["structure.accepted.side", "structure.side"], "NEUTRAL");
  }

  function badgeClass(v) {
    const s = String(v || "").toUpperCase();

    if (s.includes("BUY") || s === "UP" || s === "BULLISH" || s === "ABOVE") {
      return "text-success";
    }

    if (s.includes("SELL") || s === "DOWN" || s === "BEARISH" || s === "BELOW") {
      return "text-danger";
    }

    if (
      s.includes("WEAK") ||
      s.includes("WATCH") ||
      s.includes("ATTEMPT") ||
      s.includes("COMPRESSION")
    ) {
      return "text-warning";
    }

    return "text-body";
  }

  function renderSnapshotModal(data) {
    if (!data) return;
    currentSnapshotData = data;

    setText("smSymbol", text(data.symbol));
    setText("smTime", fmtDateTime(data.snapshot_time));
    setText("smPrice", num(data.close));
    setText("smVwapDelta", signedPct(get(data, "indicators.vwap.distance_pct"), 2));

    const state = text(structureState(data));
    const strength = text(get(data, "indicators.hma.strength"));

    setHtml("smState", `<span class="${badgeClass(state)}">${esc(state)}</span>`);
    setHtml("smStrength", `<span class="${badgeClass(strength)}">${esc(strength)}</span>`);

    setHtml("smIndicatorsBody", renderSnapshotIndicatorsRows(data));
    setHtml("smFutureBody", renderSnapshotFutureRows(get(data, "derivatives.future_sentiment_windows.5m")));
    setHtml("smOptionBody", renderSnapshotOptionRows(get(data, "derivatives.option_sentiment_windows.5m")));
    setHtml("smDerivSummaryBody", renderSnapshotDerivSummaryRows(data));

    bootstrap.Modal.getOrCreateInstance(byId("snapshotModal")).show();
  }

  function renderSnapshotIndicatorsRows(data) {
    const accepted = structureAccepted(data);
    const raw = structureRaw(data);
    const candidate = get(data, "structure.candidate", {}) || {};
    const auction = get(data, "auction", {}) || {};
    const observation = auction.observation || {};
    const lifecycle = auction.lifecycle || {};
    const directional = lifecycle.directional || {};
    const balance = lifecycle.balance || {};
    const auctionEvents = Array.isArray(lifecycle.events) ? lifecycle.events : [];
    const permissions = Array.isArray(lifecycle.permissions) ? lifecycle.permissions : [];

    const rangeText = (range) => {
      const r = range || {};
      return `${num(r.low)} - ${num(r.high)} · width ${pct(r.width_pct)} · ${text(r.range_type)}`;
    };

    const windowText = (name) => {
      const w = get(data, `market_windows.${name}`, {}) || {};
      return `${text(w.status)} · move ${signedPct(w.move_pct)} · range ${pct(w.range_pct)} · close pos ${num(w.close_position_in_range, 3)}`;
    };

    const rows = [
      ["VWAP", `Value ${num(get(data, "indicators.vwap.value"))} · Δ ${signedPct(get(data, "indicators.vwap.distance_pct"))}`],
      ["HMA", `${text(get(data, "indicators.hma.state"))} · ${text(get(data, "indicators.hma.strength"))} · flips ${intNum(get(data, "indicators.hma.flip_count_today"))}`],
      ["RSI", `${num(get(data, "indicators.rsi.value"))} · ${text(get(data, "indicators.rsi.zone"))}`],
      ["ADX", `${num(get(data, "indicators.adx.value"))} · ${text(get(data, "indicators.adx.band"))}`],
      ["ATR", `${num(get(data, "indicators.atr.value"))} · ${text(get(data, "indicators.atr.band"))}`],
      ["BB Zone", `${text(get(data, "indicators.bollinger.zone"))} · Pos ${num(get(data, "indicators.bollinger.position"), 3)}`],

      ["Auction Observation", `${text(observation.observation_state, "UNKNOWN")} · bias ${text(observation.directional_bias, "UNKNOWN")}`],
      ["Directional Episode", `${text(directional.current_state, "NONE")} · ${text(directional.direction, "UNKNOWN")} · ${text(directional.episode_id, "—")}`],
      ["Balance Episode", `${text(balance.current_state, "NONE")} · ${num(balance.frozen_low)} - ${num(balance.frozen_high)} · ${text(balance.episode_id, "—")}`],
      ["Authoritative Events", auctionEvents.length ? auctionEvents.map(event => text(event.event_type)).join(", ") : "—"],
      ["Setup Permissions", permissions.length ? permissions.map(item => `${text(item.setup_family)}:${text(item.result)}`).join(", ") : "—"],
      ["Auction Continuity", `${text(auction.continuity_mode, "UNKNOWN")} · previous ${fmtDateTime(auction.previous_snapshot_time)}`],

      ["Accepted Structure", `${text(accepted.state, "UNKNOWN")} · frozen ${accepted.frozen === true ? "YES" : "NO"}`],
      ["Accepted Range", rangeText(accepted.range)],
      ["Raw Structure", `${text(raw.state, "UNKNOWN")} · ${text(raw.side, "NEUTRAL")}`],
      ["Raw Range", rangeText(raw.range)],
      ["Candidate", `${text(candidate.status, "NONE")} · ${text(candidate.side, "NEUTRAL")} · active ${text(candidate.active, false)} · bars ${intNum(candidate.bars_confirmed)}`],
      ["Structure Flips", intNum(get(data, "structure.flip_count_today"))],

      ["Opening Range", `${num(get(data, "levels.opening_range.low"))} - ${num(get(data, "levels.opening_range.high"))} · ready ${text(get(data, "levels.opening_range.ready", false))}`],
      ["Previous Day", `PDL ${num(get(data, "levels.prev_day.low"))} · PDH ${num(get(data, "levels.prev_day.high"))} · close ${num(get(data, "levels.prev_day.close"))}`],
      ["15m Window", windowText("15m")],
      ["30m Window", windowText("30m")],
      ["60m Window", windowText("60m")],
      ["Session Window", windowText("sod")],
      ["Price Action", `${text(get(data, "price_action.slope.state"))} · 3-bar ATR ${num(get(data, "price_action.slope.bars_3_atr"), 3)} · 5-bar ATR ${num(get(data, "price_action.slope.bars_5_atr"), 3)}`],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th style="width: 180px;">${esc(k)}</th>
        <td>${esc(v)}</td>
      </tr>
    `).join("");
  }

  function renderSnapshotFutureRows(fw) {
    if (!fw) {
      return `<tr><td colspan="2" class="text-center text-muted">—</td></tr>`;
    }

    const rows = [
      ["Label", `<span class="${badgeClass(fw.label)}">${esc(text(fw.label))}</span>`],
      ["ΔLTP", esc(signed(fw.fut_ltp_delta))],
      ["ΔOI", esc(signed(fw.fut_oi_delta, 0))],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th style="width: 180px;">${k}</th>
        <td>${v}</td>
      </tr>
    `).join("");
  }

  function renderSnapshotOptionRows(ow) {
    if (!ow) {
      return `<tr><td colspan="2" class="text-center text-muted">—</td></tr>`;
    }

    const strength = ow.strength == null ? "—" : pct(Number(ow.strength) * 100, 0);
    const driverLabel = text(get(ow, "driver.label"));
    const driverShare = get(ow, "driver.share");
    const driverTxt = driverShare == null
      ? driverLabel
      : `${driverLabel} (${pct(Number(driverShare) * 100, 0)})`;

    const rows = [
      ["Indication", `<span class="${badgeClass(ow.indication)}">${esc(text(ow.indication))}</span>`],
      ["Strength", esc(strength)],
      ["Driver", esc(driverTxt)],
      ["PCR", esc(num(ow.pcr_now, 3))],
      ["ΔPCR", esc(signed(ow.pcr_delta, 3))],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th style="width: 180px;">${k}</th>
        <td>${v}</td>
      </tr>
    `).join("");
  }

  function renderSnapshotDerivSummaryRows(data) {
    const lite = get(data, "derivatives.options_lite", {});
    const rows = [
      ["ATM Strike", text(lite.atm_strike)],
      ["PCR", num(lite.pcr, 3)],
      ["Support", text(lite.support)],
      ["Resistance", text(lite.resistance)],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th style="width: 180px;">${esc(k)}</th>
        <td>${esc(v)}</td>
      </tr>
    `).join("");
  }

  function renderSnapdervModal(data) {
    if (!data) return;

    setText("snapdervMetaLine", `${text(data.symbol)} | ${fmtDateTime(data.snapshot_time)}`);

    setText("sdFutureInstrument", text(get(data, "derivatives.future.instrument")));
    setText("sdFutureLtp", num(get(data, "derivatives.future.last_price")));
    setText("sdFutureOi", intNum(get(data, "derivatives.future.oi")));
    setText("sdFutureVolume", intNum(get(data, "derivatives.future.volume")));
    setText("sdFutureExpiry", text(get(data, "derivatives.future.expiry")));

    setHtml("sdOptionsSummaryBody", renderOptionsSummaryRows(data));
    setHtml("sdTopCallsBody", renderTopOptionsRows(get(data, "derivatives.options_lite.top_calls", [])));
    setHtml("sdTopPutsBody", renderTopOptionsRows(get(data, "derivatives.options_lite.top_puts", [])));
    setHtml("sdFutureWindowsBody", renderFutureWindowsRows(get(data, "derivatives.future_sentiment_windows", {})));
    setHtml("sdOptionWindowsBody", renderOptionWindowsRows(get(data, "derivatives.option_sentiment_windows", {})));
    setHtml("sdFlowBreakdownBody", renderFlowBreakdownRows(get(data, "derivatives.option_sentiment_windows.5m")));

    bootstrap.Modal.getOrCreateInstance(byId("snapdervModal")).show();
  }

  function renderOptionsSummaryRows(data) {
    const lite = get(data, "derivatives.options_lite", {});
    const rows = [
      ["PCR", num(lite.pcr, 3)],
      ["Support", text(lite.support)],
      ["Resistance", text(lite.resistance)],
      ["ATM Strike", text(lite.atm_strike)],
      ["Max Pain", text(lite.max_pain)],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th style="width: 180px;">${esc(k)}</th>
        <td>${esc(v)}</td>
      </tr>
    `).join("");
  }

  function renderTopOptionsRows(rows) {
    const data = Array.isArray(rows) ? rows : [];
    if (!data.length) {
      return `<tr><td colspan="3" class="text-center text-muted">—</td></tr>`;
    }

    return data.map((r) => `
      <tr>
        <td>${esc(text(r.strike))}</td>
        <td>${esc(intNum(r.oi))}</td>
        <td>${esc(num(r.ltp))}</td>
      </tr>
    `).join("");
  }

  function renderFutureWindowsRows(windows) {
    const order = ["5m", "15m", "60m", "sod"];
    const rows = order.filter(k => windows && windows[k]).map((k) => {
      const w = windows[k];
      return `
        <tr class="${k === "5m" ? "table-light" : ""}">
          <td>${esc(k.toUpperCase())}</td>
          <td class="${badgeClass(w.label)}">${esc(text(w.label))}</td>
          <td>${esc(signed(w.fut_ltp_delta))}</td>
          <td>${esc(signed(w.fut_oi_delta, 0))}</td>
        </tr>
      `;
    });

    return rows.length ? rows.join("") : `<tr><td colspan="4" class="text-center text-muted">—</td></tr>`;
  }

  function renderOptionWindowsRows(windows) {
    const order = ["5m", "15m", "60m", "sod"];
    const rows = order.filter(k => windows && windows[k]).map((k) => {
      const w = windows[k];
      const strength = w.strength == null ? "—" : pct(Number(w.strength) * 100, 0);
      const driverLabel = text(get(w, "driver.label"));
      const driverShare = get(w, "driver.share");
      const driverTxt = driverShare == null
        ? driverLabel
        : `${driverLabel} (${pct(Number(driverShare) * 100, 0)})`;

      return `
        <tr class="${k === "5m" ? "table-light" : ""}">
          <td>${esc(k.toUpperCase())}</td>
          <td class="${badgeClass(w.indication)}">${esc(text(w.indication))}</td>
          <td>${esc(strength)}</td>
          <td>${esc(driverTxt)}</td>
          <td>${esc(num(w.pcr_now, 3))}</td>
          <td>${esc(signed(w.pcr_delta, 3))}</td>
        </tr>
      `;
    });

    return rows.length ? rows.join("") : `<tr><td colspan="6" class="text-center text-muted">—</td></tr>`;
  }

  function renderFlowBreakdownRows(w5) {
    const c = get(w5, "components");
    if (!c) {
      return `<tr><td colspan="2" class="text-center text-muted">—</td></tr>`;
    }

    const items = [
      ["CE Writing", c.ce_writing],
      ["PE Writing", c.pe_writing],
      ["CE Long Unwind", c.ce_long_unwind],
      ["CE Short Cover", c.ce_short_cover],
      ["PE Long Unwind", c.pe_long_unwind],
      ["PE Short Cover", c.pe_short_cover],
      ["CE Long Buildup", c.ce_long_buildup],
      ["PE Long Buildup", c.pe_long_buildup],
    ];

    return items.map(([k, v]) => `
      <tr>
        <th style="width: 220px;">${esc(k)}</th>
        <td>${esc(num(v, 2))}</td>
      </tr>
    `).join("");
  }

  function openSnapderv() {
    if (!currentSnapshotData) return;
    bootstrap.Modal.getOrCreateInstance(byId("snapshotModal")).hide();
    renderSnapdervModal(currentSnapshotData);
  }

  function backToSnapshot() {
    bootstrap.Modal.getOrCreateInstance(byId("snapdervModal")).hide();
    bootstrap.Modal.getOrCreateInstance(byId("snapshotModal")).show();
  }

  document.addEventListener("DOMContentLoaded", () => {
    byId("btnOpenSnapDerv")?.addEventListener("click", openSnapderv);
    byId("btnBackToSnapshot")?.addEventListener("click", backToSnapshot);
  });

  window.renderSnapshotModal = renderSnapshotModal;
})();